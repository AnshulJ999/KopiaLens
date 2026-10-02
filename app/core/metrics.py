"""Physical/freeable/dedup/accum metrics ported from ``kopiatoolkit.py``."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import threading
import time

from config import CACHEDIR

from .applog import get_logger
from .schedule import load_next_snapshots
from .sources import logical_stats, roots_of, sources_index, sources_meta

log = get_logger("metrics")

# Bump when the dashboard row schema changes so stale on-disk caches auto-invalidate
# (the cache used to be served verbatim, which is how added columns silently went blank).
TABLE_CACHE_VERSION = 2

# Single-flight: only ONE dashboard build per repo may run at a time. Concurrent callers
# (two browser tabs + the auto-refresh timer + the background warm pass) serve the existing cache
# instead of each launching their own 80s+ build_global - that pile-up was the 18 GB / 3-min storm.
_BUILD_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# Serializes the content_refs reinsert so concurrent builds can't collide (the DuckDB
# "Conflict on tuple deletion" we saw).
_REFCOUNT_WRITE_LOCK = threading.Lock()
def _build_lock(repo: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _BUILD_LOCKS.get(repo)
        if lk is None:
            lk = _BUILD_LOCKS[repo] = threading.Lock()
        return lk


def gb(value: int | float) -> float:
    return value / 1_000_000_000


def build_global(repo: str) -> tuple[dict[str, dict], float, int]:
    """Per-source physical/freeable for the dashboard, from ONE pass over the tree.

    This is's dashboard headline. What it replaces walked every source separately in
    Python, unioned the content-id sets, and cached each source's set in its own pickle so the
    second pass could reload it - 67 s and 3.3 GB on repo 1, plus hundreds of MB of pickles on
    disk that were really just a slower copy of what the index already held.

    ``free`` (content no other source reaches) now comes from the SAME reach in the same query
    rather than from a separately cached refcount, so the freeable figure can no longer be a
    generation behind the totals it sits next to.

    One behaviour note: sizes come from the generation's ``csize`` map, so content backed up since
    the last index contributes 0 rather than triggering a live re-read from kopia. Every other
    number on this page is already generation-bound, and the nightly rebuilds the dashboard
    immediately after the index.
    """
    srcs = sources_index(repo)
    start = time.time()

    from ..index import layer2  # lazy: avoids core<-index import cycle on load
    totals, union_total = layer2.source_totals(repo, None)

    rows: dict[str, dict] = {}
    for key in sorted(srcs):
        snaps = srcs[key]
        got = totals.get(key) or {}
        rows[key] = {
            "st": logical_stats(snaps),
            "total": int(got.get("total") or 0),
            "free": int(got.get("free") or 0),
            "phys_latest": int(got.get("phys_latest") or 0),
            "pins": sum(1 for snap in snaps if snap.get("pins")),
        }
    return rows, time.time() - start, union_total


def cached_refcount(repo: str) -> dict[str, int]:
    """content_id -> how many sources reach it, from the tree.

    Only the cold start uses this: every generation publishes carries a ``crefs`` map, and
    ``physical._refcount_map`` reads that. A generation from before the upgrade has none, and this
    answers until the first build replaces it. The per-mode pickle it replaces is gone - it cached
    a number the generation now owns.
    """
    from ..index import layer2  # lazy: avoids core<-index import cycle on load
    return layer2.source_refcounts(repo)


def physical_report(repo: str, source: str, recent: int | str | None = None, refresh: bool = False) -> dict:
    srcs = sources_index(repo)
    snaps = srcs[source]
    rec = int(recent) if recent else None
    stats = logical_stats(snaps[-rec:] if rec else snaps)
    from ..index import layer2        # lazy: avoids core<-index import cycle on load
    from ..index.physical import _size_map
    smap = _size_map(repo, refresh=refresh)
    cids = layer2.reach_content_ids(repo, roots_of(snaps, rec))
    missing = sum(1 for cid in cids if cid not in smap)
    if missing and not refresh:
        smap = _size_map(repo, refresh=True)
        missing = sum(1 for cid in cids if cid not in smap)
    phys_all = sum(smap.get(cid, 0) for cid in cids)
    latest_roots = roots_of(snaps[-1:])
    latest_cids = layer2.reach_content_ids(repo, latest_roots) if latest_roots else set()
    phys_latest = sum(smap.get(cid, 0) for cid in latest_cids)
    return {
        "source": source,
        "stats": stats,
        "physical_all": phys_all,
        "physical_latest": phys_latest,
        "dedup": stats["sum"] / phys_all if phys_all else 0,
        "accum": phys_all / phys_latest if phys_latest > 1_000_000 else 0,
        "content_ids": len(cids),
        "missing": missing,
    }


def _table_cache_name(repo: str) -> str:
    return f"table_cache_repo{repo}"


def _table_snaphash(repo: str) -> str:
    srcs = sources_index(repo)
    h = hashlib.md5()
    h.update(b"v2")
    for key in sorted(srcs):
        for snap in srcs[key]:
            h.update((snap.get("id", "")).encode())
    return h.hexdigest()


def _file_counts(repo: str) -> dict[str, dict[str, int]]:
    """Per-source file counts for the dashboard: ``latest`` = files in the newest snapshot
    (fully walked), ``distinct`` = distinct paths ever seen across all snapshots."""
    from ..index.store import read_cursor  # lazy: avoids core<-index import cycle on load
    out: dict[str, dict[str, int]] = {}
    cur = read_cursor(repo)
    try:
        for src, n in cur.execute(
            "SELECT s.source, count(*) FROM pathx p JOIN src s ON s.src_id = p.src_id "
            "GROUP BY s.source"
        ).fetchall():
            out.setdefault(src, {})["distinct"] = int(n or 0)
        for src, fc in cur.execute(
            """
            SELECT source, file_count FROM isnaps i
            WHERE start_time = (
                SELECT max(start_time) FROM isnaps i2 WHERE i2.source = i.source)
            """,
        ).fetchall():
            out.setdefault(src, {})["latest"] = int(fc or 0)
    except Exception as exc:  # noqa: BLE001 - counts are decorative; never fail the dashboard
        log.warning("repo=%s file_counts failed: %s", repo, exc)
    finally:
        cur.close()
    return out


def _read_cache(repo: str, want_hash: str | None) -> dict | None:
    """Return the cached dashboard data. If ``want_hash`` is given, only return it when the
    version + snaphash still match (a valid serve); if None, return whatever is on disk (a
    best-effort stale serve while a build is in progress)."""
    from ..index import sidecache  # lazy: avoids core<-index import cycle on load
    blob = sidecache.read(_table_cache_name(repo))
    if not isinstance(blob, dict):
        return None
    if blob.get("version") != TABLE_CACHE_VERSION:
        return None
    if want_hash is not None and blob.get("hash") != want_hash:
        return None
    data = blob.get("data")
    if data is None:
        return None
    data = dict(data)
    # carry the blob's snaphash so the frontend's change detection can tell a re-render-worthy
    # change from an unchanged table. The stale-serve path gets the STALE blob's own hash, which is
    # correct: it describes the data actually being served.
    data["hash"] = blob.get("hash")
    data["cached"] = True
    return data


def _kick_bg_recompute(repo: str) -> None:
    """Recompute the dashboard cache in the background (single-flight). Used by serve-stale."""
    def run() -> None:
        lock = _build_lock(repo)
        if not lock.acquire(blocking=False):
            return  # a build is already running
        try:
            want = _table_snaphash(repo)
            if _read_cache(repo, want) is None:  # still stale
                _compute_table_rows(repo, want)
        except Exception as exc:  # noqa: BLE001
            log.warning("bg dashboard recompute repo=%s failed: %s", repo, exc)
        finally:
            lock.release()
    threading.Thread(target=run, daemon=True).start()


def table_rows(repo: str, refresh: bool = False) -> dict:
    """Single-flight + serve-stale-while-revalidate dashboard build. A hash-valid cache serves
    instantly; a hash-STALE (but schema-valid) cache also serves instantly and triggers a
    background recompute - so the dashboard is never slow, freshness arrives in the background.
    Only a total cache miss (or refresh=True) computes synchronously, and even then exactly one
    thread runs build_global per repo (concurrent callers serve cache)."""
    want_hash = _table_snaphash(repo)
    if not refresh:
        valid = _read_cache(repo, want_hash)
        if valid is not None:
            return valid
        stale = _read_cache(repo, None)  # any schema-valid cache -> serve + revalidate
        if stale is not None:
            _kick_bg_recompute(repo)
            stale["stale"] = True
            return stale

    lock = _build_lock(repo)
    if not lock.acquire(blocking=False):
        # a build is already running: don't start another. Serve the best cache we have right now.
        existing = _read_cache(repo, want_hash) or _read_cache(repo, None)
        if existing is not None:
            existing["stale_build_in_progress"] = True
            return existing
        lock.acquire()  # nothing cached at all (first cold start) -> wait for the in-flight build
    try:
        # We hold the lock. A concurrent build may have finished while we waited - reuse it.
        if not refresh:
            valid = _read_cache(repo, want_hash)
            if valid is not None:
                return valid
        return _compute_table_rows(repo, want_hash)
    finally:
        lock.release()


def _compute_table_rows(repo: str, want_hash: str) -> dict:
    cache_name = _table_cache_name(repo)
    # No size map is built here any more: build_global prices straight out of the generation's
    # csize, so the multi-million-entry dict this used to materialize per dashboard build is gone.
    meta = sources_meta(repo)
    nexts = load_next_snapshots(repo)  # computed next-snapshot times (subprocess mode)
    fcounts = _file_counts(repo)
    rows, elapsed, union_total = build_global(repo)
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
    data = {"rows": ordered, "elapsed": elapsed, "union_total": union_total,
            "cached": False, "hash": want_hash,
            # computed_at covers same-hash rebuilds (e.g. a nightly rebuild that only moved
            # next_snapshot times) - the hash alone only flips when snapshot membership changes.
            "computed_at": time.time()}
    from ..index import sidecache
    sidecache.write(cache_name, {"version": TABLE_CACHE_VERSION, "hash": want_hash, "data": data})
    return data
