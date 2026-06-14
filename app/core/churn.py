"""Churn calculations ported from ``kopiatoolkit.py``."""

from __future__ import annotations

from .cache import cache_db
from .metrics import gb
from .sizemap import load_sizemap
from .sources import resolve_source
from .walk import walk_new


def churn_rows(repo: str, source: str, recent: int | None = None, refresh: bool = False) -> dict:
    key, srcs = resolve_source(repo, source=source)
    snaps = srcs[key]
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

    shown = rows[-recent:] if recent else rows
    ongoing = [row["incremental_physical"] for row in rows[1:]]
    avg_tail = ongoing[-30:]
    return {
        "source": key,
        "rows": shown,
        "top": sorted(rows, key=lambda item: item["incremental_physical"], reverse=True)[:12],
        "baseline": rows[0]["incremental_physical"] if rows else 0,
        "total": sum(row["incremental_physical"] for row in rows),
        "ongoing_avg": sum(avg_tail) / len(avg_tail) if avg_tail else 0,
    }
