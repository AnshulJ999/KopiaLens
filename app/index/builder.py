"""Index status/summary, and the two scratch-schema helpers the build still reuses.

The walker that used to live here - kopia's directory objects descended one folder at a time, with
an accumulating ``seen_dirs`` delta and an oldest-first first-seen backfill - is gone ( step
7). Layer 3 rebuilds a source's history from the tree in one level pass, so the delta walk, the
append path and the backfill have no caller.

What remains:
  * ``_JOBS`` - the in-process job dict the worker's progress reporter reads.
  * ``_drop_indexes`` / ``_refresh_paths`` - reused by ``artifacts._build_incremental``, which owns
    the build now.
  * ``index_status`` / ``index_status_with_summary`` - the API's status reads.
"""

from __future__ import annotations

import json
import os
import threading
import time

from config import CACHEDIR

from .store import read_cursor          # status reads come from the published generation
from .schema import offline_status
from ..core import paths as pathrules
from ..core.applog import get_logger
from ..core.sources import sources_index

log = get_logger("index")

_JOBS: dict[str, dict] = {}

_FILE_INDEXES = (
    ("files_repo_source_path", "files(repo, source, full_path)"),
    ("files_snapshot", "files(repo, snapshot_id)"),
    ("paths_repo_source", "paths(repo, source)"),
)
# Recompute the paths aggregate (1 row per distinct path) for one source. content_id =
# the latest version's object id; physical is filled from the generation's maps.
# first_seen is STICKY: merged with the pre-rewrite values captured in the _old_seen temp
# table (least of old/new), because min(start_time) over the DELTA-built files table is NOT
# "first backed up" - an unchanged file only has a latest-snapshot row, so without the merge
# its first_seen would roll forward to "now" on every rebuild (the broken recently-backed-up
# filter). _old_seen must exist before this runs (artifacts builds it, or _refresh_paths with
# captured=False).
#
# `size` is the LATEST version's size (arg_max by start_time), matching content_id and the
# physical figure derived from it; max(size) across versions mis-ranked a file that shrank.
# The _old_seen join key is the SOURCE's identity expression, not an unconditional lower():
# on a case-sensitive source two case-distinct files are two paths and must not share a date.
def _paths_sql(ident: str) -> str:
    return f"""
INSERT INTO paths (repo, source, full_path, name, ext, size, versions, first_seen, last_seen, in_latest, content_id, physical, path_lc)
SELECT t.repo, t.source, t.full_path, t.name,
       CASE WHEN contains(t.name, '.') THEN lower(list_extract(string_split(t.name, '.'), -1)) ELSE '' END AS ext,
       t.size, t.versions,
       least(coalesce(o.first_seen, t.first_seen), t.first_seen) AS first_seen,
       t.last_seen, t.in_latest, t.content_id, 0, lower(t.full_path)
FROM (
  SELECT repo, source, full_path, any_value(name) AS name, arg_max(size, start_time) AS size,
         count(DISTINCT snapshot_id) AS versions, min(start_time) AS first_seen,
         max(start_time) AS last_seen, bool_or(snapshot_id = ?) AS in_latest,
         arg_max(content_id, start_time) AS content_id
  FROM files WHERE repo=? AND source=? GROUP BY repo, source, full_path
) t
LEFT JOIN _old_seen o ON o.ident = {ident}
"""



def _capture_old_seen(con, repo: str, key: str) -> None:
    """Snapshot the source's known first-seen dates BEFORE its paths rows are rewritten, so
    first_seen stays sticky (never moves forward once known). min() also collapses any
    duplicate rows from the pre-fix append path. Grouped by the source's identity expression -
    on a case-insensitive source that is lower(full_path), i.e. the old path_lc behaviour."""
    ident = pathrules.ident_sql(key)
    con.execute(
        f"CREATE OR REPLACE TEMPORARY TABLE _old_seen AS "
        f"SELECT {ident} AS ident, min(first_seen) AS first_seen FROM paths "
        f"WHERE repo=? AND source=? AND first_seen IS NOT NULL GROUP BY {ident}",
        [repo, key],
    )



def _drop_indexes(con) -> None:
    for name, _cols in _FILE_INDEXES:
        con.execute(f"DROP INDEX IF EXISTS {name}")


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
    con.execute(_paths_sql(pathrules.ident_sql(key, "t.full_path")), [latest_id, repo, key])
    con.execute("DROP TABLE IF EXISTS _old_seen")


def _has_index(repo: str) -> bool:
    """False while the repo has no published generation (never indexed) - read_cursor would
    raise FileNotFoundError, which used to turn /api/index/status into a 500."""
    from . import store  # lazy: keeps the builder's import graph unchanged
    return store.read_manifest(repo) is not None


