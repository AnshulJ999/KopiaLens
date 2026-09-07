"""Lightweight RSS / cache-size instrumentation for tuning RAM + perf.

Cheap to call (one psutil read + a few COUNT(*) queries). Used by the background
worker to emit one compact memory line per cycle and by the /debug/memory endpoint
for on-demand inspection. Kept deliberately non-intrusive: no behaviour change, just
observation. All heavy imports are lazy so importing this module is free.
"""

from __future__ import annotations

import os
import sys

from .applog import get_logger

log = get_logger("mem")


def _rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - psutil missing / platform quirk: instrumentation must never crash
        return None


def _dir_memo_mb() -> float | None:
    """Exact DIR_MEMO footprint, tracked incrementally in cache.py (no sampling needed - the old
    ~3000-sample estimator is retired now that the byte-budget LRU tracks its own running total)."""
    try:
        from .cache import memo_bytes

        return memo_bytes() / (1024 * 1024)
    except Exception:  # noqa: BLE001 - instrumentation must never crash
        return None


def _shallow_size(obj, _depth: int = 0) -> int:
    """Approximate size of an arbitrary cached value, recursing a few levels into dict/list/tuple
    contents (depth-capped so an unexpected deep structure can't make this slow). Good enough for a
    RAM estimate, not a precise heap walk - same philosophy as _entry_bytes above."""
    total = sys.getsizeof(obj)
    if _depth >= 3:
        return total
    if isinstance(obj, dict):
        for k, v in obj.items():
            total += sys.getsizeof(k) + _shallow_size(v, _depth + 1)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            total += _shallow_size(item, _depth + 1)
    return total


def _cache_stats(cache_dict) -> tuple[int, float]:
    """(entry count, estimated MB) for an arbitrary in-process cache dict/OrderedDict."""
    try:
        n = len(cache_dict)
        total = sum(_shallow_size(k) + _shallow_size(v) for k, v in cache_dict.items())
        return n, total / (1024 * 1024)
    except Exception:  # noqa: BLE001 - instrumentation must never crash
        return 0, 0.0


def memory_snapshot() -> dict:
    """Process RSS + the in-process caches + per-repo DuckDB table sizes (the real RAM drivers)."""
    from .cache import DIR_MEMO, DIR_MEMO_MAX_BYTES

    snap: dict = {
        "rss_mb": _rss_mb(),
        "dir_memo_items": len(DIR_MEMO),
        "dir_memo_mb": _dir_memo_mb(),
        "dir_memo_max_mb": round(DIR_MEMO_MAX_BYTES / (1024 * 1024), 1),
        "repos": {},
    }
    try:  # three other named in-process caches (Pass 12) that were never individually measured
        from ..index.snapanalysis import _RECL_FILES_CACHE, _RECL_FILES_CACHE_MAX
        n, mb = _cache_stats(_RECL_FILES_CACHE)
        snap["recl_files_cache"] = {"entries": n, "max": _RECL_FILES_CACHE_MAX, "mb": round(mb, 1)}
    except Exception as exc:  # noqa: BLE001
        snap["recl_files_cache"] = {"error": str(exc)}
    try:
        from ..index.physical import _RECLAIM_CACHE, _RECLAIM_CACHE_MAX
        n, mb = _cache_stats(_RECLAIM_CACHE)
        snap["reclaim_cache"] = {"entries": n, "max": _RECLAIM_CACHE_MAX, "mb": round(mb, 1)}
    except Exception as exc:  # noqa: BLE001
        snap["reclaim_cache"] = {"error": str(exc)}
    try:  # TTL-based, not count-capped - no "max" field
        from ..index.excluded import _LIST_CACHE
        n, mb = _cache_stats(_LIST_CACHE)
        snap["excluded_list_cache"] = {"entries": n, "mb": round(mb, 1)}
    except Exception as exc:  # noqa: BLE001
        snap["excluded_list_cache"] = {"error": str(exc)}
    try:  # how often a freeable calculation had to price a chunk the refcount table did not know
        from ..index.physical import refcount_miss_stats
        snap["refcount_misses"] = refcount_miss_stats()
    except Exception as exc:  # noqa: BLE001
        snap["refcount_misses"] = {"error": str(exc)}
    try:
        from config import kopia_repos
        from ..index.store import read_cursor

        for repo in kopia_repos():  # DuckDB tables only exist for kopia repos
            try:
                cur = read_cursor(repo)
                try:
                    files = cur.execute("SELECT count(*) FROM filev").fetchone()[0]
                    paths = cur.execute("SELECT count(*) FROM pathx").fetchone()[0]
                    crefs = cur.execute("SELECT count(*) FROM crefs").fetchone()[0]
                    csizes = cur.execute("SELECT count(*) FROM csize").fetchone()[0]
                    try:  # DuckDB's own memory breakdown by internal tag (BASE_TABLE/ART_INDEX/ALLOCATOR/...)
                        dmem_rows = cur.execute(
                            "SELECT tag, SUM(memory_usage_bytes) FROM duckdb_memory() GROUP BY tag"
                        ).fetchall()
                        duckdb_mb = {tag: round(b / (1024 * 1024), 1) for tag, b in dmem_rows}
                    except Exception:  # noqa: BLE001 - availability may vary; never block the row counts above
                        duckdb_mb = None
                finally:
                    cur.close()
                snap["repos"][repo] = {
                    "files": files, "paths": paths, "content_refs": crefs, "content_sizes": csizes,
                    "duckdb_mb": duckdb_mb,
                    "duckdb_total_mb": round(sum(duckdb_mb.values()), 1) if duckdb_mb else None,
                }
            except Exception as exc:  # noqa: BLE001
                snap["repos"][repo] = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        snap["repos_error"] = str(exc)
    return snap


def log_memory(tag: str = "") -> dict:
    """Emit ONE compact line (not spammy) and return the snapshot."""
    s = memory_snapshot()
    rss = f"{s['rss_mb']:.0f}MB" if s.get("rss_mb") is not None else "?"
    dmb = f"{s['dir_memo_mb']:.0f}" if s.get("dir_memo_mb") is not None else "?"
    dmax = f"{s['dir_memo_max_mb']:.0f}" if s.get("dir_memo_max_mb") is not None else "?"
    parts = [f"rss={rss}", f"dir_memo={s['dir_memo_items']} items, {dmb}/{dmax}MB"]
    for key, label in (("recl_files_cache", "recl_files"), ("reclaim_cache", "reclaim")):
        c = s.get(key, {})
        if "error" not in c:
            parts.append(f"{label}={c['entries']}/{c['max']}(~{c['mb']:.0f}MB)")
    c = s.get("excluded_list_cache", {})
    if "error" not in c:
        parts.append(f"excl_list={c['entries']}(~{c['mb']:.0f}MB)")
    for repo, r in s.get("repos", {}).items():
        if "error" not in r:
            dtot = f"{r['duckdb_total_mb']:.0f}MB" if r.get("duckdb_total_mb") is not None else "?"
            parts.append(
                f"r{repo}[files={r['files']} paths={r['paths']} refs={r['content_refs']} duckdb={dtot}]"
            )
    log.info("%s%s", f"{tag} " if tag else "", " ".join(parts))
    return s
