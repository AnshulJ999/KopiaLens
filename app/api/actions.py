"""Operational actions.

Destructive actions are preview-then-execute: a dry run (or, where kopia has none, a
preview) runs first and the execute path is confirmed in-app before anything mutates.
"""

from __future__ import annotations

import threading
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import audit
from ..core.kopia import (
    MountError,
    list_mounts,
    maintenance_run_command,
    mount_and_browse,
    mount_snapshot,
    next_free_drive,
    open_in_explorer,
    pick_folder,
    remove_files_bat_text,
    remove_files_chunked_commands,
    remove_files_command,
    remove_files_dry_run,
    snapshot_delete_commands,
    snapshot_delete_dry_run,
    unmount,
)
from .errors import PASS_THROUGH
from ..core.nightly import start_index_pass_async
from ..index.holding import snapshots_for_path
from ..index.physical import batch_detail, folder_detail
from ..index.queries import paths_named, remove_targets, remove_targets_summary
from ..settings import load_settings

router = APIRouter(tags=["actions"])

_SEP = chr(92)  # backslash
_FILENAME_SAMPLE = 500   # collateral paths listed for a filename-mode plan (the count is exact)

# Previewed plans, by token. The panel's job start passes the token back, so the delete runs
# the exact list the user approved instead of re-deriving it from the filter (a reindex landing in
# between - the nightly, or the 5-minute post-delete pass - could otherwise add files nobody saw).
_PLANS: dict[str, dict] = {}
_PLANS_LOCK = threading.Lock()
_PLANS_TTL = 3600.0
_PLANS_MAX = 20


def index_generation(repo: str) -> int | None:
    """The published generation's number, or None when the repo has no index yet."""
    try:
        from ..index import store
        return int((store.read_manifest(repo) or {}).get("generation") or 0) or None
    except Exception:  # noqa: BLE001 - a missing manifest simply means "unknown"
        return None


def _remember_plan(plan: dict) -> str:
    token = uuid.uuid4().hex[:12]
    now = time.time()
    with _PLANS_LOCK:
        for old in [t for t, p in _PLANS.items() if now - p["created"] > _PLANS_TTL]:
            _PLANS.pop(old, None)
        while len(_PLANS) >= _PLANS_MAX:
            _PLANS.pop(next(iter(_PLANS)), None)
        _PLANS[token] = {"created": now, **plan}
    return token


def previewed_plan(token: str) -> dict | None:
    """The stored plan for a token, or None when it was never stored or has expired."""
    if not token:
        return None
    with _PLANS_LOCK:
        plan = _PLANS.get(token)
        if plan is None or time.time() - plan["created"] > _PLANS_TTL:
            _PLANS.pop(token, None)
            return None
        return dict(plan)


def _source_root(source: str) -> str:
    return source.split(":", 1)[1] if ":" in source else source


def _start_index_run(repo: str) -> dict:
    """The same worker pass + bookkeeping the Index button and the nightly scheduler use
    (running flag, freshness callback), so an action-triggered reindex updates those too."""
    from ..main import mark_nightly_result  # lazy: app.main imports this router
    return start_index_pass_async(repo, on_done=mark_nightly_result)


def _fmt_cmd(cmd: list[str]) -> str:
    """Render a kopia arg-list as a shell-pasteable string, double-quoting args with spaces
    (sources/filenames like 'C:\\Program Files\\Apollo' would otherwise split on paste)."""
    return " ".join(f'"{a}"' if (not a or " " in a) else a for a in cmd)


class MountRequest(BaseModel):
    repo: str
    snapshot_id: str
    mountpoint: str


class MountOpenRequest(BaseModel):
    repo: str
    snapshot_id: str | None = None  # None = the newest current snapshot holding full_path
    source: str
    full_path: str | None = None  # a file/folder inside the source; None = mount root only
    mountpoint: str | None = None  # auto-assigned free drive letter when omitted


class SnapshotDeleteRequest(BaseModel):
    repo: str
    snapshot_ids: list[str]


