"""Operational actions.

Restore and purge are command previews only. Mount is allowed to execute because
it is repo-read-only, but it is explicit and returns the spawned process id.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.kopia import (
    list_mounts,
    mount_and_browse,
    mount_snapshot,
    next_free_drive,
    restore_command,
    snapshot_delete_commands,
    snapshot_delete_execute,
    unmount,
)
from ..core.sources import invalidate as invalidate_sources, sources_index
from ..index.builder import start_build
from ..index.queries import snapshots_for_path

router = APIRouter(tags=["actions"])

_SEP = chr(92)  # backslash


def _source_root(source: str) -> str:
    return source.split(":", 1)[1] if ":" in source else source


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
