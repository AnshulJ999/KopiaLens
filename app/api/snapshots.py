"""Snapshot browser endpoints."""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

from ..core import paths as pathrules
from ..core.kopia import diff_execute, snapshot_diff_command, snapshot_pin_command
from ..core.nightly import is_running
from ..core.retention import set_reclaim
from ..core.sources import resolve_source, sources_index
from ..index import entries
from ..index.holding import _find, _segments
from ..index.queries import snapshot_files
from ..index.store import NoIndex
from ..index.snapanalysis import batched, compute_source, read_analysis, reclaimable_files
from .errors import PASS_THROUGH

router = APIRouter(tags=["snapshots"])
_UNREADABLE = "kopia could not read this folder. Check the connection and try again."


def _fmt_cmd(cmd: list[str]) -> str:
    return " ".join(f'"{a}"' if (not a or " " in a) else a for a in cmd)


class PinRequest(BaseModel):
    repo: str
    snapshot_ids: list[str]
    remove: bool = False


class SetReclaimRequest(BaseModel):
    repo: str
    snapshot_ids: list[str]
    refresh: bool = False

_ANALYZING: set[str] = set()
_ANALYZING_ALL: set[str] = set()  # repo-level "analyze everything" in progress
_LAST_ALL: dict[str, dict] = {}   # repo -> outcome of the last analyze-all, for analyze-status


def _snap_row(key: str, snap: dict, a: dict) -> dict:
    stats = snap.get("stats", {})
    summ = snap.get("rootEntry", {}).get("summ", {})
    logical = stats.get("totalSize", 0)
    phys = a.get("physical")  # absolute deduped footprint of this snapshot (heavy pass; 0/None = "—")
    return {
        "id": snap["id"], "source": key,
        "start_time": snap.get("startTime", ""), "end_time": snap.get("endTime", ""),
        "logical": logical, "files": summ.get("files", 0), "dirs": summ.get("dirs", 0),
        "pins": snap.get("pins"), "retention": snap.get("retention", []),
        "description": snap.get("description") or "",
        "root": snap.get("rootEntry", {}).get("obj"),
        "physical": phys or None,
        "accum": (round(logical / phys, 2) if phys else None),  # logical / physical = dedup gain
        "churn": a.get("churn"), "reclaimable": a.get("reclaimable"),
    }


def _analysis(repo: str, key: str) -> dict:
    """Persisted per-snapshot space figures, or none before the first index (and always for a repo
    reached through a kopia server): the list itself comes from kopia and still shows."""
    try:
        return read_analysis(repo, key)
    except NoIndex:
        return {}


@router.get("/snapshots")
def snapshots(repo: str, source: str | None = None, source_index: int | None = None) -> dict:
    """Snapshots for a source, merged with persisted per-snapshot churn + reclaimable (when
    available; computed by the background pre-warm or the analyze endpoint). source="*ALL*"
    returns every snapshot across all sources (for the repo-wide by-reclaimable view).
    ``analysis_fresh`` is false when the numbers on screen belong to the previous index run
    () - rows exist but their snaphash predates the current snapshot set."""
    if source == "*ALL*":
        rows, analyzed_sources = [], 0
        all_fresh = True
        for key in sorted(sources_index(repo)):
            info = _analysis(repo, key)
            analysis = info.get("by_id", {})
            if analysis:
                analyzed_sources += 1
                all_fresh = all_fresh and bool(info.get("fresh"))
            for snap in sources_index(repo)[key]:
                rows.append(_snap_row(key, snap, analysis.get(snap["id"], {})))
        analyzed = analyzed_sources > 0
        return {"source": "*ALL*", "snapshots": rows, "analyzed": analyzed,
                "analysis_fresh": (all_fresh if analyzed else True),
                "analyzing": False, "all_sources": True}
    try:
        key, srcs = resolve_source(repo, source=source, index=source_index)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    info = _analysis(repo, key)
    analysis = info.get("by_id", {})
    rows = [_snap_row(key, snap, analysis.get(snap["id"], {})) for snap in srcs[key]]
    return {"source": key, "snapshots": rows, "analyzed": bool(analysis),
            "analysis_fresh": bool(info.get("fresh")),
            "analyzing": f"{repo}|{key}" in _ANALYZING}