class RemoveFilesRequest(BaseModel):
    repo: str
    by: str = "object-id"   # "object-id" (precise content) | "filename" (by name)
    everywhere: bool = False  # object-id: drop --source to remove from ALL sources (sweeps whole repo)
    sources: list[str] | None = None  # explicit --source scope (the Delete panel's ticked backups)
    source: str | None = None      # single-file form
    full_path: str | None = None
    files: list[dict] | None = None  # batch form: [{"source":..., "full_path":...}]


class FolderReclaimRequest(BaseModel):
    repo: str
    source: str
    prefix: str


class UnmountRequest(BaseModel):
    pid: int


def _snapshot_info(repo: str, snapshot_id: str, source: str | None) -> dict:
    """Source and start time for the mount list."""
    try:
        from ..core.sources import sources_index
        for src, snaps in sources_index(repo).items():
            if source and src != source:
                continue
            for sn in snaps:
                if sn.get("id") == snapshot_id:
                    return {"source": src, "snap_time": sn.get("startTime", "")}
    except Exception:  # noqa: BLE001 - labels only
        pass
    return {"source": source or ""}


@router.post("/actions/mount")
def mount(payload: MountRequest) -> dict:
    try:
        proc = mount_snapshot(payload.repo, payload.snapshot_id, payload.mountpoint,
                              info=_snapshot_info(payload.repo, payload.snapshot_id, None))
        return {"pid": proc.pid, "command": proc.args}
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except MountError as exc:
        raise HTTPException(status_code=400, detail=f"Mount failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/actions/mount-open")
def mount_open(payload: MountOpenRequest) -> dict:
    """Mount a snapshot (auto drive letter if none given) and open Explorer at the file/folder
    inside it. Tracked in the mount manager like any other mount."""
    if not payload.snapshot_id:
        from ..api.restore import RestoreItem, resolve_item
        r = resolve_item(payload.repo, RestoreItem(source=payload.source,
                                                   full_path=payload.full_path or _source_root(payload.source)))
        payload.snapshot_id = r["snapshot_id"]
    mountpoint = payload.mountpoint or next_free_drive()
    if not mountpoint:
        raise HTTPException(status_code=409, detail="no free drive letter for an auto mount; unmount something first")
    in_mount_target = None
    if payload.full_path:
        root = _source_root(payload.source).rstrip(_SEP)
        rel = payload.full_path[len(root):].lstrip(_SEP) if payload.full_path.startswith(root) else ""
        in_mount_target = (mountpoint + _SEP + rel) if rel else (mountpoint + _SEP)
    try:
        proc = mount_and_browse(payload.repo, payload.snapshot_id, mountpoint, in_mount_target=in_mount_target,
                                info={**_snapshot_info(payload.repo, payload.snapshot_id, payload.source),
                                      "path": payload.full_path or ""})
        return {"pid": proc.pid, "command": proc.args, "mountpoint": mountpoint, "opening": in_mount_target}
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except MountError as exc:
        raise HTTPException(status_code=400, detail=f"Mount failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/actions/snapshot-delete")
def snapshot_delete(payload: SnapshotDeleteRequest) -> dict:
    """The exact delete commands (preview, nothing happens). Space frees on the next maintenance
    run (kopia's own step). Executing is the job runner's, not a route's - the old
    confirm=true branch ran kopia outside the runner's locks, audit and policy backups."""
    if not payload.snapshot_ids:
        raise HTTPException(status_code=400, detail="snapshot_ids is empty")
    cmds = snapshot_delete_commands(payload.repo, payload.snapshot_ids, batch=True)
    text = "\n".join(" ".join(c) for c in cmds)
    maint = " ".join(maintenance_run_command(payload.repo))
    audit.audit("generated", "snapshot-delete-commands", repo=payload.repo,
                summary={"ids": len(payload.snapshot_ids)})
    return {"executed": False, "count": len(payload.snapshot_ids),
            "commands": text + "\n\n# Then reclaim space with:\n" + maint}


def _rf_targets(payload: RemoveFilesRequest) -> list[tuple[str, str]]:
    if payload.files:
        return [(f["source"], f["full_path"]) for f in payload.files if f.get("source") and f.get("full_path")]
    if payload.source and payload.full_path:
        return [(payload.source, payload.full_path)]
    return []


