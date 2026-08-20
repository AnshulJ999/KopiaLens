"""DuckDB index builder - incremental, self-healing.

Delta strategy per source: index snapshots NEWEST-first with an accumulating
``seen_dirs`` set. The latest snapshot is indexed fully (powers the tree +
``in_latest``/deleted-from-disk); older snapshots emit only their UNIQUE files. Each
unique directory-OID is walked once, so the index is ~one row per file-version. Reads
the warm sqlite dir cache (no kopia re-walk).

Incremental & self-healing: a normal build only rebuilds sources whose snapshot SET
changed since last index, and drops sources that no longer exist - so the index always
reflects the current repo without re-doing unchanged sources. ``full=True`` forces a
rebuild of everything. After each source is (re)built, its rows in the ``paths``
aggregate table (one row per distinct path) are recomputed - that table powers instant
search. Runs in a background thread.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pyarrow as pa

from config import CACHEDIR

from .physical import materialize_physical
from .schema import get_db, offline_status, read_cursor
from ..core.applog import get_logger
from ..core.cache import cache_db
from ..core.sources import sources_index
from ..core.walk import walk_new_visit

log = get_logger("index")

_JOBS: dict[str, dict] = {}

_FLUSH_ROWS = 50_000
_INSERT_SQL = (
    "INSERT INTO files SELECT repo, source, full_path, name, size, content_id, "
    "snapshot_id, CAST(start_time AS TIMESTAMP), ordinal, CAST(mtime AS TIMESTAMP) FROM _ingest"
)
_FILE_INDEXES = (
    ("files_repo_source_path", "files(repo, source, full_path)"),
    ("files_snapshot", "files(repo, snapshot_id)"),
    ("paths_repo_source", "paths(repo, source)"),
)
# Recompute the paths aggregate (1 row per distinct path) for one source. content_id =
# the latest version's object id; physical is filled later by materialize_physical().
# first_seen is STICKY: merged with the pre-rewrite values captured in the _old_seen temp
# table (least of old/new), because min(start_time) over the DELTA-built files table is NOT
# "first backed up" - an unchanged file only has a latest-snapshot row, so without the merge
# its first_seen would roll forward to "now" on every rebuild (the broken recently-backed-up
# filter). _old_seen must exist before this runs (see _capture_old_seen / _refresh_paths).
_PATHS_SQL = """
INSERT INTO paths (repo, source, full_path, name, ext, size, versions, first_seen, last_seen, in_latest, content_id, physical, path_lc)
SELECT t.repo, t.source, t.full_path, t.name,
       CASE WHEN contains(t.name, '.') THEN lower(list_extract(string_split(t.name, '.'), -1)) ELSE '' END AS ext,
       t.size, t.versions,
       least(coalesce(o.first_seen, t.first_seen), t.first_seen) AS first_seen,
       t.last_seen, t.in_latest, t.content_id, 0, lower(t.full_path)