@router.post("/snapshots/analyze")
def analyze(repo: str, source: str | None = None, reclaim: bool = True) -> dict:
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
def analyze_all(repo: str, reclaim: bool = True) -> dict:
    """Compute churn + reclaimable for EVERY source in the repo, in the background, and cache it.
    Reclaimable reads the materialized content_refs (kept fresh by the dashboard refresh), so this
    is a one-shot repo-wide pass. Poll /snapshots/analyze-status for completion."""
    if repo in _ANALYZING_ALL:
        return {"started": False, "reason": "already analyzing repo"}
    # An index pass holds the repo's writer lock, so the single batched publish at the end would
    # be declined and the whole pass thrown away. Refuse up front rather than compute for minutes
    # and discard the result.
    if is_running(repo):
        return {"started": False, "reason": "indexing this repo, try again when it finishes"}

    def run() -> None:
        saved = False
        try:
            # ONE publication at the end, not one per source (repo1 = ~84 manifest swaps
            # otherwise). batched() flushes whatever completed even when an exception passes
            # through, so a crash mid-loop still saves the finished sources.
            with batched(repo) as batch:
                for key in sorted(sources_index(repo)):
                    try:
                        compute_source(repo, key, with_reclaim=reclaim)
                    except Exception:  # noqa: BLE001 - one bad source must not stop the rest
                        continue
            saved = bool(batch["saved"])
        finally:
            _LAST_ALL[repo] = {"saved": saved, "reason": None if saved else
                               "another writer held the repo - nothing was saved, run it again"}
            _ANALYZING_ALL.discard(repo)

    _ANALYZING_ALL.add(repo)
    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "repo": repo}


@router.get("/snapshots/analyze-status")
def analyze_status(repo: str) -> dict:
    return {"repo": repo, "analyzing_all": repo in _ANALYZING_ALL,
            "last_run": _LAST_ALL.get(repo)}


@router.post("/snapshots/set-reclaim")
def set_reclaim_route(payload: SetReclaimRequest) -> dict:
    """Accurate combined reclaim for the SELECTED set of snapshots: union(deleted) - union(kept),
    not the per-snapshot sum. Exact for a single-source selection. Can be slow the first time on a
    big source (walks its kept snapshots), then served warm from the dir cache."""
    if not payload.snapshot_ids:
        raise HTTPException(status_code=400, detail="snapshot_ids is empty")
    try:
        return set_reclaim(payload.repo, payload.snapshot_ids, refresh=payload.refresh)
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/snapshots/pin")
def pin(payload: PinRequest) -> dict:
    """Generate (never execute) a pin/unpin command for the selected snapshots."""
    if not payload.snapshot_ids:
        raise HTTPException(status_code=400, detail="snapshot_ids is empty")
    cmd = snapshot_pin_command(payload.repo, payload.snapshot_ids, remove=payload.remove)
    return {"remove": payload.remove, "command": _fmt_cmd(cmd)}


def _iso(value) -> str:
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        return text if value.tzinfo else text + "Z"
    return str(value or "")


@router.get("/snapshots/browse")
def browse(repo: str, source: str, snapshot_id: str, path: str = "") -> dict:
    """One folder of one snapshot, read on demand: the index when it holds the folder, kopia
    otherwise. Needs no index, so it also serves repos reached through a kopia server."""
    snap = next((s for s in sources_index(repo).get(source, []) if s.get("id") == snapshot_id), None)
    if snap is None:
        raise HTTPException(status_code=404, detail="That snapshot no longer exists.")
    sep = pathrules.sep(source)
    root = pathrules.source_root(source).rstrip(sep)
    full = (path or root).rstrip(sep) or root
    segs = _segments(source, full)
    if segs is None:
        raise HTTPException(status_code=400, detail="That folder is not inside this snapshot's source.")
    fold = not pathrules.case_sensitive(source)
    oid = (snap.get("rootEntry") or {}).get("obj") or ""
    crumbs = [{"name": root or sep, "path": root}]
    try:
        for i, seg in enumerate(segs):
            kids = entries.children_of(repo, [oid]).get(oid)
            if kids is None:
                raise HTTPException(status_code=502, detail=_UNREADABLE)
            entry = _find(kids, seg, fold)
            if entry is None or entry.get("type") != "d":
                raise HTTPException(status_code=404, detail=f"{seg} is not a folder in this snapshot.")
            oid = entry.get("obj") or ""
            crumbs.append({"name": seg, "path": sep.join([root, *segs[:i + 1]])})
        kids = entries.children_of(repo, [oid]).get(oid)
    except PASS_THROUGH:
        raise
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if kids is None:
        raise HTTPException(status_code=502, detail=_UNREADABLE)
    rows = []
    for e in kids:
        summ = e.get("summ") or {}
        is_dir = e.get("type") == "d"
        rows.append({"name": e.get("name") or "", "type": e.get("type") or "",
                     "size": int((summ.get("size") if is_dir else e.get("size")) or 0),
                     "files": int(summ.get("files") or 0) if is_dir else None,
                     "mtime": _iso(e.get("mtime")),
                     "full_path": sep.join([full, e.get("name") or ""])})
    rows.sort(key=lambda r: (r["type"] != "d", r["name"].lower()))
    return {"source": source, "snapshot_id": snapshot_id, "start_time": snap.get("startTime") or "",
            "path": full, "crumbs": crumbs, "entries": rows}


@router.get("/snapshots/diff")
def diff(repo: str, id1: str | None = None, id2: str | None = None, run: bool = False) -> dict:
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
def delete_preview(repo: str, ids: str = "", precise: bool = False, limit: int = 60) -> dict:
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