def _rf_preview(repo: str, items: list[str], flag: str, scope, by: str) -> dict:
    """confirm=false response for the batch-remove panel: 8k-chunked (terminal-pasteable) dry-run +
    commit text, batch count, and a downloadable .bat. Keeps the batch panel self-sufficient for
    large selections without sharing the folder-delete plan machinery."""
    commit_cmds = remove_files_chunked_commands(repo, items, source=scope, commit=True, max_chars=8000, flag=flag)
    dry_cmds = remove_files_chunked_commands(repo, items, source=scope, commit=False, max_chars=8000, flag=flag)
    ts = time.strftime("%Y%m%d_%H%M%S")
    audit.audit("generated", "remove-files-commands", repo=repo,
                summary={"items": len(items), "by": by, "batches": len(commit_cmds),
                         "sources": scope or [], "everywhere": scope is None})
    return {
        "executed": False, "by": by, "count": len(items), "everywhere": scope is None,
        "sources": scope or [], "batches": len(commit_cmds),
        "dry_run": "\n".join(_fmt_cmd(c) for c in dry_cmds),
        "commit": "\n".join(_fmt_cmd(c) for c in commit_cmds),
        "bat_name": f"kopialens_remove_repo{repo}_{ts}.bat",
        "bat": remove_files_bat_text(commit_cmds, f"kopialens_remove_repo{repo}_{ts}.log"),
    }


def _rf_resolve(payload: RemoveFilesRequest) -> tuple[list[str], list[str], list[str] | None]:
    """Content objects + basenames + --source scope for a remove-files request.

    Object-id mode scopes to every source referencing the content (a copy left elsewhere keeps it
    alive). Filename mode scopes to the sources holding the selected paths: a source that only
    shares the bytes under another name would match unrelated same-named files. ``sources``
    overrides both; ``everywhere`` drops --source."""
    from .deleteplan import resolve_scope, selection_facts
    targets = _rf_targets(payload)
    if not targets:
        raise HTTPException(status_code=400, detail="no files specified")
    facts = selection_facts(payload.repo, targets)
    if payload.everywhere:
        scope = None
    else:
        scope = resolve_scope(payload.repo, payload.by, payload.sources, facts)
    return facts["oids"], facts["names"], scope


@router.post("/actions/remove-files")
def remove_files(payload: RemoveFilesRequest) -> dict:
    """The dry-run + commit commands for removing file(s) from ALL snapshots (nothing runs),
    via `kopia snapshot fix remove-files`. by="object-id" removes the exact content everywhere
    (no --source); by="filename" is scoped per source. Executing is the job runner's."""
    oids, names, scope = _rf_resolve(payload)

    if payload.by == "filename":
        return _rf_preview(payload.repo, names, "--filename", scope, "filename")
    if not oids:
        raise HTTPException(status_code=404, detail="no content objects found")
    return _rf_preview(payload.repo, oids, "--object-id", scope, "object-id")


def _dry_response(r: dict) -> dict:
    """Uniform dry-run response: the run's own verdict rides in `ok`, never raised - the
    output IS the answer the user asked to see, errors included."""
    r["ok"] = r.get("returncode") == 0
    return r


@router.post("/actions/remove-files-dry-run")
def remove_files_dry_run_route(payload: RemoveFilesRequest) -> dict:
    """RUN remove-files without --commit: kopia walks every snapshot in scope and reports what it
    would rewrite, changing nothing. Synchronous (spinner); the shared job runner streams it later."""
    oids, names, scope = _rf_resolve(payload)
    if payload.by == "filename":
        if not names:
            raise HTTPException(status_code=404, detail="no matching files")
        r = remove_files_dry_run(payload.repo, filenames=names, source=scope)
    else:
        if not oids:
            raise HTTPException(status_code=404, detail="no content objects found")
        r = remove_files_dry_run(payload.repo, object_ids=oids, source=scope)
    return _dry_response(r)


