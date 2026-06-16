"""Snapshot browser endpoints."""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

from ..core.sources import resolve_source
from ..index.snapanalysis import compute_source, read_analysis

router = APIRouter(tags=["snapshots"])

_ANALYZING: set[str] = set()


@router.get("/snapshots")
def snapshots(repo: str = "1", source: str | None = None, source_index: int | None = None) -> dict:
    """Snapshots for a source, merged with persisted per-snapshot churn + reclaimable (when
    available; computed by the background pre-warm or the analyze endpoint)."""
    try:
        key, srcs = resolve_source(repo, source=source, index=source_index)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    analysis = read_analysis(repo, key).get("by_id", {})
    rows = []
    for snap in srcs[key]:
        stats = snap.get("stats", {})
        summ = snap.get("rootEntry", {}).get("summ", {})
        a = analysis.get(snap["id"], {})
        rows.append(
            {
                "id": snap["id"],
                "source": key,
                "start_time": snap.get("startTime", ""),
                "end_time": snap.get("endTime", ""),
                "logical": stats.get("totalSize", 0),
                "files": summ.get("files", 0),
                "dirs": summ.get("dirs", 0),
                "pins": snap.get("pins"),
                "retention": snap.get("retention", []),
                "root": snap.get("rootEntry", {}).get("obj"),
                "churn": a.get("churn"),          # None -> "—" (not yet analyzed)
                "reclaimable": a.get("reclaimable"),
            }
        )
    return {"source": key, "snapshots": rows, "analyzed": bool(analysis), "analyzing": f"{repo}|{key}" in _ANALYZING}


@router.post("/snapshots/analyze")
def analyze(repo: str = "1", source: str | None = None, reclaim: bool = True) -> dict:
    """Kick a background per-snapshot analysis (churn + reclaimable) for one source. Poll
    /snapshots to pick up results when ``analyzing`` flips back to false."""
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    key, _ = resolve_source(repo, source=source)
    tag = f"{repo}|{key}"
    if tag in _ANALYZING:
        return {"started": False, "reason": "already analyzing", "source": key}

    def run() -> None:
        try:
            compute_source(repo, key, with_reclaim=reclaim)
        finally:
            _ANALYZING.discard(tag)

    _ANALYZING.add(tag)
    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "source": key}
