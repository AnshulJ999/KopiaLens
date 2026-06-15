"""Churn calculations ported from ``kopiatoolkit.py``.

Churn is a per-source object-tree walk (heavy). Results are RAM-cached keyed by the
source's snapshot-set hash, so re-viewing the same source is instant; it recomputes only
when that source gets new/removed snapshots (or on ``refresh``). Background pre-warming
of every source is a later step.
"""

from __future__ import annotations

import hashlib

from .cache import cache_db
from .metrics import gb
from .sizemap import load_sizemap
from .sources import resolve_source
from .walk import walk_new

_CACHE: dict[str, dict] = {}


def _snaphash(snaps: list[dict]) -> str:
    digest = hashlib.md5()
    for snap in snaps:
        digest.update(snap.get("id", "").encode())
    return digest.hexdigest()


def _compute(repo: str, key: str, snaps: list[dict], refresh: bool) -> dict:
    db = cache_db(repo)
    smap = load_sizemap(repo, refresh=refresh)
    seen_dirs: set[str] = set()
    seen_content: set[str] = set()
    rows = []
    for snap in snaps:
        root = snap.get("rootEntry", {}).get("obj")
        if not root:
            incr = 0
        else:
            found = walk_new(repo, db, [root], seen_dirs)
            incr = sum(smap.get(cid, 0) for cid in found if cid not in seen_content)
            seen_content |= found
        rows.append(
            {
                "id": snap["id"],
                "start_time": snap.get("startTime", ""),
                "logical": snap.get("stats", {}).get("totalSize", 0),
                "non_cached_files": snap.get("stats", {}).get("nonCachedFiles", 0),
                "incremental_physical": incr,
                "incremental_gb": gb(incr),
            }
        )
    ongoing = [row["incremental_physical"] for row in rows[1:]]
    avg_tail = ongoing[-30:]
    return {
        "source": key,
        "rows": rows,  # full set; sliced per-request below
        "top": sorted(rows, key=lambda item: item["incremental_physical"], reverse=True)[:12],
        "baseline": rows[0]["incremental_physical"] if rows else 0,
        "total": sum(row["incremental_physical"] for row in rows),
        "ongoing_avg": sum(avg_tail) / len(avg_tail) if avg_tail else 0,
    }


def churn_rows(repo: str, source: str, recent: int | None = None, refresh: bool = False) -> dict:
    key, srcs = resolve_source(repo, source=source)
    snaps = srcs[key]
    cache_key = f"{repo}|{key}"
    snap_hash = _snaphash(snaps)
    cached = _CACHE.get(cache_key)
    if refresh or not cached or cached["hash"] != snap_hash:
        data = _compute(repo, key, snaps, refresh)
        _CACHE[cache_key] = {"hash": snap_hash, "data": data}
    else:
        data = cached["data"]
    rows = data["rows"]
    shown = rows[-recent:] if recent else rows
    return {**data, "rows": shown}