class RemovePlanRequest(BaseModel):
    """Chunked folder/filter delete: removes the files matching the CURRENT view/filter (optionally
    scoped to a folder ``prefix``), biggest-files-first, via chunked remove-files."""
    repo: str
    query: str = ""
    source: str | None = None
    ext: str | None = None
    ext_exclude: str | None = None
    deleted_only: bool = False
    over_gb: float = 0
    min_gb: float = 0
    max_gb: float = 0
    min_versions: int = 0
    shared_min: int = 0
    date_from: str | None = None
    date_to: str | None = None
    first_from: str | None = None   # "recently backed up" cutoff (first_seen >= date), like Search
    prefix: str | None = None       # folder scope (Tree/Bloat); None = the whole filter
    all_versions: bool = False      # B2: every version's object vs latest only
    everywhere: bool = False        # B1: drop --source (whole-repo sweep) vs scope to the folder's sources
    by: str = "object-id"           # "object-id" (exact content) | "filename" (by basename)
    cap: int | None = None          # None = the remove_plan_cap setting (the UI sends top_n here)
    parts: list[dict] | None = None  # several folders/files: [{source|None, prefix}]; replaces prefix/source


DEFAULT_REMOVE_PLAN_CAP = 200000


def _plan_cap() -> int:
    """Ceiling on how many files one chunked-delete plan may enumerate. A setting rather than a
    constant so a large repo can raise it deliberately; the no-filter 400 guard below is the
    protection that actually matters and is not configurable."""
    try:
        return max(1, int(load_settings().get("kopialens", {}).get("remove_plan_cap",
                                                                   DEFAULT_REMOVE_PLAN_CAP)))
    except Exception:  # noqa: BLE001
        return DEFAULT_REMOVE_PLAN_CAP


def _remove_parts(parts: list[dict]) -> list[tuple[str | None, str]]:
    """Distinct (source, prefix) parts; a part inside another selected folder is dropped."""
    clean = []
    for part in parts or []:
        prefix = (part.get("prefix") or "").rstrip("\\/")
        if prefix:
            clean.append((part.get("source") or None, prefix))
    clean = list(dict.fromkeys(clean))

    def covers(outer, inner) -> bool:
        (osrc, opre), (isrc, ipre) = outer, inner
        if outer == inner or (osrc is not None and osrc != isrc):
            return False
        o, i = opre.lower(), ipre.lower()
        return i == o or i.startswith(o + _SEP) or i.startswith(o + "/")

    return [p for p in clean if not any(covers(o, p) for o in clean if o != p)]


def _remove_targets(p: RemovePlanRequest) -> dict:
    # Footgun guard: with NO constraining filter at all, this would plan a delete over the entire
    # repo. Require at least one real constraint before enumerating anything.
    parts = _remove_parts(p.parts) if p.parts is not None else None
    if not any([p.query, p.deleted_only, p.min_gb, p.over_gb, p.max_gb, p.source, p.ext,
                p.ext_exclude, p.date_from, p.date_to, p.first_from, p.min_versions,
                p.shared_min, p.prefix, parts]):
        raise HTTPException(status_code=400,
                            detail="refusing to plan a delete with no filter - this would match the entire repo")
    min_size = int((p.min_gb or p.over_gb or 0) * 1_000_000_000)
    common = dict(
        query=p.query, deleted_only=p.deleted_only, min_size=min_size,
        max_size=int((p.max_gb or 0) * 1_000_000_000), ext=p.ext or None,
        ext_exclude=p.ext_exclude or None, date_from=p.date_from or None, date_to=p.date_to or None,
        min_versions=p.min_versions, shared_min=p.shared_min, first_from=p.first_from or None,
        all_versions=p.all_versions,
    )
    if parts is None:
        return remove_targets(p.repo, source=p.source or None, prefix=p.prefix or None,
                              cap=p.cap or _plan_cap(), **common)
    return _merge_targets([remove_targets(p.repo, source=src, prefix=prefix, cap=_plan_cap(), **common)
                           for src, prefix in parts])


