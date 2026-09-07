"""Operational actions.

Restore and purge are command previews only. Mount is allowed to execute because
it is repo-read-only, but it is explicit and returns the spawned process id.
"""

from __future__ import annotations

import threading
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import audit
from ..core.kopia import (
    _REMOVE_JOBS,
    list_mounts,
    mount_and_browse,
    mount_snapshot,
    next_free_drive,
    pick_folder,
    remove_files_bat_text,
    remove_files_chunked_commands,
    remove_files_chunked_execute,
    remove_files_command,
    remove_files_execute,
    remove_log_path,
    maintenance_run_command,
    restore_command,
    restore_command_full,
    restore_execute,
    snapshot_delete_commands,
    snapshot_delete_execute,
    unmount,
)
from ..core.sources import invalidate as invalidate_sources, sources_index
from ..index.schema import RepoOffline
from ..core.nightly import start_index_pass_async
from ..index.physical import batch_detail, folder_detail
from ..index.queries import remove_targets, snapshots_for_path, sources_for_content, versions as file_versions
from ..settings import load_settings

router = APIRouter(tags=["actions"])

_SEP = chr(92)  # backslash


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


class RestorePreview(BaseModel):
    repo: str = "1"
    source: str
    destination: str
    overwrite: bool = False


class MountRequest(BaseModel):
    repo: str = "1"
    snapshot_id: str
    mountpoint: str


class MountOpenRequest(BaseModel):
    repo: str = "1"
    snapshot_id: str
    source: str
    full_path: str | None = None  # a file/folder inside the source; None = mount root only
    mountpoint: str | None = None  # auto-assigned free drive letter when omitted


class SnapshotDeleteRequest(BaseModel):
    repo: str = "1"
    snapshot_ids: list[str]
    confirm: bool = False


class RemoveFilesRequest(BaseModel):
    repo: str = "1"
    by: str = "object-id"   # "object-id" (precise content) | "filename" (by name)
    confirm: bool = False
    everywhere: bool = False  # object-id: drop --source to remove from ALL sources (sweeps whole repo)
    source: str | None = None      # single-file form
    full_path: str | None = None
    files: list[dict] | None = None  # batch form: [{"source":..., "full_path":...}]


class RestoreRequest(BaseModel):
    repo: str = "1"
    source_id: str                 # object id (file/dir/snapshot root), optionally with /sub/path
    target: str                    # destination folder or a .zip/.tar archive
    options: dict = {}
    confirm: bool = False


class FolderReclaimRequest(BaseModel):
    repo: str = "1"
    source: str
    prefix: str


class UnmountRequest(BaseModel):
    pid: int


@router.post("/actions/restore-preview")
def restore_preview(payload: RestorePreview) -> dict:
    audit.audit("generated", "restore-command", repo=payload.repo,
                summary={"source": payload.source, "destination": payload.destination})
    return {"command": restore_command(payload.repo, payload.source, payload.destination, overwrite=payload.overwrite)}


@router.post("/actions/mount")
def mount(payload: MountRequest) -> dict:
    try:
        proc = mount_snapshot(payload.repo, payload.snapshot_id, payload.mountpoint)
        return {"pid": proc.pid, "command": proc.args}
    except RepoOffline:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/actions/mount-open")
def mount_open(payload: MountOpenRequest) -> dict:
    """Mount a snapshot (auto drive letter if none given) and open Explorer at the file/folder
    inside it. Tracked in the mount manager like any other mount."""
    mountpoint = payload.mountpoint or next_free_drive()
    if not mountpoint:
        raise HTTPException(status_code=409, detail="no free drive letter for an auto mount; unmount something first")
    in_mount_target = None
    if payload.full_path:
        root = _source_root(payload.source).rstrip(_SEP)
        rel = payload.full_path[len(root):].lstrip(_SEP) if payload.full_path.startswith(root) else ""
        in_mount_target = (mountpoint + _SEP + rel) if rel else (mountpoint + _SEP)
    try:
        proc = mount_and_browse(payload.repo, payload.snapshot_id, mountpoint, in_mount_target=in_mount_target)
        return {"pid": proc.pid, "command": proc.args, "mountpoint": mountpoint, "opening": in_mount_target}
    except RepoOffline:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/actions/snapshot-delete")