FROM (
  SELECT repo, source, full_path, any_value(name) AS name, max(size) AS size,
         count(DISTINCT snapshot_id) AS versions, min(start_time) AS first_seen,
         max(start_time) AS last_seen, bool_or(snapshot_id = ?) AS in_latest,
         arg_max(content_id, start_time) AS content_id
  FROM files WHERE repo=? AND source=? GROUP BY repo, source, full_path
) t
LEFT JOIN _old_seen o ON o.path_lc = lower(t.full_path)
"""


def _source_root(source: str) -> str:
    return source.split(":", 1)[1] if ":" in source else source


def _full_path(source: str, relative: str) -> str:
    root = _source_root(source)
    rel = relative.lstrip("\\/")
    if not rel:
        return root
    if root.endswith("\\") or root.endswith("/"):
        return root + rel
    return root + "\\" + rel


def _flush(con, rows: list[tuple]) -> None:
    """Bulk-insert a row batch via an Arrow table (~150x faster than executemany)."""
    if not rows:
        return
    c = list(zip(*rows))
    tbl = pa.table({
        "repo": c[0], "source": c[1], "full_path": c[2], "name": c[3],
        "size": c[4], "content_id": c[5], "snapshot_id": c[6], "start_time": c[7], "ordinal": c[8],
        "mtime": c[9],
    })
    con.register("_ingest", tbl)
    con.execute(_INSERT_SQL)
    con.unregister("_ingest")
    rows.clear()


def _drop_indexes(con) -> None:
    for name, _cols in _FILE_INDEXES:
        con.execute(f"DROP INDEX IF EXISTS {name}")


def _create_indexes(con) -> None:
    for name, cols in _FILE_INDEXES:
        con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {cols}")


def _delete_source(con, repo: str, key: str) -> None:
    con.execute("DELETE FROM files WHERE repo=? AND source=?", [repo, key])
    con.execute("DELETE FROM indexed_snapshots WHERE repo=? AND source=?", [repo, key])
    con.execute("DELETE FROM paths WHERE repo=? AND source=?", [repo, key])


def _capture_old_seen(con, repo: str, key: str) -> None:
    """Snapshot the source's known first-seen dates BEFORE its paths rows are rewritten, so
    first_seen stays sticky (never moves forward once known). min() also collapses any
    duplicate rows from the pre-fix append path."""
    con.execute(
        "CREATE OR REPLACE TEMPORARY TABLE _old_seen AS "
        "SELECT path_lc, min(first_seen) AS first_seen FROM paths "
        "WHERE repo=? AND source=? AND first_seen IS NOT NULL GROUP BY path_lc",
        [repo, key],
    )


def _refresh_paths(con, repo: str, key: str, captured: bool = False) -> None:
    """Rewrite the paths aggregate for one source: capture old first_seen (unless the caller
    already did), DELETE the source's rows, re-INSERT merged with the captured dates.
    The DELETE here is essential: the append fast-path used to skip it, so every append
    INSERTed a full extra copy of the source's paths (rows duplicated up to ~18x in the wild).
    Per-source DELETE against paths' ART indexes is the same pattern _delete_source has always
    run daily (never hit the DuckDB index-delete bug, unlike the full-table map rewrites)."""
    if not captured:
        _capture_old_seen(con, repo, key)
    row = con.execute(
        "SELECT snapshot_id FROM indexed_snapshots WHERE repo=? AND source=? ORDER BY start_time DESC LIMIT 1",
        [repo, key],
    ).fetchone()
    latest_id = row[0] if row else ""
    con.execute("DELETE FROM paths WHERE repo=? AND source=?", [repo, key])
    con.execute(_PATHS_SQL, [latest_id, repo, key])
    con.execute("DROP TABLE IF EXISTS _old_seen")


def _build_source(con, db, repo: str, key: str, snaps: list[dict], job: dict) -> None:
    _capture_old_seen(con, repo, key)  # before the delete, so first_seen survives the rebuild
    _delete_source(con, repo, key)
    seen_dirs: set[str] = set()
    snaps_sorted = sorted(snaps, key=lambda s: s.get("startTime", ""), reverse=True)  # newest first
    rows: list[tuple] = []
    for snap in snaps_sorted:
        root = snap.get("rootEntry", {}).get("obj")
        snap_id = snap["id"]
        start = snap.get("startTime", "") or None
        if not root:
            job["indexed"] += 1
            continue
        count = [0]

        def visitor(parent: str, entry: dict, _key=key, _sid=snap_id, _start=start) -> None:
            rel = (parent + "\\" + entry.get("name", "")).lstrip("\\")
            rows.append((
                repo, _key, _full_path(_key, rel), entry.get("name", ""),
                int(entry.get("size", 0) or 0), entry.get("obj", ""), _sid, _start, count[0],
                entry.get("mtime") or None,
            ))
            count[0] += 1
            if len(rows) >= _FLUSH_ROWS:
                _flush(con, rows)

        walk_new_visit(repo, db, [root], seen_dirs, visitor)
        con.execute(
            "INSERT OR REPLACE INTO indexed_snapshots(repo, snapshot_id, source, start_time, root_obj, file_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [repo, snap_id, key, start, root, count[0]],
        )
        job["indexed"] += 1
    _flush(con, rows)
    _refresh_paths(con, repo, key, captured=True)  # _old_seen captured before _delete_source


def _append_set(snaps: list[dict], indexed_ids: set[str]) -> list[dict] | None:
    """Append-only eligibility: return the new snapshots IFF the ONLY change to this source is
    snapshots strictly NEWER than every already-indexed one (the daily-snapshot case). Returns
    None for anything else (a removed/rewritten snapshot id - e.g. remove-files - or a new
    snapshot landing mid-timeline) so the caller falls back to the safe full-source rebuild.
    ISO-8601 startTime strings compare chronologically as plain strings."""
    if not indexed_ids:
        return None
    cur_ids = {s["id"] for s in snaps}
    if not indexed_ids <= cur_ids:
        return None  # an indexed snapshot vanished/was rewritten -> rebuild the source
    new_ids = cur_ids - indexed_ids
    if not new_ids:
        return None
    max_indexed_start = max((s.get("startTime", "") or "" for s in snaps if s["id"] in indexed_ids), default="")
    new_snaps = [s for s in snaps if s["id"] in new_ids]
    if any((s.get("startTime", "") or "") <= max_indexed_start for s in new_snaps):
        return None  # a new snapshot is not strictly the newest -> rebuild (keeps the delta sound)
    return new_snaps


def _extend_source(con, db, repo: str, key: str, snaps: list[dict], new_snaps: list[dict], job: dict) -> int:
    """Append-only fast path: walk only the new (strictly newest) snapshots + re-walk the previous
    latest as a delta, and KEEP every older snapshot's rows untouched. This preserves the exact
    newest-full / older-delta invariant of a full rebuild: adding newer snapshots on top does not
    change an older snapshot's unique-content set. Returns the number of snapshots actually walked."""
    snaps_sorted = sorted(snaps, key=lambda s: s.get("startTime", "") or "", reverse=True)  # newest first
    new_ids = {s["id"] for s in new_snaps}
    prev_latest = next((s for s in snaps_sorted if s["id"] not in new_ids), None)
    rewalk = [s for s in snaps_sorted if s["id"] in new_ids or (prev_latest and s["id"] == prev_latest["id"])]
    ids = [s["id"] for s in rewalk]
    if ids:  # drop only the rows we will rewrite: prev_latest changes full->delta; new ones have none yet
        placeholders = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM files WHERE repo=? AND source=? AND snapshot_id IN ({placeholders})", [repo, key, *ids])
        con.execute(f"DELETE FROM indexed_snapshots WHERE repo=? AND snapshot_id IN ({placeholders})", [repo, *ids])
    seen_dirs: set[str] = set()
    rows: list[tuple] = []
    for snap in rewalk:  # newest-first so seen_dirs accumulates correctly (new snaps, then prev_latest)
        root = snap.get("rootEntry", {}).get("obj")
        snap_id = snap["id"]
        start = snap.get("startTime", "") or None
        if not root:
            job["indexed"] += 1
            continue
        count = [0]

        def visitor(parent: str, entry: dict, _key=key, _sid=snap_id, _start=start) -> None:
            rel = (parent + "\\" + entry.get("name", "")).lstrip("\\")
            rows.append((
                repo, _key, _full_path(_key, rel), entry.get("name", ""),
                int(entry.get("size", 0) or 0), entry.get("obj", ""), _sid, _start, count[0],
                entry.get("mtime") or None,
            ))
            count[0] += 1
            if len(rows) >= _FLUSH_ROWS:
                _flush(con, rows)

        walk_new_visit(repo, db, [root], seen_dirs, visitor)
        con.execute(
            "INSERT OR REPLACE INTO indexed_snapshots(repo, snapshot_id, source, start_time, root_obj, file_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [repo, snap_id, key, start, root, count[0]],
        )
        job["indexed"] += 1
    _flush(con, rows)
    _refresh_paths(con, repo, key)
    job["indexed"] += max(0, len(snaps) - len(rewalk))  # older snaps stay indexed -> keep progress honest
    return len(rewalk)