def _merge_targets(results: list[dict]) -> dict:
    """One plan from several folder/file parts. Every part is taken whole (no top-N), and the
    combined set must fit the plan cap."""
    cap = _plan_cap()
    if any(t["capped"] for t in results) or sum(t["file_count"] for t in results) > cap:
        total = sum(t["total_file_count"] for t in results)
        raise HTTPException(status_code=400, detail=(
            f"{total:,} files is more than one delete takes ({cap:,}). Delete fewer folders at once."))
    seen_pairs: set[tuple[str, str]] = set()
    files, pairs = [], []
    for t in results:
        for pair in t["pairs"]:
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                pairs.append(pair)
        files.extend(t["files"])
    files = sorted({(f["source"], f["full_path"]): f for f in files}.values(),
                   key=lambda f: int(f.get("physical") or 0), reverse=True)
    oids = list(dict.fromkeys(o for t in results for o in t["object_ids"]))
    names = list(dict.fromkeys(n for t in results for n in t["names"]))
    return {
        "file_count": len(pairs),
        "total_file_count": len(pairs),
        "total_physical": sum(t["total_physical"] for t in results),
        "total_logical": sum(t["total_logical"] for t in results),
        "capped": False,
        "logical": sum(t["logical"] for t in results),
        "physical": sum(t["physical"] for t in results),
        "object_ids": oids,
        "names": names,
        "pairs": pairs,
        "files": files[:200],
        "sources": sorted({s for t in results for s in t["sources"]}),
    }


def _remove_scope(everywhere: bool, matched_sources: list[str]):
    """Scope = the sources that OWN the matched paths (NOT sources_for_content, which over a folder
    of mixed small files drags in every source via ubiquitous deduped content). ``everywhere`` is
    the explicit, warned whole-repo sweep (--source dropped)."""
    return None if everywhere else sorted(set(matched_sources))


def _filename_matches(repo: str, scope: list[str] | None, names: list[str], cap: int,
                      biggest_first: bool = False) -> dict:
    """Every file ``--filename`` removes: all same-named files in the scoped backups, or in every
    backup when ``scope`` is None (kopia then drops --source)."""
    if scope is None:
        from ..core.sources import sources_index
        scope = sorted(sources_index(repo))
    return paths_named(repo, scope, names, cap=cap, biggest_first=biggest_first)


def _remove_items(t: dict, by: str) -> tuple[list[str], str]:
    """Return (items, flag) for the chosen mechanism: object-ids (exact content) or basenames."""
    return (t["names"], "--filename") if by == "filename" else (t["object_ids"], "--object-id")