def _nightly_progress_block(repo: str, prog: dict) -> dict:
    """The flat shape the indexing panel reads: one overall percentage and ETA, the phase, the
    counters and the stage list. ``estimated`` says whether the number came from run history."""
    run = _nightly_run_progress(repo, prog)
    pct = run.get("pct")
    if pct is None:
        pct = prog.get("pct")          # no estimate: the index stage's own REAL fraction
    return {"stage": run.get("stage") or prog.get("stage"), "pct": pct,
            "eta_seconds": run.get("eta_seconds"), "estimated": bool(run.get("estimated")),
            "stages": run.get("stages"), "phase": prog.get("phase"), "sync": prog.get("sync"),
            "seed": prog.get("seed"), "current_source": prog.get("current_source")}


def _nightly_queued_repo(repo: str) -> str | None:
    from ..core.nightly import queued_repo  # lazy: keeps core<->index imports one-directional
    try:
        return queued_repo(repo)
    except Exception:  # noqa: BLE001 - a label must never break the status route
        return None


def _nightly_run_progress(repo: str, prog: dict) -> dict:
    from ..core.nightly import run_progress  # lazy: keeps core<->index imports one-directional
    try:
        return run_progress(repo, prog)
    except Exception as exc:  # noqa: BLE001 - an estimate must never break the status route
        return {"stage": prog.get("stage"), "pct": None, "eta_seconds": None,
                "estimated": False, "stages": [], "error": str(exc)[:120]}


def _nightly_log_tail(repo: str) -> list[str]:
    from ..core.nightly import worker_log_tail  # lazy: same reason
    return worker_log_tail(repo)


def index_status(repo: str) -> dict:
    srcs = sources_index(repo)
    total = sum(len(snaps) for snaps in srcs.values())
    job = _JOBS.get(repo, {})
    if job.get("building"):
        return {
            "repo": repo,
            # the RUNNING job's own total (changed targets), not the repo-wide snapshot count -
            # the counter only advances over targets, so the repo total made every incremental
            # pass look stuck at a fraction of the bar
            "snapshots": job.get("total") or total,
            "indexed_snapshots": job.get("indexed", 0),
            "files": None, "building": True, "error": None,
            "current_source": job.get("current_source"), "targets": job.get("targets"),
        }
    # The worker subprocess cannot be queried in-process and, on Parquet, holds no exclusive
    # lock the app could detect either - so a run in flight is visible only through the nightly
    # state plus the worker's progress file. Gated on the state saying a pass is actually
    # running, so a stale progress file from a crashed run cannot fake "building". Must come
    # BEFORE read_cursor(), which would serve the previous generation as if nothing happened.
    running = offline_status(repo) is not None
    if not running:
        from ..core.nightly import state as _nightly_state  # lazy: keeps core<->index one-way
        running = bool(_nightly_state(repo).get("running"))
    if running:
        from ..core.nightly import worker_progress  # lazy: keeps core<->index imports one-directional
        prog = worker_progress(repo) or {}
        off = offline_status(repo)
        return {
            "repo": repo, "snapshots": prog.get("total") or total,
            "indexed_snapshots": prog.get("indexed", 0), "files": None,
            "building": True, "offline": off is not None,
            "offline_reason": off.get("reason") if off else None,
            "offline_since": off.get("since") if off else None,
            "stage": prog.get("stage"),
            "current_source": prog.get("current_source"), "targets": prog.get("targets"),
            "error": prog.get("error"),
            # the rest of the worker's progress, which nothing read until now: the phase
            # name, the sync/seed counters, the honest per-stage pct, the whole-run estimate and
            # the live log tail the panel renders. ``progress`` is the flat block the panel reads
            # (``indexStatus.progress``); the same numbers also ride the top level.
            "phase": prog.get("phase"), "sync": prog.get("sync"), "seed": prog.get("seed"),
            "pct": prog.get("pct"),
            "run": _nightly_run_progress(repo, prog),
            "log_tail": _nightly_log_tail(repo),
            "queued_repo": _nightly_queued_repo(repo),
            "progress": _nightly_progress_block(repo, prog),
        }
    cur = read_cursor(repo) if _has_index(repo) else None
    if cur is None:
        # A repo that has never been indexed is a normal state, not an error: the manifest is
        # simply absent. Report zeros with never_indexed=True so the UI can offer the Index
        # button instead of erroring every page (observed live on a fresh repo 3, 2026-09-13).
        return {
            "repo": repo, "snapshots": total, "indexed_snapshots": 0,
            "missing_snapshots": total, "files": 0, "last_indexed_at": None,
            "never_indexed": True, "building": False, "error": job.get("error"),
        }
    try:
        indexed = cur.execute("SELECT COUNT(*) FROM isnaps").fetchone()[0]
        files = cur.execute("SELECT COUNT(*) FROM filev").fetchone()[0]
        last = cur.execute("SELECT max(indexed_at) FROM isnaps").fetchone()[0]
    finally:
        cur.close()
    return {
        "repo": repo, "snapshots": total, "indexed_snapshots": indexed,
        "missing_snapshots": total - indexed, "files": files,
        "last_indexed_at": str(last) if last else None,
        "never_indexed": False, "building": False, "error": job.get("error"),
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