def _indexed_sets(con, repo: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for src, sid in con.execute(
        "SELECT source, snapshot_id FROM indexed_snapshots WHERE repo=?", [repo]
    ).fetchall():
        out.setdefault(src, set()).add(sid)
    return out


def _backfill_source_first_seen(con, db, repo: str, key: str, snaps: list[dict]) -> int:
    """One-time EXACT first_seen for a source: walk its snapshots OLDEST-first with a fresh
    seen_dirs set, so each unique dir version is attributed to the EARLIEST snapshot containing
    it - stamping every path with the earliest surviving snapshot that holds it. The delta index
    alone cannot know this (an unchanged file only has a latest-snapshot row). Cost = one full
    source walk (reads the warm sqlite dir cache; no kopia calls when warm)."""
    seen_dirs: set[str] = set()
    first: dict[str, str] = {}
    for snap in sorted(snaps, key=lambda s: s.get("startTime", "") or ""):  # OLDEST first
        root = snap.get("rootEntry", {}).get("obj")
        start = snap.get("startTime", "") or None
        if not root or not start:
            continue

        def visitor(parent: str, entry: dict, _start=start) -> None:
            rel = (parent + "\\" + entry.get("name", "")).lstrip("\\")
            plc = _full_path(key, rel).lower()
            if plc not in first:  # oldest-first walk -> the first emission IS the earliest snapshot
                first[plc] = _start

        walk_new_visit(repo, db, [root], seen_dirs, visitor)
    if not first:
        return 0
    tbl = pa.table({"path_lc": list(first.keys()), "fs": list(first.values())})
    con.register("_fs_arrow", tbl)
    con.execute(
        "CREATE OR REPLACE TEMPORARY TABLE _fs_backfill AS "
        "SELECT path_lc, CAST(fs AS TIMESTAMP) AS fs FROM _fs_arrow"
    )
    con.unregister("_fs_arrow")
    con.execute(  # targeted: only rows whose stored date is LATER than the true earliest
        "UPDATE paths SET first_seen = b.fs FROM _fs_backfill b "
        "WHERE paths.repo=? AND paths.source=? AND paths.path_lc = b.path_lc AND b.fs < paths.first_seen",
        [repo, key],
    )
    con.execute("DROP TABLE IF EXISTS _fs_backfill")
    return len(first)


def backfill_first_seen(con, db, repo: str, job: dict) -> int:
    """One-time per-source first-seen backfill + duplicate-row cleanup, tracked in
    first_seen_meta. Survives normal rebuilds (sticky first_seen in _refresh_paths preserves
    the backfilled dates) and reruns automatically after a from-scratch DB rebuild. The
    _refresh_paths call also rewrites the source's paths cleanly = the dedupe migration for
    the append-path duplicate-rows bug. Returns the number of sources processed."""
    srcs = sources_index(repo)
    done = {r[0] for r in con.execute("SELECT source FROM first_seen_meta WHERE repo=?", [repo]).fetchall()}
    todo = [k for k in sorted(srcs) if k not in done]
    for key in todo:
        job["current_source"] = f"(first-seen backfill) {key}"
        t0 = time.perf_counter()
        _refresh_paths(con, repo, key)  # dedupe + sticky rewrite
        n = _backfill_source_first_seen(con, db, repo, key, srcs[key])
        con.execute("INSERT INTO first_seen_meta VALUES (?, ?, now())", [repo, key])
        con.commit()
        log.info("repo=%s source=%s first-seen backfill %d paths %.1fs", repo, key, n, time.perf_counter() - t0)
    return len(todo)


def _run_build(repo: str, source: str | None, full: bool) -> None:
    job = _JOBS[repo]
    con = None
    try:
        srcs = sources_index(repo)
        con = get_db(repo).cursor()  # build writes via its own cursor off the shared pool
        db = cache_db(repo)
        indexed = _indexed_sets(con, repo)
        have_paths = {r[0] for r in con.execute("SELECT DISTINCT source FROM paths WHERE repo=?", [repo]).fetchall()}
        if source:
            targets = [source] if source in srcs else []
        elif full:
            targets = sorted(srcs)
        else:  # incremental: sources whose snapshot SET changed, or that lack the paths aggregate (migration)
            changed = {k for k in srcs if {s["id"] for s in srcs[k]} != indexed.get(k, set())}
            migrate = (set(indexed) & set(srcs)) - have_paths  # indexed but no paths rows yet
            targets = sorted(changed | migrate)
        stale = [k for k in indexed if k not in srcs]  # self-heal: removed sources

        job["total"] = sum(len(srcs[k]) for k in targets if k in srcs)
        job["indexed"] = 0
        job["targets"] = len(targets)
        if full:
            _drop_indexes(con)
        for key in stale:
            _delete_source(con, repo, key)
            con.execute("DELETE FROM first_seen_meta WHERE repo=? AND source=?", [repo, key])
        for key in targets:
            job["current_source"] = key
            t0 = time.perf_counter()
            # Append-only fast path for the common daily-snapshot case (only when the source already
            # has a paths aggregate); anything else (remove-files rewrites, mid-timeline snapshots,
            # migration) falls back to the safe full-source rebuild.
            append_set = _append_set(srcs[key], indexed.get(key, set())) if (not full and key in have_paths) else None
            if append_set:
                walked = _extend_source(con, db, repo, key, srcs[key], append_set, job)
                mode_used = f"append(+{len(append_set)} new, walked {walked})"
            else:
                _build_source(con, db, repo, key, srcs[key], job)
                mode_used = "rebuild"
            log.info("repo=%s source=%s %s %d snaps %.1fs", repo, key, mode_used, len(srcs[key]), time.perf_counter() - t0)
        # Physical/dedup materialization only when something actually changed - skipping it on
        # no-op incremental passes is the main fix for the periodic idle disk churn (it used to
        # run an UPDATE over the whole paths table every cycle even when nothing was rebuilt).
        if targets or stale or full:
            job["current_source"] = "(physical maps)"
            materialize_physical(con, repo)
        con.commit()
        try:  # one-time per source (first_seen_meta); a cheap no-op query once populated
            if backfill_first_seen(con, db, repo, job):
                job["current_source"] = "(physical maps)"
                materialize_physical(con, repo)  # backfill rewrote paths rows with physical=0 -> refill
                con.commit()
        except Exception as exc:  # noqa: BLE001 - the backfill must never fail a build
            log.warning("first-seen backfill repo=%s failed: %s", repo, exc)
        if targets or stale or full:  # only log when real work happened (no spam on no-op passes)
            try:
                from ..core.instrument import log_memory
                log_memory(f"post-index repo={repo} targets={len(targets)} stale={len(stale)} full={full}")
            except Exception:  # noqa: BLE001 - instrumentation must never break a build
                pass
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        job["error"] = str(exc)
    finally:
        if con is not None:
            try:
                if full:
                    _create_indexes(con)
                con.commit()
            except Exception:
                pass
            con.close()
        job["building"] = False
        job["finished_at"] = time.time()


def index_status(repo: str) -> dict:
    srcs = sources_index(repo)
    total = sum(len(snaps) for snaps in srcs.values())
    job = _JOBS.get(repo, {})
    if job.get("building"):
        return {
            "repo": repo, "snapshots": total,
            "indexed_snapshots": job.get("indexed", 0),
            "files": None, "building": True, "error": None,
            "current_source": job.get("current_source"), "targets": job.get("targets"),
        }
    # The index worker subprocess holds the DuckDB, so no query is possible here - report from its
    # progress file instead. Must come BEFORE read_cursor(), which would raise RepoOffline.
    off = offline_status(repo)
    if off:
        from ..core.nightly import worker_progress  # lazy: keeps core<->index imports one-directional
        prog = worker_progress(repo) or {}
        return {
            "repo": repo, "snapshots": total,
            "indexed_snapshots": prog.get("indexed", 0), "files": None,
            "building": True, "offline": True, "offline_reason": off.get("reason"),
            "offline_since": off.get("since"), "stage": prog.get("stage"),
            "current_source": prog.get("current_source"), "targets": prog.get("targets"),
            "error": prog.get("error"),
        }
    cur = read_cursor(repo)
    try:
        indexed = cur.execute("SELECT COUNT(*) FROM indexed_snapshots WHERE repo=?", [repo]).fetchone()[0]
        files = cur.execute("SELECT COUNT(*) FROM files WHERE repo=?", [repo]).fetchone()[0]
        last = cur.execute("SELECT max(indexed_at) FROM indexed_snapshots WHERE repo=?", [repo]).fetchone()[0]
    finally:
        cur.close()
    return {
        "repo": repo, "snapshots": total, "indexed_snapshots": indexed,
        "missing_snapshots": total - indexed, "files": files,
        "last_indexed_at": str(last) if last else None,
        "building": False, "error": job.get("error"),
    }


# ---- index_summary serve-stale cache (Pass E) ------------------------------------------
# Measured (2026-07-10, warm app): /api/index/status took 3.8-4.6s on repo1 (18M-row files table),
# 2.1s on repo2 - the ONLY slow call in the whole Kopia page-load path (everything else <10ms).
# The cost is index_summary()'s COUNT(DISTINCT source/snapshot_id/full_path) + SUM(size) over the
# whole `files` table - DISTINCT aggregation can't use a zonemap shortcut like a plain COUNT(*),
# so it touches every row. index_status()'s own 3 counts are cheap and stay live/uncached; only the
# summary is cached here, serve-stale-while-revalidate + disk-persisted (like table_rows), so a
# page load never blocks on this after the very first one.
_STATUS_CACHE: dict[str, dict] = {}
_STATUS_LOCK = threading.Lock()
_STATUS_BUILD_LOCKS: dict[str, threading.Lock] = {}
_STATUS_TTL = 300  # 5 min


def _status_cache_path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"index_summary_repo{repo}.json")


