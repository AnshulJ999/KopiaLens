"""Per-snapshot analysis: churn (new physical) + reclaimable (freed if this snapshot is
deleted), persisted to DuckDB so it survives restarts and is instant to re-view.

Churn = a forward incremental walk (chunks reachable via directories new vs older snapshots).
Reclaimable = chunks that exist ONLY in this snapshot: new in BOTH the forward pass (not in any
older snapshot) AND the backward pass (not in any newer), and not present in any OTHER source
(global source refcount == 1). Deleting the snapshot frees exactly those.
"""

from __future__ import annotations

import hashlib

from ..core.cache import cache_db
from ..core.sizemap import load_sizemap
from ..core.sources import resolve_source
from ..core.walk import walk_new
from .physical import _refcount_map
from .schema import get_db, read_cursor


def _snaphash(snaps: list[dict]) -> str:
    digest = hashlib.md5()
    for snap in snaps:
        digest.update(snap.get("id", "").encode())
    return digest.hexdigest()


def _persist(repo: str, source: str, rows: list[dict], snaphash: str) -> None:
    con = get_db(repo).cursor()
    try:
        con.execute("DELETE FROM snapshot_analysis WHERE repo=? AND source=?", [repo, source])
        for r in rows:
            rec = r["reclaimable"]
            con.execute(
                "INSERT OR REPLACE INTO snapshot_analysis(repo, source, snapshot_id, churn, physical, reclaimable, snaphash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [repo, source, r["id"], int(r["churn"]), 0, None if rec is None else int(rec), snaphash],
            )
        con.commit()
    finally:
        con.close()


def compute_source(repo: str, source: str, with_reclaim: bool = True) -> dict:
    """Walk a source's snapshots and compute per-snapshot churn (+ reclaimable). Persisted."""
    key, srcs = resolve_source(repo, source=source)
    snaps = sorted(srcs[key], key=lambda s: s.get("startTime", ""))  # oldest -> newest
    db = cache_db(repo)
    smap = load_sizemap(repo)

    # Forward pass: churn (new physical) + remember each snapshot's forward-new chunk set.
    seen_dirs: set[str] = set()
    seen_content: set[str] = set()
    fwd_new: dict[str, set[str]] = {}
    churn: dict[str, int] = {}
    for snap in snaps:
        root = snap.get("rootEntry", {}).get("obj")
        found = walk_new(repo, db, [root], seen_dirs) if root else set()
        new_here = found - seen_content
        fwd_new[snap["id"]] = new_here
        churn[snap["id"]] = sum(smap.get(c, 0) for c in new_here)
        seen_content |= found

    reclaim: dict[str, int | None] = {sid: None for sid in churn}  # None = not computed (shows "—")
    if with_reclaim:
        refs = _refcount_map(repo)  # global per-source refcount: ==1 means only this source has it
        seen_dirs_b: set[str] = set()
        seen_content_b: set[str] = set()
        for snap in reversed(snaps):  # newest -> oldest
            root = snap.get("rootEntry", {}).get("obj")
            found = walk_new(repo, db, [root], seen_dirs_b) if root else set()
            new_bwd = found - seen_content_b
            exclusive = fwd_new[snap["id"]] & new_bwd  # only in this snapshot (no older, no newer)
            reclaim[snap["id"]] = sum(smap.get(c, 0) for c in exclusive if refs.get(c, 0) <= 1)
            seen_content_b |= found

    rows = [{"id": sid, "churn": churn[sid], "reclaimable": reclaim[sid]} for sid in churn]
    snaphash = _snaphash(snaps)
    _persist(repo, key, rows, snaphash)
    return {"source": key, "rows": rows, "snaphash": snaphash, "with_reclaim": with_reclaim}


def read_analysis(repo: str, source: str, compute_if_stale: bool = False, with_reclaim: bool = True) -> dict:
    """Return persisted per-snapshot churn/reclaimable; (re)compute if missing/stale when asked."""
    key, srcs = resolve_source(repo, source=source)
    want = _snaphash(srcs[key])
    cur = read_cursor(repo)
    try:
        rows = cur.execute(
            "SELECT snapshot_id, churn, reclaimable, snaphash FROM snapshot_analysis WHERE repo=? AND source=?",
            [repo, key],
        ).fetchall()
    finally:
        cur.close()
    fresh = bool(rows) and all(r[3] == want for r in rows)
    if compute_if_stale and not fresh:
        rows = [(r["id"], r["churn"], r["reclaimable"], want)
                for r in compute_source(repo, key, with_reclaim=with_reclaim)["rows"]]
        fresh = True
    return {
        "source": key, "fresh": fresh,
        "by_id": {r[0]: {"churn": r[1], "reclaimable": r[2]} for r in rows},  # None preserved -> "—"
    }


def prewarm(repo: str, with_reclaim: bool = False) -> None:
    """Background: ensure every source has churn computed + persisted (cheap when unchanged)."""
    from ..core.sources import sources_index
    for key in sorted(sources_index(repo)):
        try:
            if not read_analysis(repo, key)["fresh"]:
                compute_source(repo, key, with_reclaim=with_reclaim)
        except Exception:
            continue