@router.post("/actions/remove-plan")
def remove_plan(payload: RemovePlanRequest) -> dict:
    """Preview the chunked delete for the matched set: counts, scope sources, batch count, the
    biggest-file preview, and the full dry-run + commit command text (copy-pasteable). Nothing runs."""
    t = _remove_targets(payload)
    items, flag = _remove_items(t, payload.by)
    if not items:
        return {"file_count": 0, "object_count": 0, "name_count": 0, "item_count": 0, "batches": 0,
                "commit": "", "dry_run": "", "preview": [], "sources_scope": [], "everywhere": False,
                "logical": 0, "physical": 0, "all_versions": payload.all_versions, "by": payload.by,
                "sources_matched": [], "total_file_count": 0, "total_physical": 0,
                "total_logical": 0, "capped": False,
                "filename_match_total": None, "filename_extra_count": 0, "filename_extra": [],
                "filename_extra_capped": False, "filename_extra_logical": 0,
                "filename_extra_physical": 0}
    scope = _remove_scope(payload.everywhere, t["sources"])
    generation = index_generation(payload.repo)
    plan_token = _remember_plan({"items": items, "flag": flag, "source": scope, "by": payload.by,
                                 "repo": payload.repo, "generation": generation})
    # 8k per command so the copied lines are pasteable into a terminal (cmd.exe line cap ~8191).
    # The background executor uses larger 32k batches (fewer kopia invocations).
    commit_cmds = remove_files_chunked_commands(payload.repo, items, source=scope, commit=True, max_chars=8000, flag=flag)
    dry_cmds = remove_files_chunked_commands(payload.repo, items, source=scope, commit=False, max_chars=8000, flag=flag)
    audit.audit("generated", "remove-plan", repo=payload.repo,
                summary={"items": len(items), "by": payload.by, "batches": len(commit_cmds),
                         "sources": scope or [], "everywhere": scope is None,
                         "query": payload.query, "first_from": payload.first_from})
    # ``--filename`` is a basename match across the whole scoped source, so a folder plan
    # also removes same-named files OUTSIDE it. These fields say how much rides along, and which.
    extra: dict = {}
    if payload.by == "filename" and t["names"]:
        found = _filename_matches(payload.repo, scope, t["names"], _FILENAME_SAMPLE)
        planned = {(s, fp) for s, fp in t["pairs"]}
        rows = [{**r} for r in found["rows"] if (r["source"], r["full_path"]) not in planned]
        extra = {
            "filename_match_total": int(found["total"]),
            "filename_extra_count": max(0, int(found["total"]) - len(planned)),
            "filename_extra": rows,
            "filename_extra_capped": int(found["total"]) > len(found["rows"]),
            # every planned file is itself a match, so the extras are the match totals minus the plan's
            "filename_extra_logical": max(0, int(found["logical"]) - int(t["logical"] or 0)),
            "filename_extra_physical": max(0, int(found["physical"]) - int(t["physical"] or 0)),
        }
    return {
        "file_count": t["file_count"], "object_count": len(t["object_ids"]), "name_count": len(t["names"]),
        "item_count": len(items), "by": payload.by,
        "logical": t["logical"], "physical": t["physical"],
        # Uncapped totals for the same filter, so a top-N delete can show what share it covers
        # ("these 100 files are 70% of the folder").
        "total_file_count": t["total_file_count"], "total_physical": t["total_physical"],
        "total_logical": t["total_logical"], "capped": t["capped"],
        "sources_scope": scope or [], "everywhere": scope is None,
        "sources_matched": t["sources"], "batches": len(commit_cmds),
        "preview": t["files"], "all_versions": payload.all_versions,
        "commit": "\n".join(_fmt_cmd(c) for c in commit_cmds),
        "dry_run": "\n".join(_fmt_cmd(c) for c in dry_cmds),
        "filename_match_total": None, "filename_extra_count": 0, "filename_extra": [],
        "filename_extra_capped": False, "filename_extra_logical": 0, "filename_extra_physical": 0,
        **extra,
        # the job start passes plan_token back; index_generation is what it compares.
        "plan_token": plan_token, "index_generation": generation,
    }


# Everything on a remove plan that narrows the match to a SUBSET of the folder. folder_detail knows
# only about a prefix, so if any of these is set its whole-folder answer describes a different
# deletion than the one being planned.
_NARROWING_FILTERS = ("query", "ext", "ext_exclude", "deleted_only", "over_gb", "min_gb", "max_gb",
                      "min_versions", "shared_min", "date_from", "date_to", "first_from")


def _has_filters(payload: RemovePlanRequest) -> bool:
    return any(getattr(payload, name, None) for name in _NARROWING_FILTERS)


_DETAIL_LIMIT = 5000


def _filename_detail(payload: RemovePlanRequest) -> dict:
    """A file-name delete removes every same-named file in the scoped backups, not only the plan's
    own set, and every version of each (kopia rewrites every snapshot), so the detail prices that
    whole set across all versions, biggest first."""
    t = _remove_targets(payload)
    if not t["names"]:
        return {"found": False}
    found = _filename_matches(payload.repo, _remove_scope(payload.everywhere, t["sources"]),
                              t["names"], _DETAIL_LIMIT, biggest_first=True)
    if not found["rows"]:
        return {"found": False}
    out = batch_detail(payload.repo, [{"source": r["source"], "full_path": r["full_path"]}
                                      for r in found["rows"]], all_versions=True)
    out["capped"] = found["total"] > len(found["rows"])
    out["scope"] = "filename"
    return out