def snapshot_delete(payload: SnapshotDeleteRequest) -> dict:
    """confirm=false -> return the exact delete commands (preview, nothing happens).
    confirm=true  -> actually delete, then reindex the affected sources so the UI is current.
    Space frees on the next maintenance run (kopia's own step)."""
    if not payload.snapshot_ids:
        raise HTTPException(status_code=400, detail="snapshot_ids is empty")
    if not payload.confirm:
        cmds = snapshot_delete_commands(payload.repo, payload.snapshot_ids, batch=True)
        text = "\n".join(" ".join(c) for c in cmds)
        maint = " ".join(maintenance_run_command(payload.repo))
        audit.audit("generated", "snapshot-delete-commands", repo=payload.repo,
                    summary={"ids": len(payload.snapshot_ids)})
        return {"executed": False, "count": len(payload.snapshot_ids),
                "commands": text + "\n\n# Then reclaim space with:\n" + maint}
    result = snapshot_delete_execute(payload.repo, payload.snapshot_ids)
    deleted = int(result.get("deleted") or 0)
    # The execute chunks at 200 ids per kopia call, so a later chunk can fail after earlier ones
    # succeeded. Raising first left the index describing snapshots that no longer exist until the
    # next nightly pass - refresh on ANY progress, then report the failure.
    if deleted:
        invalidate_sources(payload.repo)        # the snapshot set changed -> drop the cached listing
        try:
            _start_index_run(payload.repo)      # incremental reindex picks up the now-missing snapshots
        except Exception:  # noqa: BLE001 - reindex is best-effort; UI Refresh also recovers
            pass
    if result["returncode"] != 0:
        err = result["stderr"].strip() or "kopia snapshot delete failed"
        raise HTTPException(
            status_code=500,
            detail=(f"deleted {deleted} of {len(payload.snapshot_ids)} snapshots, then failed: {err}"
                    if deleted else err))
    return {"deleted": deleted, "command": result.get("command")}


def _rf_targets(payload: RemoveFilesRequest) -> list[tuple[str, str]]:
    if payload.files:
        return [(f["source"], f["full_path"]) for f in payload.files if f.get("source") and f.get("full_path")]
    if payload.source and payload.full_path:
        return [(payload.source, payload.full_path)]
    return []


def _post_remove(repo: str) -> None:
    invalidate_sources(repo)
    try:
        _start_index_run(repo)  # snapshots were rewritten -> reindex so the UI reflects reality
    except Exception:  # noqa: BLE001
        pass


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


@router.post("/actions/remove-files")
def remove_files(payload: RemoveFilesRequest) -> dict:
    """Remove file(s) from ALL snapshots (without deleting the snapshots), via
    `kopia snapshot fix remove-files`. Supports single or batch. by="object-id" removes the exact
    content everywhere (no --source); by="filename" is scoped per source. confirm=false returns
    dry-run + commit commands; confirm=true executes with --commit then reindexes."""
    targets = _rf_targets(payload)
    if not targets:
        raise HTTPException(status_code=400, detail="no files specified")

    # Distinct content objects across all targets' versions. Used both as the object-id set AND to
    # find EVERY source that references this content - because a deduped copy left in another source
    # keeps the content alive (GC won't free it). So we scope --source to all referencing sources:
    # complete (frees everywhere it lives) AND bounded (not the slow whole-repo scan). everywhere=true
    # drops --source for a full-repo sweep (the paranoid fallback for old-snapshot-only references).
    seen: set[str] = set()
    oids: list[str] = []
    for src, fp in targets:
        for v in file_versions(payload.repo, src, fp):
            oid = v.get("content_id")
            if oid and oid not in seen:
                seen.add(oid)
                oids.append(oid)
    scope = None if payload.everywhere else (sources_for_content(payload.repo, oids) or sorted({s for s, _ in targets}))

    if payload.by == "filename":
        names: list[str] = []
        for _src, fp in targets:
            name = fp.rsplit(_SEP, 1)[-1].rsplit("/", 1)[-1]
            if name not in names:
                names.append(name)
        if not payload.confirm:
            return _rf_preview(payload.repo, names, "--filename", scope, "filename")
        r = remove_files_execute(payload.repo, filenames=names, source=scope)
        if r["returncode"] != 0:
            raise HTTPException(status_code=500, detail=r["stderr"].strip() or "remove-files failed")
        _post_remove(payload.repo)
        return {"executed": True}

    # object-id (exact content)
    if not oids:
        raise HTTPException(status_code=404, detail="no content objects found")
    if not payload.confirm:
        return _rf_preview(payload.repo, oids, "--object-id", scope, "object-id")
    r = remove_files_execute(payload.repo, object_ids=oids, source=scope)
    if r["returncode"] != 0:
        raise HTTPException(status_code=500, detail=r["stderr"].strip() or "remove-files failed")
    _post_remove(payload.repo)
    return {"executed": True}


