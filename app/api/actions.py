"""Operational actions.

Restore and purge are command previews only. Mount is allowed to execute because
it is repo-read-only, but it is explicit and returns the spawned process id.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.kopia import (
    list_mounts,
    mount_snapshot,
    restore_command,
    snapshot_delete_commands,
    unmount,
)
from ..core.sources import sources_index
from ..index.queries import snapshots_for_path

router = APIRouter(tags=["actions"])


class RestorePreview(BaseModel):
    repo: str = "1"
    source: str
    destination: str
    overwrite: bool = False


class MountRequest(BaseModel):
    repo: str = "1"
    snapshot_id: str
    mountpoint: str


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