@router.post("/actions/remove-detail")
def remove_detail(payload: RemovePlanRequest) -> dict:
    """On-demand reclaim/freeable/shared for the matched set (a chunk walk, so it's a button, not
    auto). Folder under one source -> folder_detail (subtree walk); otherwise -> batch_detail over
    the matched files (capped). reads the cheap summary - totals + distinct sources + the
    top rows - never the full top-200k materialization. The detail walk describes the PLAN's set:
    its limit is min(cap, 5000), so a "Largest 100" plan is detailed over those 100 files, not the
    top 5,000 of the whole match."""
    if payload.by == "filename":
        return _filename_detail(payload)
    if payload.parts is not None:
        t = _remove_targets(payload)
        if not t["pairs"]:
            return {"found": False}
        pairs = t["pairs"][:5000]
        out = batch_detail(payload.repo, [{"source": s, "full_path": fp} for s, fp in pairs],
                           all_versions=payload.all_versions)
        out["capped"] = len(t["pairs"]) > len(pairs)
        out["scope"] = "selection"
        return out
    if not any([payload.query, payload.deleted_only, payload.min_gb, payload.over_gb, payload.max_gb,
                payload.source, payload.ext, payload.ext_exclude, payload.date_from, payload.date_to,
                payload.first_from, payload.min_versions, payload.shared_min, payload.prefix]):
        raise HTTPException(status_code=400,
                            detail="refusing to plan a delete with no filter - this would match the entire repo")
    cap = payload.cap or _plan_cap()
    t = remove_targets_summary(
        payload.repo, query=payload.query, deleted_only=payload.deleted_only,
        min_size=int((payload.min_gb or payload.over_gb or 0) * 1_000_000_000),
        max_size=int((payload.max_gb or 0) * 1_000_000_000), source=payload.source or None,
        ext=payload.ext or None, ext_exclude=payload.ext_exclude or None,
        date_from=payload.date_from or None, date_to=payload.date_to or None,
        min_versions=payload.min_versions, shared_min=payload.shared_min,
        first_from=payload.first_from or None, prefix=payload.prefix or None,
        cap=cap, detail_limit=min(cap, 5000),
    )
    if not t["pairs"]:
        return {"found": False}
    # The whole-folder shortcut is only valid when the plan really is the whole folder. With a
    # top-N cap ("Largest 100") the matched set is a slice, and folder_detail would report the
    # entire folder's freeable/shared - numbers with nothing to do with what is about to be
    # deleted, in exactly the workflow the cap exists for. The same applies to ANY active filter:
    # folder_detail takes a prefix and nothing else, so "delete files over 1 GB in here" was being
    # priced as "delete this entire folder".
    if payload.prefix and len(t["sources"]) == 1 and not t.get("capped") and not _has_filters(payload):
        out = folder_detail(payload.repo, t["sources"][0], payload.prefix,
                            all_versions=payload.all_versions)
        out["scope"] = "folder"
        return out
    out = batch_detail(payload.repo, [{"source": s, "full_path": fp} for s, fp in t["pairs"]],
                       all_versions=payload.all_versions)
    out["capped"] = t["total_file_count"] > len(t["pairs"])
    out["scope"] = "selection"
    return out


@router.post("/actions/remove-bat")
def remove_bat(payload: RemovePlanRequest) -> dict:
    """Write the chunked commit commands to a timestamped .bat (each batch logs kopia's output to a
    .log next to it) so the user can run + monitor it themselves. Returns the file paths."""
    t = _remove_targets(payload)
    items, flag = _remove_items(t, payload.by)
    if not items:
        raise HTTPException(status_code=404, detail="no matching files")
    scope = _remove_scope(payload.everywhere, t["sources"])
    cmds = remove_files_chunked_commands(payload.repo, items, source=scope, commit=True, max_chars=8000, flag=flag)
    ts = time.strftime("%Y%m%d_%H%M%S")
    fname = f"kopialens_remove_repo{payload.repo}_{ts}.bat"
    content = remove_files_bat_text(cmds, f"kopialens_remove_repo{payload.repo}_{ts}.log")
    audit.audit("generated", "remove-bat", repo=payload.repo,
                summary={"items": len(items), "batches": len(cmds), "filename": fname,
                         "sources": scope or [], "everywhere": scope is None})
    return {"filename": fname, "content": content, "batches": len(cmds), "items": len(items)}


@router.post("/actions/remove-plan-dry-run")
def remove_plan_dry_run(payload: RemovePlanRequest) -> dict:
    """RUN the chunked plan's dry run (remove-files without --commit) over the matched set.
    Synchronous; output capped, full text logged to data/logs."""
    t = _remove_targets(payload)
    items, flag = _remove_items(t, payload.by)
    if not items:
        raise HTTPException(status_code=404, detail="no matching files")
    scope = _remove_scope(payload.everywhere, t["sources"])
    if flag == "--filename":
        r = remove_files_dry_run(payload.repo, filenames=items, source=scope)
    else:
        r = remove_files_dry_run(payload.repo, object_ids=items, source=scope)
    return _dry_response(r)