class RemovePlanRequest(BaseModel):
    """Chunked folder/filter delete: removes the files matching the CURRENT view/filter (optionally
    scoped to a folder ``prefix``), biggest-files-first, via chunked remove-files."""
    repo: str = "1"
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


def _remove_targets(p: RemovePlanRequest) -> dict:
    # Footgun guard: with NO constraining filter at all, this would plan a delete over the entire
    # repo. Require at least one real constraint before enumerating anything.
    if not any([p.query, p.deleted_only, p.min_gb, p.over_gb, p.max_gb, p.source, p.ext,
                p.ext_exclude, p.date_from, p.date_to, p.first_from, p.min_versions,
                p.shared_min, p.prefix]):
        raise HTTPException(status_code=400,
                            detail="refusing to plan a delete with no filter - this would match the entire repo")
    min_size = int((p.min_gb or p.over_gb or 0) * 1_000_000_000)
    return remove_targets(
        p.repo, query=p.query, deleted_only=p.deleted_only, min_size=min_size,
        max_size=int((p.max_gb or 0) * 1_000_000_000), source=p.source or None, ext=p.ext or None,
        ext_exclude=p.ext_exclude or None, date_from=p.date_from or None, date_to=p.date_to or None,
        min_versions=p.min_versions, shared_min=p.shared_min, first_from=p.first_from or None,
        prefix=p.prefix or None,
        all_versions=p.all_versions, cap=p.cap or _plan_cap(),
    )


def _remove_scope(everywhere: bool, matched_sources: list[str]):
    """Scope = the sources that OWN the matched paths (NOT sources_for_content, which over a folder
    of mixed small files drags in every source via ubiquitous deduped content). ``everywhere`` is
    the explicit, warned whole-repo sweep (--source dropped)."""
    return None if everywhere else sorted(set(matched_sources))


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
                "total_logical": 0, "capped": False}
    scope = _remove_scope(payload.everywhere, t["sources"])
    # 8k per command so the copied lines are pasteable into a terminal (cmd.exe line cap ~8191).
    # The background executor uses larger 32k batches (fewer kopia invocations).
    commit_cmds = remove_files_chunked_commands(payload.repo, items, source=scope, commit=True, max_chars=8000, flag=flag)
    dry_cmds = remove_files_chunked_commands(payload.repo, items, source=scope, commit=False, max_chars=8000, flag=flag)
    audit.audit("generated", "remove-plan", repo=payload.repo,
                summary={"items": len(items), "by": payload.by, "batches": len(commit_cmds),
                         "sources": scope or [], "everywhere": scope is None,
                         "query": payload.query, "first_from": payload.first_from})
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
    }


# Everything on a remove plan that narrows the match to a SUBSET of the folder. folder_detail knows
# only about a prefix, so if any of these is set its whole-folder answer describes a different
# deletion than the one being planned.
_NARROWING_FILTERS = ("query", "ext", "ext_exclude", "deleted_only", "over_gb", "min_gb", "max_gb",
                      "min_versions", "shared_min", "date_from", "date_to", "first_from")


def _has_filters(payload: RemovePlanRequest) -> bool:
    return any(getattr(payload, name, None) for name in _NARROWING_FILTERS)