def _read_status_cache(repo: str) -> dict | None:
    entry = _STATUS_CACHE.get(repo)
    if entry is not None:
        return entry
    try:
        with open(_status_cache_path(repo), encoding="utf-8") as fh:
            entry = json.load(fh)
    except Exception:
        return None
    _STATUS_CACHE[repo] = entry
    return entry


def _write_status_cache(repo: str, summary: dict) -> None:
    entry = {"summary": summary, "ts": time.time()}
    _STATUS_CACHE[repo] = entry
    try:
        path = _status_cache_path(repo)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - never fail the request on cache-write housekeeping
        log.warning("repo=%s index_summary cache write failed: %s", repo, exc)


def _status_build_lock(repo: str) -> threading.Lock:
    with _STATUS_LOCK:
        lock = _STATUS_BUILD_LOCKS.get(repo)
        if lock is None:
            lock = _STATUS_BUILD_LOCKS[repo] = threading.Lock()
        return lock


def _kick_status_recompute(repo: str) -> None:
    def run() -> None:
        lock = _status_build_lock(repo)
        if not lock.acquire(blocking=False):
            return  # a recompute is already in flight
        try:
            from .queries import index_summary
            _write_status_cache(repo, index_summary(repo))
        except Exception as exc:  # noqa: BLE001
            log.warning("bg index_summary recompute repo=%s failed: %s", repo, exc)
        finally:
            lock.release()
    threading.Thread(target=run, daemon=True).start()


