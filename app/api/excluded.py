"""Excluded browser endpoints — local-disk coverage gaps vs both kopia repos."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.everything import available, list_drives
from ..index.excluded import folder_listing

router = APIRouter(tags=["excluded"])


@router.get("/excluded/drives")
def drives() -> dict:
    """Local fixed drives + whether Everything (es.exe) is reachable."""
    return {"drives": list_drives(), "everything_ok": available()}


@router.get("/excluded/list")
def list_folder(path: str = "") -> dict:
    """Direct children of `path` with backup status (backed / partial / excluded / new) + the
    'excluded under here' byte total per folder. Empty path -> caller should show the drive list."""
    if not path:
        raise HTTPException(status_code=400, detail="path is required (use /excluded/drives for roots)")
    try:
        return folder_listing(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