@router.post("/actions/remove-detail")
def remove_detail(payload: RemovePlanRequest) -> dict:
    """On-demand reclaim/freeable/shared for the matched set (a chunk walk, so it's a button, not
    auto). Folder under one source -> folder_detail (subtree walk); otherwise -> batch_detail over
    the matched files (capped)."""
    t = _remove_targets(payload)
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
    files = [{"source": s, "full_path": fp} for s, fp in t["pairs"][:5000]]  # cap the walk
    out = batch_detail(payload.repo, files, all_versions=payload.all_versions)
    out["capped"] = len(t["pairs"]) > 5000
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


@router.post("/actions/remove-execute")
def remove_execute(payload: RemovePlanRequest) -> dict:
    """Run the chunked delete in the background (sequential --commit batches). Returns a job_id;
    poll /actions/remove-status. kopia output is appended to the returned log path."""
    t = _remove_targets(payload)
    items, flag = _remove_items(t, payload.by)
    if not items:
        raise HTTPException(status_code=404, detail="no matching files")
    scope = _remove_scope(payload.everywhere, t["sources"])
    job_id = uuid.uuid4().hex[:12]
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_path = remove_log_path(f"remove_repo{payload.repo}_{ts}.log")
    _REMOVE_JOBS[job_id] = {"total": 0, "done": 0, "running": True, "errors": 0,
                            "objects": len(items), "log": log_path}

    def run() -> None:
        try:
            remove_files_chunked_execute(payload.repo, items, scope, job_id, log_path, flag=flag)
        finally:
            _post_remove(payload.repo)  # snapshots rewritten -> reindex so the UI reflects reality

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id, "items": len(items), "log": log_path}


@router.get("/actions/remove-status")
def remove_status(job_id: str) -> dict:
    return _REMOVE_JOBS.get(job_id, {"running": False, "total": 0, "done": 0, "unknown": True})


@router.post("/actions/restore")
def restore(payload: RestoreRequest) -> dict:
    """confirm=false -> the restore command preview. confirm=true -> execute it (writes to the
    target folder / archive). Overwrite/delete-extra only happen if those options are enabled."""
    if not payload.target:
        raise HTTPException(status_code=400, detail="target is required")
    if not payload.confirm:
        cmd = restore_command_full(payload.repo, payload.source_id, payload.target, payload.options)
        audit.audit("generated", "restore-command", repo=payload.repo,
                    summary={"source_id": payload.source_id, "target": payload.target})
        return {"executed": False, "command": _fmt_cmd(cmd)}
    result = restore_execute(payload.repo, payload.source_id, payload.target, payload.options)
    if result["returncode"] != 0:
        raise HTTPException(status_code=500, detail=result["stderr"].strip() or "restore failed")
    return {"executed": True, "command": result.get("command")}


@router.post("/actions/browse-folder")
def browse_folder(payload: dict) -> dict:
    """Open the native Windows folder picker and return the chosen path (server is local)."""
    return {"path": pick_folder(payload.get("initial"))}


@router.post("/actions/folder-reclaim")
def folder_reclaim(payload: FolderReclaimRequest) -> dict:
    """Subtree reclaim (logical / physical / freeable) for a folder - an on-demand walk."""
    try:
        return folder_detail(payload.repo, payload.source, payload.prefix)
    except RepoOffline:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class BatchDetailRequest(BaseModel):
    repo: str = "1"
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
    except RepoOffline:
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


@router.get("/actions/purge-plan")
def purge_plan(repo: str = "1", source: str | None = None, full_path: str | None = None) -> dict:
    if not source or not full_path:
        raise HTTPException(status_code=400, detail="source and full_path are required")
    snapshot_ids = snapshots_for_path(repo, source, full_path)
    snaps = {s["id"]: s for s in sources_index(repo).get(source, [])}
    items = [
        {
            "id": sid,
            "retention": snaps.get(sid, {}).get("retention", []),
            "pinned": bool(snaps.get(sid, {}).get("pins")),
        }
        for sid in snapshot_ids
    ]
    return {
        "repo": repo,
        "source": source,
        "full_path": full_path,
        "snapshot_ids": snapshot_ids,
        "items": items,  # per-snapshot retention/pin status (don't purge a kept one)
        "commands": snapshot_delete_commands(repo, snapshot_ids),
        "executes": False,
    }
