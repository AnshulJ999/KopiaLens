"""Logs tab routes () - the pieces that are not audit, jobs or index runs.

The app log itself (data/logs/kopialens.log, rotating) is surfaced here so
"what has the app been doing" is answerable inside the UI. Read-only, tail only.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from ..core.applog import LOG_FILE

router = APIRouter(tags=["logs"])

_APP_LOG_TAIL_BYTES = 262_144  # 256 KB is far more than 500 lines


@router.get("/logs/app")
def app_log_tail(limit: int = 500) -> dict:
    """The last ``limit`` lines of the app log (the current rotation file only - not the
    rolled history; that stays on disk for the really old stuff)."""
    limit = max(1, min(limit, 2000))
    try:
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, "rb") as fh:
            if size > _APP_LOG_TAIL_BYTES:
                fh.seek(-_APP_LOG_TAIL_BYTES, os.SEEK_END)
                fh.readline()  # discard the partial line the seek landed in
            tail = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"app log not found: {exc}") from exc
    lines = tail.splitlines()
    return {"lines": lines[-limit:], "path": LOG_FILE,
            "truncated": len(lines) > limit or size > _APP_LOG_TAIL_BYTES}
