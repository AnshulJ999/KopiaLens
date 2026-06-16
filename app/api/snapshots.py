"""Snapshot browser endpoints."""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

from ..core.sources import resolve_source, sources_index
from ..index.snapanalysis import compute_source, read_analysis

router = APIRouter(tags=["snapshots"])

_ANALYZING: set[str] = set()
_ANALYZING_ALL: set[str] = set()  # repo-level "analyze everything" in progress


def _snap_row(key: str, snap: dict, a: dict) -> dict:
    stats = snap.get("stats", {})
    summ = snap.get("rootEntry", {}).get("summ", {})
    return {
        "id": snap["id"], "source": key,
        "start_time": snap.get("startTime", ""), "end_time": snap.get("endTime", ""),
        "logical": stats.get("totalSize", 0), "files": summ.get("files", 0), "dirs": summ.get("dirs", 0),
        "pins": snap.get("pins"), "retention": snap.get("retention", []),
        "root": snap.get("rootEntry", {}).get("obj"),
        "churn": a.get("churn"), "reclaimable": a.get("reclaimable"),
    }


@router.get("/snapshots")
def snapshots(repo: str = "1", source: str | None = None, source_index: int | None = None) -> dict:
    """Snapshots for a source, merged with persisted per-snapshot churn + reclaimable (when
    available; computed by the background pre-warm or the analyze endpoint). source="*ALL*"
    returns every snapshot across all sources (for the repo-wide by-reclaimable view)."""
    if source == "*ALL*":
        rows, any_analysis = [], False
        for key in sorted(sources_index(repo)):
            analysis = read_analysis(repo, key).get("by_id", {})
            any_analysis = any_analysis or bool(analysis)
            for snap in sources_index(repo)[key]:
                rows.append(_snap_row(key, snap, analysis.get(snap["id"], {})))
        return {"source": "*ALL*", "snapshots": rows, "analyzed": any_analysis,
                "analyzing": False, "all_sources": True}
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


@router.post("/snapshots/analyze-all")
def analyze_all(repo: str = "1", reclaim: bool = True) -> dict:
    """Compute churn + reclaimable for EVERY source in the repo, in the background, and cache it.
    Reclaimable reads the materialized content_refs (kept fresh by the dashboard refresh), so this
    is a one-shot repo-wide pass. Poll /snapshots/analyze-status for completion."""
    if repo in _ANALYZING_ALL:
        return {"started": False, "reason": "already analyzing repo"}

    def run() -> None:
        try:
            for key in sorted(sources_index(repo)):
                try:
                    compute_source(repo, key, with_reclaim=reclaim)
                except Exception:  # noqa: BLE001 - one bad source must not stop the rest
                    continue
        finally:
            _ANALYZING_ALL.discard(repo)

    _ANALYZING_ALL.add(repo)
    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "repo": repo}


@router.get("/snapshots/analyze-status")
def analyze_status(repo: str = "1") -> dict:
    return {"repo": repo, "analyzing_all": repo in _ANALYZING_ALL}
