"""Snapshot browser endpoints."""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

from ..core.kopia import diff_execute, snapshot_diff_command, snapshot_pin_command
from ..core.retention import set_reclaim
from ..core.sources import resolve_source, sources_index
from ..index.queries import snapshot_files
from ..index.snapanalysis import compute_source, read_analysis, reclaimable_files

router = APIRouter(tags=["snapshots"])


def _fmt_cmd(cmd: list[str]) -> str:
    return " ".join(f'"{a}"' if (not a or " " in a) else a for a in cmd)


class PinRequest(BaseModel):
    repo: str = "1"
    snapshot_ids: list[str]
    remove: bool = False


class SetReclaimRequest(BaseModel):
    repo: str = "1"
    snapshot_ids: list[str]
    refresh: bool = False

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


@router.post("/snapshots/set-reclaim")
def set_reclaim_route(payload: SetReclaimRequest) -> dict:
    """Accurate combined reclaim for the SELECTED set of snapshots: union(deleted) - union(kept),
    not the per-snapshot sum. Exact for a single-source selection. Can be slow the first time on a
    big source (walks its kept snapshots), then served warm from the dir cache."""
    if not payload.snapshot_ids:
        raise HTTPException(status_code=400, detail="snapshot_ids is empty")
    try:
        return set_reclaim(payload.repo, payload.snapshot_ids, refresh=payload.refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/snapshots/pin")
def pin(payload: PinRequest) -> dict:
    """Generate (never execute) a pin/unpin command for the selected snapshots."""
    if not payload.snapshot_ids:
        raise HTTPException(status_code=400, detail="snapshot_ids is empty")
    cmd = snapshot_pin_command(payload.repo, payload.snapshot_ids, remove=payload.remove)
    return {"remove": payload.remove, "command": _fmt_cmd(cmd)}


@router.get("/snapshots/diff")
def diff(repo: str = "1", id1: str | None = None, id2: str | None = None, run: bool = False) -> dict:
    """Diff two snapshots. run=false -> the command; run=true -> execute and return the text."""
    if not id1 or not id2:
        raise HTTPException(status_code=400, detail="id1 and id2 are required")
    out: dict = {"command": _fmt_cmd(snapshot_diff_command(repo, id1, id2))}
    if run:
        try:
            out["output"] = diff_execute(repo, id1, id2)
        except Exception as exc:  # noqa: BLE001
            out["output"] = f"(diff failed: {exc})"
    return out


@router.get("/snapshots/delete-preview")
def delete_preview(repo: str = "1", ids: str = "", precise: bool = False, limit: int = 60) -> dict:
    """What gets reclaimed by deleting the selected snapshots.
    Fast = largest files new in them (delta). Precise (single snapshot) = the ACCURATE
    exclusive-content file list (matches the reclaimable number)."""
    snap_ids = [s for s in ids.split(",") if s]
    if precise and len(snap_ids) == 1:
        sid = snap_ids[0]
        src = next((k for k, lst in sources_index(repo).items() if any(s["id"] == sid for s in lst)), None)
        if src:
            return {"files": reclaimable_files(repo, src, sid, limit=limit), "precise": True, "exact": True}
    files = snapshot_files(repo, snap_ids, limit=limit, reclaimable_only=precise)
    return {"files": files, "precise": precise, "exact": False}