def index_status_with_summary(repo: str) -> dict:
    """index_status() (cheap, always live) + a cached index_summary() (expensive - see above).
    A fresh cache serves instantly; a stale-but-present cache also serves instantly and kicks a
    background recompute; only a true first-ever call (nothing cached yet, e.g. right after a
    schema change) blocks. Persisted to disk so a cold app restart also serves instantly instead
    of every browser tab blocking on its own synchronous recompute."""
    status = index_status(repo)
    if status.get("building"):
        return status  # building already short-circuits before any DB query; summary would be moot
    cached = _read_status_cache(repo)
    if cached is not None:
        status["summary"] = cached["summary"]
        if time.time() - cached["ts"] >= _STATUS_TTL:
            _kick_status_recompute(repo)
        return status
    lock = _status_build_lock(repo)
    with lock:
        cached = _read_status_cache(repo)  # a concurrent caller may have just computed it
        if cached is None:
            from .queries import index_summary
            summary = index_summary(repo)
            _write_status_cache(repo, summary)
            cached = _STATUS_CACHE[repo]
    status["summary"] = cached["summary"]
    return status


def start_build(repo: str, source: str | None = None, full: bool = False) -> dict:
    """Kick off a background index build. Default = incremental (only changed sources);
    ``full=True`` rebuilds everything; ``source`` rebuilds one source. Poll index_status."""
    if _JOBS.get(repo, {}).get("building"):
        return {"started": False, "reason": "already building", "repo": repo}
    _JOBS[repo] = {"building": True, "indexed": 0, "total": 0, "error": None, "current_source": None}
    threading.Thread(target=_run_build, args=(repo, source, full), daemon=True).start()
    return {"started": True, "repo": repo, "source": source, "full": full}


