"""Operational actions.

Restore and purge are command previews only. Mount is allowed to execute because
it is repo-read-only, but it is explicit and returns the spawned process id.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from collections import defaultdict

from ..core.kopia import (
    list_mounts,
    mount_and_browse,
    mount_snapshot,
    next_free_drive,
    pick_folder,
    remove_files_command,
    remove_files_execute,
    restore_command,
    restore_command_full,
    restore_execute,
    snapshot_delete_commands,
    snapshot_delete_execute,
    unmount,
)
from ..core.sources import invalidate as invalidate_sources, sources_index
from ..index.builder import start_build
from ..index.physical import folder_detail
from ..index.queries import snapshots_for_path, versions as file_versions

router = APIRouter(tags=["actions"])

_SEP = chr(92)  # backslash


def _source_root(source: str) -> str:
    return source.split(":", 1)[1] if ":" in source else source


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
    by: str = "object-id"   # "object-id" (precise, all versions, all sources) | "filename" (per-source)
    confirm: bool = False
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
    return {"command": restore_command(payload.repo, payload.source, payload.destination, overwrite=payload.overwrite)}


@router.post("/actions/mount")
def mount(payload: MountRequest) -> dict:
    try:
        proc = mount_snapshot(payload.repo, payload.snapshot_id, payload.mountpoint)
        return {"pid": proc.pid, "command": proc.args}
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
        cmds = snapshot_delete_commands(payload.repo, payload.snapshot_ids)
        return {"executed": False, "count": len(payload.snapshot_ids),
                "commands": "\n".join(" ".join(c) for c in cmds)}
    result = snapshot_delete_execute(payload.repo, payload.snapshot_ids)
    if result["returncode"] != 0:
        raise HTTPException(status_code=500, detail=result["stderr"].strip() or "kopia snapshot delete failed")
    invalidate_sources(payload.repo)            # the snapshot set changed -> drop the cached listing
    try:
        start_build(payload.repo)               # incremental reindex picks up the now-missing snapshots
    except Exception:  # noqa: BLE001 - reindex is best-effort; UI Refresh also recovers
        pass
    return {"deleted": result["deleted"], "command": result.get("command")}


def _rf_targets(payload: RemoveFilesRequest) -> list[tuple[str, str]]:
    if payload.files:
        return [(f["source"], f["full_path"]) for f in payload.files if f.get("source") and f.get("full_path")]
    if payload.source and payload.full_path:
        return [(payload.source, payload.full_path)]
    return []


def _post_remove(repo: str) -> None:
    invalidate_sources(repo)
    try:
        start_build(repo)  # snapshots were rewritten -> reindex so the UI reflects reality
    except Exception:  # noqa: BLE001
        pass


@router.post("/actions/remove-files")
def remove_files(payload: RemoveFilesRequest) -> dict:
    """Remove file(s) from ALL snapshots (without deleting the snapshots), via
    `kopia snapshot fix remove-files`. Supports single or batch. by="object-id" removes the exact
    content everywhere (no --source); by="filename" is scoped per source. confirm=false returns
    dry-run + commit commands; confirm=true executes with --commit then reindexes."""
    targets = _rf_targets(payload)
    if not targets:
        raise HTTPException(status_code=400, detail="no files specified")

    if payload.by == "filename":  # group basenames per source (scoped, safe)
        groups: dict[str, list[str]] = defaultdict(list)
        for src, fp in targets:
            name = fp.rsplit(_SEP, 1)[-1].rsplit("/", 1)[-1]
            if name not in groups[src]:
                groups[src].append(name)
        if not payload.confirm:
            dry = [_fmt_cmd(remove_files_command(payload.repo, filenames=n, source=s, commit=False)) for s, n in groups.items()]
            com = [_fmt_cmd(remove_files_command(payload.repo, filenames=n, source=s, commit=True)) for s, n in groups.items()]
            return {"executed": False, "by": "filename", "count": sum(len(n) for n in groups.values()),
                    "dry_run": "\n".join(dry), "commit": "\n".join(com)}
        for s, n in groups.items():
            r = remove_files_execute(payload.repo, filenames=n, source=s)
            if r["returncode"] != 0:
                raise HTTPException(status_code=500, detail=r["stderr"].strip() or "remove-files failed")
        _post_remove(payload.repo)
        return {"executed": True}

    # object-id: distinct content objects across all targets' versions (precise, all sources)
    seen: set[str] = set()
    oids: list[str] = []
    for src, fp in targets:
        for v in file_versions(payload.repo, src, fp):
            oid = v.get("content_id")
            if oid and oid not in seen:
                seen.add(oid)
                oids.append(oid)
    if not oids:
        raise HTTPException(status_code=404, detail="no content objects found")
    if not payload.confirm:
        return {"executed": False, "by": "object-id", "count": len(oids),
                "dry_run": _fmt_cmd(remove_files_command(payload.repo, object_ids=oids, commit=False)),
                "commit": _fmt_cmd(remove_files_command(payload.repo, object_ids=oids, commit=True))}
    r = remove_files_execute(payload.repo, object_ids=oids)
    if r["returncode"] != 0:
        raise HTTPException(status_code=500, detail=r["stderr"].strip() or "remove-files failed")
    _post_remove(payload.repo)
    return {"executed": True}


@router.post("/actions/restore")
def restore(payload: RestoreRequest) -> dict:
    """confirm=false -> the restore command preview. confirm=true -> execute it (writes to the
    target folder / archive). Overwrite/delete-extra only happen if those options are enabled."""
    if not payload.target:
        raise HTTPException(status_code=400, detail="target is required")
    if not payload.confirm:
        cmd = restore_command_full(payload.repo, payload.source_id, payload.target, payload.options)
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
