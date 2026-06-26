"""Physical/freeable/dedup/accum metrics ported from ``kopiatoolkit.py``."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import pickle
import threading
import time

from config import CACHEDIR

from .applog import get_logger
from .cache import cache_db
from .sizemap import load_sizemap
from .schedule import load_next_snapshots
from .sources import logical_stats, roots_of, sources_index, sources_meta, split_sources
from .walk import walk

log = get_logger("metrics")

# Bump when the dashboard row schema changes so stale on-disk caches auto-invalidate
# (the cache used to be served verbatim, which is how added columns silently went blank).
TABLE_CACHE_VERSION = 2

# Single-flight: only ONE dashboard build per (repo, mode) may run at a time. Concurrent callers
# (two browser tabs + the auto-refresh timer + the background warm pass) serve the existing cache
# instead of each launching their own 80s+ build_global - that pile-up was the 18 GB / 3-min storm.
_BUILD_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# Serializes the content_refs reinsert so concurrent builds can't collide (the DuckDB
# "Conflict on tuple deletion" we saw).
_REFCOUNT_WRITE_LOCK = threading.Lock()
# Time-gate the DuckDB content_refs rewrite. The full-table DELETE+INSERT (~3.5M rows) is grow-only
# in DuckDB and was the single biggest growth source (snapshots land many x/day, so the distinct-
# content count changes constantly -> a rewrite nearly every build). The dashboard uses the FRESH
# in-memory count below; only file_detail / reclaimable read the DuckDB table and tolerate a few-
# hours-stale refcount, so we rewrite at most once per window. (last write per repo; 0 = never ->
# the first build after a cold start always populates it.)
_LAST_REFCOUNT_WRITE: dict[str, float] = {}
_DEFAULT_REFCOUNT_WRITE_INTERVAL = 21600  # 6h; overridable via settings.kopialens.refcount_write_interval


def _refcount_write_interval() -> float:
    try:
        from ..settings import load_settings
        return float(load_settings().get("kopialens", {}).get("refcount_write_interval",
                                                               _DEFAULT_REFCOUNT_WRITE_INTERVAL))
    except Exception:  # noqa: BLE001
        return _DEFAULT_REFCOUNT_WRITE_INTERVAL


def _build_lock(repo: str, mode: str) -> threading.Lock:
    key = f"{repo}|{mode}"
    with _LOCKS_GUARD:
        lk = _BUILD_LOCKS.get(key)
        if lk is None:
            lk = _BUILD_LOCKS[key] = threading.Lock()
        return lk


def gb(value: int | float) -> float:
    return value / 1_000_000_000


def src_snaphash(snaps: list[dict], recent: int | str | None) -> str:
    digest = hashlib.md5()
    digest.update(str(recent).encode())
    for snap in snaps:
        digest.update(snap.get("id", "").encode())
    return digest.hexdigest()


def snaphash(srcs: dict[str, list[dict]], keys: list[str], recent: int | str | None, mode: str) -> str:
    digest = hashlib.md5()
    digest.update(f"v2|{recent}|{mode}".encode())
    for key in keys:
        for snap in srcs[key]:
            digest.update(snap.get("id", "").encode())
    return digest.hexdigest()


def source_set(repo: str, db, smap: dict[str, int], key: str, snaps: list[dict], recent: int | str | None = None):
    rec = int(recent) if recent else None
    use = snaps[-rec:] if rec else snaps
    snap_hash = src_snaphash(use, recent)
    folder = os.path.join(CACHEDIR, f"srcsets_repo{repo}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, hashlib.md5(key.encode()).hexdigest() + f"_{rec or 'all'}.pkl")
    try:
        with open(path, "rb") as handle:
            cached = pickle.load(handle)
        if cached.get("hash") == snap_hash:
            return cached["cids"], cached["phys_latest"]
    except Exception:
        pass

    cids, _ = walk(repo, roots_of(use), db)
    latest_cids, _ = walk(repo, roots_of(use[-1:]), db) if use else (set(), {})
    phys_latest = sum(smap.get(cid, 0) for cid in latest_cids)
    try:
        with open(path, "wb") as handle:
            pickle.dump({"hash": snap_hash, "cids": cids, "phys_latest": phys_latest}, handle, protocol=4)
    except Exception:
        pass
    return cids, phys_latest


def refcount_path(repo: str, mode: str) -> str:
    return os.path.join(CACHEDIR, f"refcount_repo{repo}_{mode}.pkl")


def build_global(
    repo: str,
    db,
    smap: dict[str, int],
    recent: int | str | None = None,
    mode: str = "all",
) -> tuple[dict[str, dict], float, dict[str, int], int]:
    """Global content refcount + per-source physical/freeable.

    Memory-light streaming: we build the refcount Counter one source at a time and DISCARD
    each source's content-id set immediately (it is cheap to reload from the on-disk
    ``srcsets`` pkl cache for the freeable pass), instead of holding every source's set in RAM
    simultaneously. Peak RAM drops from (all sets + counter) to (counter + one set). The
    refcount is also persisted to the DuckDB ``content_refs`` table so file_detail /
    reclaimable read it without recomputing.
    """
    srcs = sources_index(repo)
    membership, display = split_sources(srcs, mode)
    rec = int(recent) if recent else None
    count: collections.Counter[str] = collections.Counter()
    latest: dict[str, int] = {}
    start = time.time()

    # Pass 1: accumulate the global refcount, keeping only the counter (sets discarded).
    for key in membership:
        cids, phys_latest = source_set(repo, db, smap, key, srcs[key], recent)
        latest[key] = phys_latest
        count.update(cids)
        del cids  # release this source's set before loading the next

    # Pass 2: per-source physical/freeable, reloading each set (from the pkl cache) then discarding.
    rows: dict[str, dict] = {}
    for key in display:
        cids, _ = source_set(repo, db, smap, key, srcs[key], recent)
        snaps = srcs[key][-rec:] if rec else srcs[key]
        st = logical_stats(snaps)
        total = sum(smap.get(cid, 0) for cid in cids)
        free = sum(smap.get(cid, 0) for cid in cids if count[cid] == 1)
        pins = sum(1 for snap in snaps if snap.get("pins"))
        rows[key] = {"st": st, "total": total, "free": free, "phys_latest": latest.get(key, 0), "pins": pins}
        del cids

    union_total = sum(smap.get(cid, 0) for cid in count)
    try:
        with open(refcount_path(repo, mode), "wb") as handle:
            pickle.dump(
                {"hash": snaphash(srcs, membership, recent, mode), "count": dict(count), "union": union_total},
                handle,
                protocol=4,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("repo=%s refcount pkl write failed: %s", repo, exc)

    # persist the canonical full refcount into DuckDB (serialized + time-gated to cut grow-only churn)
    if mode == "all" and not rec and (time.time() - _LAST_REFCOUNT_WRITE.get(repo, 0.0) >= _refcount_write_interval()):
        with _REFCOUNT_WRITE_LOCK:
            try:
                from ..index.physical import materialize_refcount  # lazy: avoids core<-index import cycle
                from ..index.schema import get_db
                con = get_db(repo).cursor()
                try:
                    materialize_refcount(con, repo, dict(count))
                    con.commit()
                finally:
                    con.close()
                _LAST_REFCOUNT_WRITE[repo] = time.time()
                log.info("repo=%s content_refs materialized (%d contents)", repo, len(count))
            except Exception as exc:  # noqa: BLE001
                log.warning("repo=%s content_refs materialize failed: %s", repo, exc)

    return rows, time.time() - start, dict(count), union_total


def _read_refcount_pkl(repo: str, mode: str, key: str):
    try:
        with open(refcount_path(repo, mode), "rb") as handle:
            cached = pickle.load(handle)
        if cached.get("hash") == key:
            return cached["count"], cached["union"]
    except Exception:
        pass
    return None


def cached_refcount(repo: str, db, smap: dict[str, int], mode: str = "all", recent: int | str | None = None):
    srcs = sources_index(repo)
    membership, _ = split_sources(srcs, mode)
    key = snaphash(srcs, membership, recent, mode)
    hit = _read_refcount_pkl(repo, mode, key)
    if hit is not None:
        return hit
    # Share the dashboard single-flight lock so a refcount build can't run concurrently with a
    # table_rows build (or another refcount build) - that was part of the multi-build RAM storm.
    with _build_lock(repo, mode):
        hit = _read_refcount_pkl(repo, mode, key)  # a concurrent build may have just written it
        if hit is not None:
            return hit
        _, _, count, union = build_global(repo, db, smap, recent=recent, mode=mode)
        return count, union


def physical_report(repo: str, source: str, recent: int | str | None = None, refresh: bool = False) -> dict:
    srcs = sources_index(repo)
    snaps = srcs[source]
    rec = int(recent) if recent else None
    stats = logical_stats(snaps[-rec:] if rec else snaps)
    db = cache_db(repo)
    smap = load_sizemap(repo, refresh=refresh)
    cids, walk_stats = walk(repo, roots_of(snaps, rec), db)
    missing = sum(1 for cid in cids if cid not in smap)
    if missing and not refresh:
        smap = load_sizemap(repo, refresh=True)
        missing = sum(1 for cid in cids if cid not in smap)
    phys_all = sum(smap.get(cid, 0) for cid in cids)
    latest_roots = roots_of(snaps[-1:])
    latest_cids, _ = walk(repo, latest_roots, db) if latest_roots else (set(), {})
    phys_latest = sum(smap.get(cid, 0) for cid in latest_cids)
    return {
        "source": source,
        "stats": stats,
        "physical_all": phys_all,
        "physical_latest": phys_latest,
        "dedup": stats["sum"] / phys_all if phys_all else 0,
        "accum": phys_all / phys_latest if phys_latest > 1_000_000 else 0,
        "walk": walk_stats,
        "content_ids": len(cids),
        "missing": missing,
    }


def _table_cache_path(repo: str, mode: str) -> str:
    return os.path.join(CACHEDIR, f"table_cache_repo{repo}_{mode}.json")


def _table_snaphash(repo: str, mode: str, recent) -> str:
    srcs = sources_index(repo)
    membership, _ = split_sources(srcs, mode)
    h = hashlib.md5()
    h.update(f"v1|{recent}|{mode}".encode())
    for key in membership:
        for snap in srcs[key]:
            h.update((snap.get("id", "")).encode())
    return h.hexdigest()


def _file_counts(repo: str) -> dict[str, dict[str, int]]:
    """Per-source file counts for the dashboard: ``latest`` = files in the newest snapshot
    (fully walked), ``distinct`` = distinct paths ever seen across all snapshots."""
    from ..index.schema import read_cursor  # lazy: avoids core<-index import cycle on load
    out: dict[str, dict[str, int]] = {}
    cur = read_cursor(repo)
    try:
        for src, n in cur.execute(
            "SELECT source, count(*) FROM paths WHERE repo=? GROUP BY source", [repo]
        ).fetchall():
            out.setdefault(src, {})["distinct"] = int(n or 0)
        for src, fc in cur.execute(
            """
            SELECT source, file_count FROM indexed_snapshots i
            WHERE repo=? AND start_time = (
                SELECT max(start_time) FROM indexed_snapshots i2
                WHERE i2.repo = i.repo AND i2.source = i.source)
            """,
            [repo],
        ).fetchall():
            out.setdefault(src, {})["latest"] = int(fc or 0)
    except Exception as exc:  # noqa: BLE001 - counts are decorative; never fail the dashboard
        log.warning("repo=%s file_counts failed: %s", repo, exc)
    finally:
        cur.close()
    return out


def _read_cache(repo: str, mode: str, want_hash: str | None) -> dict | None:
    """Return the cached dashboard data. If ``want_hash`` is given, only return it when the
    version + snaphash still match (a valid serve); if None, return whatever is on disk (a
    best-effort stale serve while a build is in progress)."""
    try:
        with open(_table_cache_path(repo, mode), encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:
        return None
    if blob.get("version") != TABLE_CACHE_VERSION:
        return None
    if want_hash is not None and blob.get("hash") != want_hash:
        return None
    data = blob.get("data")
    if data is None:
        return None
    data = dict(data)
    data["cached"] = True
    return data


def _kick_bg_recompute(repo: str, recent: int | str | None, mode: str) -> None:
    """Recompute the dashboard cache in the background (single-flight). Used by serve-stale."""
    def run() -> None:
        lock = _build_lock(repo, mode)
        if not lock.acquire(blocking=False):
            return  # a build is already running
        try:
            want = _table_snaphash(repo, mode, recent)
            if _read_cache(repo, mode, want) is None:  # still stale
                _compute_table_rows(repo, recent, mode, want)
        except Exception as exc:  # noqa: BLE001
            log.warning("bg dashboard recompute repo=%s failed: %s", repo, exc)
        finally:
            lock.release()
    threading.Thread(target=run, daemon=True).start()


def table_rows(repo: str, recent: int | str | None = None, refresh: bool = False, mode: str = "all") -> dict:
    """Single-flight + serve-stale-while-revalidate dashboard build. A hash-valid cache serves
    instantly; a hash-STALE (but schema-valid) cache also serves instantly and triggers a
    background recompute - so the dashboard is never slow, freshness arrives in the background.
    Only a total cache miss (or refresh=True) computes synchronously, and even then exactly one
    thread runs build_global per (repo, mode) (concurrent callers serve cache)."""
    want_hash = _table_snaphash(repo, mode, recent)
    if not refresh:
        valid = _read_cache(repo, mode, want_hash)
        if valid is not None:
            return valid
        stale = _read_cache(repo, mode, None)  # any schema-valid cache -> serve + revalidate
        if stale is not None:
            _kick_bg_recompute(repo, recent, mode)
            stale["stale"] = True
            return stale

    lock = _build_lock(repo, mode)
    if not lock.acquire(blocking=False):
        # a build is already running: don't start another. Serve the best cache we have right now.
        existing = _read_cache(repo, mode, want_hash) or _read_cache(repo, mode, None)
        if existing is not None:
            existing["stale_build_in_progress"] = True
            return existing
        lock.acquire()  # nothing cached at all (first cold start) -> wait for the in-flight build
    try:
        # We hold the lock. A concurrent build may have finished while we waited - reuse it.
        if not refresh:
            valid = _read_cache(repo, mode, want_hash)
            if valid is not None:
                return valid
        return _compute_table_rows(repo, recent, mode, want_hash)
    finally:
        lock.release()


def _compute_table_rows(repo: str, recent: int | str | None, mode: str, want_hash: str) -> dict:
    cache_file = _table_cache_path(repo, mode)
    db = cache_db(repo)
    smap = load_sizemap(repo)
    meta = sources_meta(repo)
    nexts = load_next_snapshots(repo)  # computed next-snapshot times (subprocess mode)
    fcounts = _file_counts(repo)
    rows, elapsed, _count, union_total = build_global(repo, db, smap, recent=recent, mode=mode)
    ordered = []
    for index, key in enumerate(sorted(rows, key=lambda item: rows[item]["total"], reverse=True)):
        row = rows[key]
        stats = row["st"]
        smeta = meta.get(key) or {}
        ordered.append(
            {
                "rank": index,
                "source": key,
                "snaps": stats["count"],
                "logical_latest": stats["latest"],
                "logical_sum": stats["sum"],
                "physical": row["total"],
                "physical_latest": row["phys_latest"],
                "accum": row["total"] / row["phys_latest"] if row["phys_latest"] > 1_000_000 else 0,
                "repo_pct": 100 * row["total"] / union_total if union_total else 0,
                "freeable": row["free"],
                "dedup": stats["sum"] / row["total"] if row["total"] else 0,
                "pins": row["pins"],
                "first": stats["first"],
                "last": stats["last"],
                "first_time": stats["first_time"],
                "last_time": stats["last_time"],
                "next_snapshot": nexts.get(key) or smeta.get("nextSnapshotTime"),
                "status": smeta.get("status"),
                "files_latest": (fcounts.get(key) or {}).get("latest", 0),
                "files_distinct": (fcounts.get(key) or {}).get("distinct", 0),
            }
        )
    data = {"rows": ordered, "elapsed": elapsed, "union_total": union_total, "mode": mode, "recent": recent, "cached": False}
    try:
        with open(cache_file, "w", encoding="utf-8") as fh:
            json.dump({"version": TABLE_CACHE_VERSION, "hash": want_hash, "data": data}, fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("repo=%s table cache write failed: %s", repo, exc)
    return data
