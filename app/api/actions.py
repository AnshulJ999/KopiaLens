"""Operational actions.

Destructive actions are preview-then-execute: a dry run (or, where kopia has none, a
preview) runs first and the execute path is confirmed in-app before anything mutates.
"""

from __future__ import annotations

import csv
import io
import os
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..core import audit
from ..core import paths as pathrules
from ..core.applog import get_logger
from ..core.kopia import (
    MountError,
    docker_exec,
    list_mounts,
    maintenance_run_command,
    mount_and_browse,
    mount_snapshot,
    open_in_explorer,
    PickerBusy,
    pick_folder,
    remove_files_script,
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
from ..index.physical import batch_detail, copies_outside, folder_detail
from ..index.queries import paths_named, remove_targets, remove_targets_summary
from ..licensing import features
from ..settings import load_settings

router = APIRouter(tags=["actions"])
log = get_logger("actions")

FULL_LIST_CAP = 1_000_000   # the most rows any "Show all" list returns
_FILENAME_SAMPLE = 500   # collateral paths listed for a filename-mode plan (the count is exact)
_PLAN_PREVIEW = 1000     # files a plan lists in the panel; the full list is the CSV download

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


def _note_plan(token: str | None, **facts) -> None:
    """Add what the preview found later (sample paths, what goes along) to a stored plan."""
    with _PLANS_LOCK:
        if token and token in _PLANS:
            _PLANS[token].update(facts)


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
    return " ".join(f'"{a}"' if (not a or " " in a) else a for a in docker_exec() + cmd)


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
    from ..core import host, mounts as mount_registry
    mountpoint = payload.mountpoint or mount_registry.auto_mountpoint(payload.repo, payload.snapshot_id)
    if not mountpoint:
        raise HTTPException(status_code=409, detail="no free drive letter for an auto mount; unmount something first")
    in_mount_target = None
    if payload.full_path:
        sep = pathrules.sep(payload.source)
        root = _source_root(payload.source).rstrip(sep)
        rel = payload.full_path[len(root):] if payload.full_path.startswith(root) else ""
        mount_root = mountpoint if mountpoint.endswith(("\\", "/")) else mountpoint + os.sep
        in_mount_target = os.path.join(mount_root, *[s for s in rel.split(sep) if s])
    try:
        proc = mount_and_browse(payload.repo, payload.snapshot_id, mountpoint, in_mount_target=in_mount_target,
                                info={**_snapshot_info(payload.repo, payload.snapshot_id, payload.source),
                                      "path": payload.full_path or ""})
        return {"pid": proc.pid, "command": proc.args, "mountpoint": mountpoint,
                "shown": host.shown_path(mountpoint), "opening": in_mount_target}
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
    stem = f"kopialens_remove_repo{repo}_{time.strftime('%Y%m%d_%H%M%S')}"
    ext, script = remove_files_script(commit_cmds, stem)
    audit.audit("generated", "remove-files-commands", repo=repo,
                summary={"items": len(items), "by": by, "batches": len(commit_cmds),
                         "sources": scope or [], "everywhere": scope is None})
    return {
        "executed": False, "by": by, "count": len(items), "everywhere": scope is None,
        "sources": scope or [], "batches": len(commit_cmds),
        "dry_run": "\n".join(_fmt_cmd(c) for c in dry_cmds),
        "commit": "\n".join(_fmt_cmd(c) for c in commit_cmds),
        "bat_name": stem + ext,
        "bat": script,
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
    features.require("remove", payload.repo)  # licence check: do not remove or bypass (KopiaLens License)
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
    features.require("remove", payload.repo)  # licence check: do not remove or bypass (KopiaLens License)
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
    extra_cap: int | None = None    # rows of the same-name / identical-copy lists ("Show all" asks for every one)


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
        raw = part.get("prefix") or ""
        base, sep = pathrules.folder_key(raw)
        if sep is not None:
            clean.append((part.get("source") or None, base or raw))
    clean = list(dict.fromkeys(clean))

    def covers(outer, inner) -> bool:
        (osrc, opre), (isrc, ipre) = outer, inner
        if outer == inner or (osrc is not None and osrc != isrc):
            return False
        sep = pathrules.path_sep(opre)
        if pathrules.path_sep(ipre) != sep:
            return False
        o, i = (opre.lower(), ipre.lower()) if sep == pathrules.WIN_SEP else (opre, ipre)
        o = pathrules.folder_key(o)[0]
        return i == o or i.startswith(o + sep)

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
    pairs = list(dict.fromkeys(pair for t in results for pair in t["pairs"]))
    # One file per (host, path): the same file reached through two parts merges its sources.
    merged: dict[tuple, dict] = {}
    for t in results:
        for f in t["files"]:
            key = (f.get("host"), f["full_path"])
            have = merged.get(key)
            if have is None:
                merged[key] = dict(f)
            else:
                have["sources"] = sorted(set(have["sources"]) | set(f["sources"]))
                have["source"] = have["sources"][0]
                have["size"] = max(int(have.get("size") or 0), int(f.get("size") or 0))
                have["physical"] = max(int(have.get("physical") or 0), int(f.get("physical") or 0))
    files = sorted(merged.values(), key=lambda f: int(f.get("physical") or 0), reverse=True)
    oids = list(dict.fromkeys(o for t in results for o in t["object_ids"]))
    names = list(dict.fromkeys(n for t in results for n in t["names"]))
    logical = sum(int(f.get("size") or 0) for f in files)
    physical = sum(int(f.get("physical") or 0) for f in files)
    return {
        "file_count": len(files),
        "total_file_count": len(files),
        "total_physical": physical,
        "total_logical": logical,
        "capped": False,
        "logical": logical,
        "physical": physical,
        "object_ids": oids,
        "names": names,
        "pairs": pairs,
        "files": files,
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


def _plan_prefixes(p: RemovePlanRequest) -> list[str]:
    if p.parts is not None:
        return [x.get("prefix") or "" for x in p.parts]
    return [p.prefix] if p.prefix else []


def _plan_breakdown(files: list[dict], prefixes: list[str]) -> dict:
    """The plan's files grouped three ways for the panel: by folder one level below each selected
    folder (files directly inside count as the folder itself), by file type, and by source."""
    bases = []
    for pre in prefixes:
        base, sep = pathrules.folder_key(pre)
        if sep is not None:
            bases.append((base, sep, sep == pathrules.WIN_SEP))
    groups: dict[str, dict] = {"folder": {}, "ext": {}, "source": {}}

    def add(kind: str, key: str, f: dict) -> None:
        e = groups[kind].setdefault(key, [0, 0, 0])
        e[0] += 1
        e[1] += int(f.get("size") or 0)
        e[2] += int(f.get("physical") or 0)

    direct = set()
    for f in files:
        fp = f["full_path"]
        folder = None
        for base, sep, fold in bases:
            a, b = (fp.lower(), base.lower()) if fold else (fp, base)
            if a.startswith(b + sep):
                rest = fp[len(base) + 1:]
                cut = rest.find(sep)
                folder = base + sep + rest[:cut] if cut > 0 else base
                if cut <= 0:
                    direct.add(base)
                break
        if folder is None:
            s = pathrules.path_sep(fp)
            cut = fp.rfind(s)
            folder = fp[:cut] if cut > 0 else fp
        add("folder", folder, f)
        add("ext", (f.get("ext") or "").lower(), f)
        for src in f.get("sources") or [f["source"]]:
            add("source", src, f)

    def rows(kind: str, n: int) -> list[dict]:
        top = sorted(groups[kind].items(), key=lambda kv: (-kv[1][2], -kv[1][1], kv[0]))[:n]
        out = [{kind: k, "files": v[0], "size": v[1], "physical": v[2]} for k, v in top]
        if kind == "folder":
            for r in out:
                r["direct"] = r["folder"] in direct
        return out

    return {"folders": rows("folder", 200), "types": rows("ext", 100), "sources": rows("source", 500),
            "folder_count": len(groups["folder"]), "type_count": len(groups["ext"])}


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
                "filename_extra_physical": 0, "breakdown": _plan_breakdown([], [])}
    scope = _remove_scope(payload.everywhere, t["sources"])
    generation = index_generation(payload.repo)
    # Free installs see the whole preview, but never the commands or ids that rebuild the delete.
    pro = features.allowed("remove", payload.repo)  # licence check: do not remove or bypass (KopiaLens License)
    plan_token = _remember_plan({"items": items, "flag": flag, "source": scope, "by": payload.by,
                                 "repo": payload.repo, "generation": generation,
                                 "file_count": t["file_count"], "physical": t["physical"],
                                 "logical": t["logical"]}) if pro else None
    # 8k per command so the copied lines are pasteable into a terminal (cmd.exe line cap ~8191).
    # The background executor uses larger 32k batches (fewer kopia invocations).
    commit_cmds = remove_files_chunked_commands(payload.repo, items, source=scope, commit=True, max_chars=8000, flag=flag)
    dry_cmds = remove_files_chunked_commands(payload.repo, items, source=scope, commit=False, max_chars=8000, flag=flag)
    if payload.extra_cap is None:   # a "Show all" re-read is not a new plan
        audit.audit("generated", "remove-plan", repo=payload.repo,
                    summary={"items": len(items), "by": payload.by, "batches": len(commit_cmds),
                             "sources": scope or [], "everywhere": scope is None,
                             "query": payload.query, "first_from": payload.first_from})
    extra_cap = min(max(payload.extra_cap or _FILENAME_SAMPLE, 1), FULL_LIST_CAP)
    # ``--filename`` is a basename match across the whole scoped source, so a folder plan
    # also removes same-named files OUTSIDE it. These fields say how much rides along, and which.
    extra: dict = {}
    if payload.by == "filename" and t["names"]:
        found = _filename_matches(payload.repo, scope, t["names"], extra_cap)
        planned = set(t["pairs"])
        rows = [{**r} for r in found["rows"]
                if not any((s, r["full_path"]) in planned for s in r["sources"])]
        extra = {
            "filename_match_total": int(found["total"]),
            "filename_extra_count": max(0, int(found["total"]) - len(t["files"])),
            "filename_extra": rows,
            "filename_extra_capped": int(found["total"]) > len(found["rows"]),
            # every planned file is itself a match, so the extras are the match totals minus the plan's
            "filename_extra_logical": max(0, int(found["logical"]) - int(t["logical"] or 0)),
            "filename_extra_physical": max(0, int(found["physical"]) - int(t["physical"] or 0)),
        }
    elif t["pairs"]:
        # [DEL7 F8] ``--object-id`` removes that content at every path in the scoped sources, so
        # identical copies outside the plan lose those versions too.
        copies = copies_outside(payload.repo, t["pairs"], sources=scope, cap=extra_cap,
                                all_versions=payload.all_versions)
        extra = {"content_extra_count": copies["total"], "content_extra": copies["rows"],
                 "content_extra_capped": copies["total"] > len(copies["rows"]),
                 "content_extra_logical": copies["logical"], "content_extra_physical": copies["physical"]}
    _note_plan(plan_token, sample=[f["full_path"] for f in t["files"][:10]],
               filename_extra_count=extra.get("filename_extra_count", 0),
               content_extra_count=extra.get("content_extra_count", 0))
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
        "preview": (t["files"][:_PLAN_PREVIEW] if pro else
                    [{k: v for k, v in f.items() if k != "content_id"} for f in t["files"][:_PLAN_PREVIEW]]),
        "breakdown": _plan_breakdown(t["files"], _plan_prefixes(payload)),
        "all_versions": payload.all_versions,
        "commit": "\n".join(_fmt_cmd(c) for c in commit_cmds) if pro else "",
        "dry_run": "\n".join(_fmt_cmd(c) for c in dry_cmds) if pro else "",
        "filename_match_total": None, "filename_extra_count": 0, "filename_extra": [],
        "filename_extra_capped": False, "filename_extra_logical": 0, "filename_extra_physical": 0,
        "content_extra_count": 0, "content_extra": [], "content_extra_capped": False,
        "content_extra_logical": 0, "content_extra_physical": 0,
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


def _filters(p: RemovePlanRequest) -> dict:
    """The plan's file filters, as ``remove_targets`` / ``remove_targets_summary`` keywords."""
    return dict(query=p.query, deleted_only=p.deleted_only,
                min_size=int((p.min_gb or p.over_gb or 0) * 1_000_000_000),
                max_size=int((p.max_gb or 0) * 1_000_000_000), ext=p.ext or None,
                ext_exclude=p.ext_exclude or None, date_from=p.date_from or None,
                date_to=p.date_to or None, min_versions=p.min_versions, shared_min=p.shared_min,
                first_from=p.first_from or None)


def _filename_detail(payload: RemovePlanRequest) -> dict:
    """A file-name delete removes every same-named file in the scoped backups, not only the plan's
    own set, and every version of each (kopia rewrites every snapshot), so the detail prices that
    whole set across all versions. It removes those paths and nothing else: an identical file under
    another name keeps its data, so those pieces stay () - listed as ``kept_files``."""
    t = _remove_targets(payload)
    if not t["names"]:
        return {"found": False}
    found = _filename_matches(payload.repo, _remove_scope(payload.everywhere, t["sources"]),
                              t["names"], _plan_cap(), biggest_first=True)
    if not found["rows"]:
        return {"found": False}
    pairs = [(s, r["full_path"]) for r in found["rows"] for s in r["sources"]]
    out = batch_detail(payload.repo, [{"source": s, "full_path": fp} for s, fp in pairs],
                       all_versions=True, keep_copies=True)
    kept = copies_outside(payload.repo, pairs, cap=_FILENAME_SAMPLE)
    out.update({"capped": found["total"] > len(found["rows"]), "scope": "filename",
                "kept_files": kept["rows"], "kept_count": kept["total"]})
    return out


def _remove_detail(payload: RemovePlanRequest) -> dict:
    if payload.by == "filename":
        return _filename_detail(payload)
    if payload.parts is not None:
        # the light read per part: totals and the files' pairs, never the full row set.
        parts = _remove_parts(payload.parts)
        if not parts:
            return {"found": False}
        cap = _plan_cap()
        sums = [remove_targets_summary(payload.repo, source=src, prefix=prefix, cap=cap,
                                       detail_limit=cap, **_filters(payload)) for src, prefix in parts]
        if any(s_["capped"] for s_ in sums):
            raise HTTPException(status_code=400, detail=(
                f"{sum(s_['total_file_count'] for s_ in sums):,} files is more than one delete takes "
                f"({cap:,}). Delete fewer folders at once."))
        pairs = list(dict.fromkeys(pr for s_ in sums for pr in s_["pairs"]))
        if not pairs:
            return {"found": False}
        out = batch_detail(payload.repo, [{"source": s, "full_path": fp} for s, fp in pairs],
                           all_versions=payload.all_versions)
        out["capped"] = False
        out["scope"] = "selection"
        return out
    if not any([payload.query, payload.deleted_only, payload.min_gb, payload.over_gb, payload.max_gb,
                payload.source, payload.ext, payload.ext_exclude, payload.date_from, payload.date_to,
                payload.first_from, payload.min_versions, payload.shared_min, payload.prefix]):
        raise HTTPException(status_code=400,
                            detail="refusing to plan a delete with no filter - this would match the entire repo")
    cap = payload.cap or _plan_cap()
    t = remove_targets_summary(payload.repo, source=payload.source or None,
                               prefix=payload.prefix or None, cap=cap, detail_limit=cap,
                               **_filters(payload))
    if not t["pairs"]:
        return {"found": False}
    # The whole-folder shortcut is only valid when the plan really is the whole folder. With a
    # top-N cap ("Largest 100") the matched set is a slice, and with any filter it is a subset;
    # either way folder_detail would price a different deletion than the one being planned.
    if payload.prefix and len(t["sources"]) == 1 and not t.get("capped") and not _has_filters(payload):
        out = folder_detail(payload.repo, t["sources"][0], payload.prefix,
                            all_versions=payload.all_versions)
        out["scope"] = "folder"
        return out
    out = batch_detail(payload.repo, [{"source": s, "full_path": fp} for s, fp in t["pairs"]],
                       all_versions=payload.all_versions)
    out["capped"] = False             # every file of the plan is priced (a top-N plan: those N)
    out["scope"] = "selection"
    return out


@router.post("/actions/remove-detail")
def remove_detail(payload: RemovePlanRequest) -> dict:
    """On-demand reclaim/freeable/shared for the matched set (a button, not auto). A whole folder
    under one source -> folder_detail; otherwise -> batch_detail over every matched file (a top-N
    plan: those N). ``alt_freeable`` is the same plan's Reclaimable with the other match method
    (exact content vs file name), for the side-by-side in the match switch."""
    out = _remove_detail(payload)
    if out.get("found") is not False:
        other = "filename" if payload.by != "filename" else "object-id"
        try:
            alt = _remove_detail(payload.model_copy(update={"by": other}))
            out["alt_freeable"] = alt.get("freeable") if alt.get("found") is not False else None
        except Exception as exc:  # noqa: BLE001 - the side-by-side figure is extra; the plan's own stands
            log.warning("remove-detail: the %s figure could not be worked out: %s", other, exc)
            out["alt_freeable"] = None
    return out


@router.post("/actions/remove-bat")
def remove_bat(payload: RemovePlanRequest) -> dict:
    """Write the chunked commit commands to a timestamped .bat (each batch logs kopia's output to a
    .log next to it) so the user can run + monitor it themselves. Returns the file paths."""
    features.require("remove", payload.repo)  # licence check: do not remove or bypass (KopiaLens License)
    t = _remove_targets(payload)
    items, flag = _remove_items(t, payload.by)
    if not items:
        raise HTTPException(status_code=404, detail="no matching files")
    scope = _remove_scope(payload.everywhere, t["sources"])
    cmds = remove_files_chunked_commands(payload.repo, items, source=scope, commit=True, max_chars=8000, flag=flag)
    stem = f"kopialens_remove_repo{payload.repo}_{time.strftime('%Y%m%d_%H%M%S')}"
    ext, content = remove_files_script(cmds, stem)
    fname = stem + ext
    audit.audit("generated", "remove-bat", repo=payload.repo,
                summary={"items": len(items), "batches": len(cmds), "filename": fname,
                         "sources": scope or [], "everywhere": scope is None})
    return {"filename": fname, "content": content, "batches": len(cmds), "items": len(items)}


@router.post("/actions/remove-list")
def remove_list(payload: RemovePlanRequest) -> dict:
    """Every file the plan removes, as CSV (Pro). A file-name plan also lists the same-named files
    outside the selection, since kopia removes those too."""
    features.require("remove", payload.repo)  # licence check: do not remove or bypass (KopiaLens License)
    t = _remove_targets(payload)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Sources", "Path", "Logical", "Physical", "Why"])
    for f in t["files"]:
        w.writerow(["; ".join(f["sources"]), f["full_path"], f.get("size") or 0, f.get("physical") or 0,
                    "selected"])
    rows = len(t["files"])
    if payload.by == "filename" and t["names"]:
        scope = _remove_scope(payload.everywhere, t["sources"])
        planned = set(t["pairs"])
        for r in _filename_matches(payload.repo, scope, t["names"], _plan_cap())["rows"]:
            if not any((s, r["full_path"]) in planned for s in r["sources"]):
                w.writerow(["; ".join(r["sources"]), r["full_path"], r.get("size") or 0,
                            r.get("physical") or 0, "same file name"])
                rows += 1
    fname = f"kopialens_delete_list_repo{payload.repo}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    return {"filename": fname, "content": buf.getvalue(), "rows": rows}


@router.post("/actions/remove-plan-dry-run")
def remove_plan_dry_run(payload: RemovePlanRequest) -> dict:
    """RUN the chunked plan's dry run (remove-files without --commit) over the matched set.
    Synchronous; output capped, full text logged to data/logs."""
    features.require("remove", payload.repo)  # licence check: do not remove or bypass (KopiaLens License)
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
def browse_folder(payload: dict, request: Request) -> dict:
    """Open the Windows folder dialog on this PC and return the chosen path. Only for a browser on
    this PC: anywhere else the dialog would open on a screen nobody is looking at."""
    from ..core import host
    if host.folder_picker(request.client.host if request.client else None) != "native":
        raise HTTPException(status_code=409, detail="Browse… shows the folder list here. The Windows dialog opens only when it's chosen under Folder picker in Settings, and only for a browser on this PC.")
    try:
        return {"path": pick_folder(payload.get("initial"))}
    except PickerBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/actions/folders")
def folders(path: str = "") -> dict:
    """The in-app folder browser: the folders inside ``path`` on this machine, or the roots."""
    from ..core import host
    if not path:
        return {"path": "", "parent": "", "folders": host.folder_roots(), "truncated": False}
    try:
        return host.list_folders(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="That folder doesn't exist.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="KopiaLens isn't allowed to open that folder.") from exc


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
    # licence check: do not remove or bypass (KopiaLens License)
    pro = features.allowed("purge", repo)   # Free: which snapshots and when, without their ids
    snapshot_ids = [r["snapshot_id"] for r in held] if pro else []
    items = [{"id": r["snapshot_id"] if pro else "", "retention": r["retention"], "pinned": r["pinned"],
              "start_time": r["start_time"]} for r in held]
    return {
        "repo": repo,
        "source": source,
        "full_path": full_path,
        "snapshot_ids": snapshot_ids,
        "items": items,  # per-snapshot retention/pin status (don't purge a kept one)
        "commands": snapshot_delete_commands(repo, snapshot_ids) if pro else [],
        "executes": False,
    }
