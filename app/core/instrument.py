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


def _entry_bytes(entries) -> int:
    """Shallow-ish size of one DIR_MEMO value (a list of child dicts): list + each dict + its
    str keys/values. Good enough to tune DIR_MEMO_MAX from real data (not a precise heap walk)."""
    total = sys.getsizeof(entries)
    for d in entries:
        total += sys.getsizeof(d)
        if isinstance(d, dict):
            for k, v in d.items():
                total += sys.getsizeof(k) + sys.getsizeof(v)
    return total


def _dir_memo_mb() -> float | None:
    """Estimated RAM held by the DIR_MEMO LRU. STEP-samples evenly across the whole LRU (not just the
    oldest N entries) with a large sample, so a few very large directories don't skew it - the prior
    first-200 approach swung ~14x (272 MB vs 3.7 GB) for the same 50k entries. Still a sample (avoids
    walking every child list each cycle), just a representative one."""
    try:
        from .cache import DIR_MEMO

        n = len(DIR_MEMO)
        if not n:
            return 0.0
        step = max(1, n // 3000)  # ~3000 evenly-spread samples
        total, taken = 0, 0
        for i, (key, entries) in enumerate(DIR_MEMO.items()):
            if i % step:
                continue
            total += sys.getsizeof(key) + _entry_bytes(entries)
            taken += 1
        avg = total / taken if taken else 0
        return (avg * n) / (1024 * 1024)
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
    from .cache import DIR_MEMO, DIR_MEMO_MAX

    snap: dict = {
        "rss_mb": _rss_mb(),
        "dir_memo": len(DIR_MEMO),
        "dir_memo_max": DIR_MEMO_MAX,
        "dir_memo_mb": _dir_memo_mb(),
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
    try:
        from config import kopia_repos
        from ..index.schema import read_cursor

        for repo in kopia_repos():  # DuckDB tables only exist for kopia repos
            try:
                cur = read_cursor(repo)
                try:
                    files = cur.execute("SELECT count(*) FROM files WHERE repo=?", [repo]).fetchone()[0]
                    paths = cur.execute("SELECT count(*) FROM paths WHERE repo=?", [repo]).fetchone()[0]
                    crefs = cur.execute("SELECT count(*) FROM content_refs WHERE repo=?", [repo]).fetchone()[0]
                    csizes = cur.execute("SELECT count(*) FROM content_sizes WHERE repo=?", [repo]).fetchone()[0]
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
    dmb = f"~{s['dir_memo_mb']:.0f}MB" if s.get("dir_memo_mb") is not None else "?"
    parts = [f"rss={rss}", f"dir_memo={s['dir_memo']}/{s['dir_memo_max']}({dmb})"]
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