def auto_reindex(repo: str) -> None:
    """Background reindex (self-heal). When many indexed sources lack the paths aggregate, or
    paths predate the content_id/physical columns (a schema migration), do a full rebuild
    (drops indexes = faster); otherwise incremental - a no-op-fast pass when nothing changed."""
    cur = read_cursor(repo)
    try:
        nfiles_src = cur.execute("SELECT count(DISTINCT source) FROM files WHERE repo=?", [repo]).fetchone()[0]
        npaths_src = cur.execute("SELECT count(DISTINCT source) FROM paths WHERE repo=?", [repo]).fetchone()[0]
        null_cid = cur.execute("SELECT count(*) FROM paths WHERE repo=? AND content_id IS NULL", [repo]).fetchone()[0]
    finally:
        cur.close()
    start_build(repo, full=(nfiles_src - npaths_src > 5 or null_cid > 0))


def _needs_full(repo: str) -> bool:
    """Migration detector shared by the threaded and synchronous reindex entry points: many indexed
    sources with no paths aggregate, or paths predating the content_id column."""
    cur = read_cursor(repo)
    try:
        nfiles_src = cur.execute("SELECT count(DISTINCT source) FROM files WHERE repo=?", [repo]).fetchone()[0]
        npaths_src = cur.execute("SELECT count(DISTINCT source) FROM paths WHERE repo=?", [repo]).fetchone()[0]
        null_cid = cur.execute("SELECT count(*) FROM paths WHERE repo=? AND content_id IS NULL", [repo]).fetchone()[0]
    finally:
        cur.close()
    return bool(nfiles_src - npaths_src > 5 or null_cid > 0)


def build_sync(repo: str, source: str | None = None, full: bool = False) -> dict:
    """Run a build to completion IN THIS THREAD (no background thread, no polling).

    ``start_build`` returns immediately and the caller polls ``index_status``; that is right for the
    web UI but wrong for the index worker subprocess, which has nothing else to do and must not exit
    before the build finishes. Same ``_run_build`` underneath, so behaviour is identical - only the
    threading differs. Raises on a build error rather than parking it in the job dict, so the worker
    reports a real failure."""
    _JOBS[repo] = {"building": True, "indexed": 0, "total": 0, "error": None, "current_source": None}
    try:
        _run_build(repo, source, full)
    finally:
        _JOBS.get(repo, {})["building"] = False
    job = _JOBS.get(repo, {})
    if job.get("error"):
        raise RuntimeError(job["error"])
    return {"repo": repo, "full": full, "indexed": job.get("indexed", 0), "targets": job.get("targets")}


def auto_reindex_sync(repo: str, full: bool = False) -> dict:
    """Synchronous twin of ``auto_reindex`` for the worker subprocess: same full-vs-incremental
    decision, but runs to completion instead of spawning a thread."""
    return build_sync(repo, full=bool(full) or _needs_full(repo))
