"""Snapshot browser endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.churn import churn_rows
from ..core.sources import resolve_source

router = APIRouter(tags=["snapshots"])


@router.get("/snapshots")
def snapshots(repo: str = "1", source: str | None = None, source_index: int | None = None) -> dict:
    try:
        key, srcs = resolve_source(repo, source=source, index=source_index)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = []
    for snap in srcs[key]:
        stats = snap.get("stats", {})
        rows.append(
            {
                "id": snap["id"],
                "source": key,
                "start_time": snap.get("startTime", ""),
                "end_time": snap.get("endTime", ""),
                "logical": stats.get("totalSize", 0),
                "files": stats.get("totalFileCount", 0),
                "dirs": stats.get("totalDirectoryCount", 0),
                "pins": snap.get("pins"),
                "root": snap.get("rootEntry", {}).get("obj"),
            }
        )
    return {"source": key, "snapshots": rows}


@router.get("/snapshots/churn")
def snapshot_churn(repo: str = "1", source: str | None = None, recent: int = 20) -> dict:
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    try:
        return churn_rows(repo, source, recent=recent)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