@router.post("/actions/snapshot-delete-dry-run")
def snapshot_delete_dry_run_route(payload: SnapshotDeleteRequest) -> dict:
    """RUN snapshot delete without --delete: kopia lists exactly what it would delete.
    Synchronous (spinner); the shared job runner streams it later."""
    if not payload.snapshot_ids:
        raise HTTPException(status_code=400, detail="snapshot_ids is empty")
    return _dry_response(snapshot_delete_dry_run(payload.repo, payload.snapshot_ids))


@router.post("/actions/browse-folder")
def browse_folder(payload: dict) -> dict:
    """Open the native Windows folder picker and return the chosen path (server is local)."""
    return {"path": pick_folder(payload.get("initial"))}


@router.post("/actions/folder-reclaim")
def folder_reclaim(payload: FolderReclaimRequest) -> dict:
    """Subtree reclaim (logical / physical / freeable) for a folder - an on-demand walk."""
    try:
        return folder_detail(payload.repo, payload.source, payload.prefix)
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class BatchDetailRequest(BaseModel):
    repo: str
    files: list[dict]  # [{"source":..., "full_path":...}, ...]
    all_versions: bool = True  # must mirror the caller's delete plan, or the preview prices a
                               # different deletion than the one about to run


@router.post("/actions/batch-detail")
def batch_detail_route(payload: BatchDetailRequest) -> dict:
    """Union (content-aware/deduped) footprint for a multi-file selection - the multi-file
    equivalent of /search/detail. Can be slow the first time on a big selection (walks each
    file's chunks), then served from the warm dir cache."""
    if not payload.files:
        raise HTTPException(status_code=400, detail="no files specified")
    try:
        return batch_detail(payload.repo, payload.files, all_versions=payload.all_versions)
    except PASS_THROUGH:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/actions/compact")
def compact(payload: dict) -> dict:
    """No longer applicable. Kept so an old client gets an explanation rather than a 404.

    Compaction existed because DuckDB never reclaims space on delete, so the index file only ever
    grew. The index is now immutable Parquet: a source's artifact is rewritten whole and the space
    from the old one is returned when the generation that referenced it is collected. There is
    nothing to compact.
    """
    return {"ok": True, "compacted": False, "repo": payload.get("repo", "1"),
            "reason": "The index no longer needs compaction. Space is reclaimed automatically."}


@router.get("/actions/mounts")
def mounts() -> dict:
    return {"mounts": list_mounts()}


@router.post("/actions/unmount")
def do_unmount(payload: UnmountRequest) -> dict:
    return {"ok": unmount(payload.pid)}


@router.post("/actions/unmount-all")
def do_unmount_all() -> dict:
    from ..core import mounts
    return {"unmounted": mounts.unmount_all()}


@router.post("/actions/mount-browse")
def mount_browse(payload: UnmountRequest) -> dict:
    """Open Explorer on a running mount (at the path it was opened for, else its root)."""
    from ..core import mounts
    rec = mounts.get(payload.pid)
    if not rec:
        raise HTTPException(status_code=404, detail="That mount is no longer running.")
    target = rec.get("open") or rec.get("mountpoint")
    open_in_explorer(target)
    return {"opening": target}


@router.get("/actions/purge-plan")
def purge_plan(repo: str, source: str | None = None, full_path: str | None = None) -> dict:
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    held = snapshots_for_path(repo, source, full_path)
    snapshot_ids = [r["snapshot_id"] for r in held]
    items = [{"id": r["snapshot_id"], "retention": r["retention"], "pinned": r["pinned"],
              "start_time": r["start_time"]} for r in held]
    return {
        "repo": repo,
        "source": source,
        "full_path": full_path,
        "snapshot_ids": snapshot_ids,
        "items": items,  # per-snapshot retention/pin status (don't purge a kept one)
        "commands": snapshot_delete_commands(repo, snapshot_ids),
        "executes": False,
    }
