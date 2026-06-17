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

import threading
import time

import pyarrow as pa

from .physical import materialize_physical
from .schema import get_db, read_cursor
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
_PATHS_SQL = """
INSERT INTO paths (repo, source, full_path, name, ext, size, versions, first_seen, last_seen, in_latest, content_id, physical, path_lc)
SELECT repo, source, full_path, name,
       CASE WHEN contains(name, '.') THEN lower(list_extract(string_split(name, '.'), -1)) ELSE '' END AS ext,
       size, versions, first_seen, last_seen, in_latest, content_id, 0, lower(full_path)
FROM (
  SELECT repo, source, full_path, any_value(name) AS name, max(size) AS size,
         count(DISTINCT snapshot_id) AS versions, min(start_time) AS first_seen,
         max(start_time) AS last_seen, bool_or(snapshot_id = ?) AS in_latest,
         arg_max(content_id, start_time) AS content_id
  FROM files WHERE repo=? AND source=? GROUP BY repo, source, full_path
)
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


def _refresh_paths(con, repo: str, key: str) -> None:
    row = con.execute(
        "SELECT snapshot_id FROM indexed_snapshots WHERE repo=? AND source=? ORDER BY start_time DESC LIMIT 1",
        [repo, key],
    ).fetchone()
    latest_id = row[0] if row else ""
    con.execute(_PATHS_SQL, [latest_id, repo, key])


def _build_source(con, db, repo: str, key: str, snaps: list[dict], job: dict) -> None:
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
    _refresh_paths(con, repo, key)


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
