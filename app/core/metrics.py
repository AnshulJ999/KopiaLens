"""Physical/freeable/dedup/accum metrics ported from ``kopiatoolkit.py``."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import pickle
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

    if mode == "all" and not rec:  # persist the canonical full refcount into DuckDB
        try:
            from ..index.physical import materialize_refcount  # lazy: avoids core<-index import cycle
            from ..index.schema import get_db
            con = get_db(repo).cursor()
            try:
                materialize_refcount(con, repo, dict(count))
                con.commit()
            finally:
                con.close()
            log.info("repo=%s content_refs materialized (%d contents)", repo, len(count))
        except Exception as exc:  # noqa: BLE001
            log.warning("repo=%s content_refs materialize failed: %s", repo, exc)

    return rows, time.time() - start, dict(count), union_total


def cached_refcount(repo: str, db, smap: dict[str, int], mode: str = "all", recent: int | str | None = None):
    srcs = sources_index(repo)
    membership, _ = split_sources(srcs, mode)
    key = snaphash(srcs, membership, recent, mode)
    try:
        with open(refcount_path(repo, mode), "rb") as handle:
            cached = pickle.load(handle)
        if cached.get("hash") == key:
            return cached["count"], cached["union"]
    except Exception:
        pass
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


def table_rows(repo: str, recent: int | str | None = None, refresh: bool = False, mode: str = "all") -> dict:
    cache_file = _table_cache_path(repo, mode)
    want_hash = _table_snaphash(repo, mode, recent)
    if not refresh:                       # serve cache -> instant, but only if version + snaphash still match
        try:
            with open(cache_file, encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("version") == TABLE_CACHE_VERSION and blob.get("hash") == want_hash:
                cached = blob["data"]
                cached["cached"] = True
                return cached
        except Exception:
            pass
    db = cache_db(repo)
    smap = load_sizemap(repo, refresh=refresh)
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
