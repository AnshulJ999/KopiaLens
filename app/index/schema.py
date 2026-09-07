"""DuckDB schema + a per-repo pooled connection for the file index.

One read-write connection per repo is opened once and shared. Reads use thread-local
cursors (``read_cursor``) - DuckDB's recommended multi-thread pattern - so concurrent
searches and the background index build coexist via MVCC (readers see a consistent
snapshot). Opening the same DuckDB file more than once per process is what previously
risked a lock conflict between a query and a running build.
"""

from __future__ import annotations

import contextlib
import gc
import json
import os
import threading
import time

import duckdb

from config import DATA_DIR, REPOS

from ..core.applog import get_logger

log = get_logger("duckdb")

_POOL: dict[str, duckdb.DuckDBPyConnection] = {}
_LOCK = threading.Lock()

# ---- Compaction gating (auto in-place compaction needs to swap the .duckdb file safely) ----
# A repo's reads go through `read_cursor`, which increments a per-repo counter and decrements on
# close. Compaction sets `_maint[repo]`, waits for the counter to hit 0 (so no query is mid-fetch),
# closes the pooled connection, swaps the file, then reopens. New reads/writes wait on `_maint`.
_MAINT_COND = threading.Condition()
_maint: dict[str, bool] = {}
_active_reads: dict[str, int] = {}

# ---- Offline windows (nightly worker) -------------------------------------------------------
# DuckDB takes an EXCLUSIVE OS file lock: while the index worker subprocess holds a repo's .duckdb,
# this process cannot open it at all - not even read-only (verified). So a worker run takes the repo
# fully offline here. Unlike a compaction swap (seconds-to-minutes, callers WAIT), a build can run
# for tens of minutes, so callers must fail fast with a clear message instead of hanging: reads
# raise RepoOffline, which the API layer turns into a 503. Kept separate from `_maint` so the
# compaction path's wait-and-retry semantics are completely unchanged.
_offline: dict[str, dict] = {}


class RepoOffline(RuntimeError):
    """Raised when a repo's DuckDB is held by the index worker. Carries when/why for the UI."""

    def __init__(self, repo: str, info: dict) -> None:
        self.repo = repo
        self.info = dict(info)
        since = float(info.get("since") or 0)
        # Human-readable, because this string reaches the UI directly from any route that catches
        # broad exceptions before the dedicated 503 handler can run. A raw epoch is not a message.
        if since:
            mins = max(0, int((time.time() - since) / 60))
            started = time.strftime("%H:%M", time.localtime(since))
            when = f"started {started}, {mins} min ago"
        else:
            when = "in progress"
        super().__init__(
            f"Repo {repo} is being indexed ({when}). It will be back shortly - "
            f"other repos are unaffected.")


def offline_status(repo: str) -> dict | None:
    """``{"reason":..., "since": epoch}`` while a repo is held by the worker, else None."""
    info = _offline.get(repo)
    return dict(info) if info else None


def _guard_offline(repo: str) -> None:
    info = _offline.get(repo)
    if info:
        raise RepoOffline(repo, info)


def _read_done(repo: str) -> None:
    with _MAINT_COND:
        _active_reads[repo] = max(0, _active_reads.get(repo, 0) - 1)
        if _active_reads[repo] == 0:
            _MAINT_COND.notify_all()


class _PooledCursor:
    """Thin proxy over a DuckDB cursor that decrements the in-flight-read counter on close()."""

    def __init__(self, repo: str, cur) -> None:
        self._repo = repo
        self._cur = cur
        self._closed = False

    def __getattr__(self, name):  # proxy execute/fetchall/fetchone/fetchmany/description/...
        return getattr(self._cur, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._cur.close()
        finally:
            _read_done(self._repo)


# Bound DuckDB's buffer pool so two open repos can't grow to ~80% of system RAM (the default).
# DuckDB spills above this to the temp directory, so it only costs disk, never correctness.
MEMORY_LIMIT = os.environ.get("KOPIALENS_DUCKDB_MEMORY", "3GB")

# Bump when a change makes an existing index WRONG rather than merely older. A mismatch triggers a
# full rebuild from the SQLite walk cache (no kopia re-walk) - there is deliberately no migration
# framework. A DB with no stamp predates stamping and is treated as current: the per-source path
# model is forward-only, so existing rows are not contaminated.
SCHEMA_VERSION = 1


def stored_schema_version(con) -> int | None:
    try:
        row = con.execute(
            "SELECT value FROM kl_meta WHERE key='schema_version' ORDER BY set_at DESC LIMIT 1"
        ).fetchone()
    except Exception:  # noqa: BLE001 - table absent on a pre-stamp DB
        return None
    try:
        return int(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def stamp_schema_version(con, version: int = SCHEMA_VERSION) -> None:
    """Append-only: kl_meta carries no index, so a rewrite can never hit the ART delete bug."""
    try:
        con.execute("INSERT INTO kl_meta(key, value) VALUES ('schema_version', ?)", [str(version)])
    except Exception as exc:  # noqa: BLE001 - never fail a build on the stamp
        log.warning("schema_version stamp failed: %s", exc)


def db_path(repo: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"kopialens_repo{repo}.duckdb")


def get_db(repo: str) -> duckdb.DuckDBPyConnection:
    """The shared read-write connection for a repo, created (and schema-initialised) once."""
    # Central repo-id guard: reject any value not in the configured set BEFORE it can reach a query
    # or open a DB file. Blocks the SQL-injection / junk-DB-file class at the chokepoint that every
    # DuckDB read+write (read_cursor -> get_db) funnels through - defense-in-depth with the
    # parameterized queries.
    if repo not in REPOS:
        raise ValueError(f"unknown repo id: {repo!r}")
    # ...and reject a repo that exists but is not DuckDB-backed. The Backblaze repo is a separate
    # read-only SQLite index; letting `repo=bz` through here would create an empty kopia DuckDB for
    # it and answer kopia-shaped questions from it.
    if REPOS[repo].get("kind", "kopia") != "kopia":
        raise ValueError(f"repo {repo!r} has no DuckDB index (kind={REPOS[repo].get('kind')!r})")
    _guard_offline(repo)  # worker holds the file: fail fast, never block for a whole build
    with _MAINT_COND:  # wait out an in-progress compaction swap (connection is being replaced)
        while _maint.get(repo):
            _MAINT_COND.wait()
    with _LOCK:
        con = _POOL.get(repo)
        if con is None:
            con = duckdb.connect(db_path(repo))
            con.execute("PRAGMA threads=4")
            tmp = os.path.join(DATA_DIR, f"duck_tmp_repo{repo}")
            os.makedirs(tmp, exist_ok=True)
            try:
                con.execute(f"PRAGMA memory_limit='{MEMORY_LIMIT}'")
                con.execute(f"PRAGMA temp_directory='{tmp}'")
                # We always ORDER BY explicitly, so insertion order is irrelevant -> let DuckDB
                # stream large sorts/aggregations instead of buffering them (was OOMing Search).
                con.execute("PRAGMA preserve_insertion_order=false")
            except Exception as exc:  # noqa: BLE001 - never block startup on a pragma
                log.warning("repo=%s memory_limit/temp_directory pragma failed: %s", repo, exc)
            log.info("repo=%s DuckDB connection opened (memory_limit=%s)", repo, MEMORY_LIMIT)
            init_schema(con)
            _POOL[repo] = con
        return con


def read_cursor(repo: str):
    """A cursor off the shared connection, for concurrent reads. Returns a proxy that MUST be
    ``.close()``d when done (callers already do) - the close decrements the in-flight-read counter
    that compaction waits on before swapping the DB file."""
    _guard_offline(repo)  # worker holds the file: fail fast, never block for a whole build
    with _MAINT_COND:  # don't start a read during a compaction swap; register this read
        while _maint.get(repo):
            _MAINT_COND.wait()
        _active_reads[repo] = _active_reads.get(repo, 0) + 1
    try:
        con = get_db(repo)
        with _LOCK:
            cur = con.cursor()
    except Exception:
        _read_done(repo)
        raise
    return _PooledCursor(repo, cur)


def write_cursor(repo: str):
    """A cursor for code that WRITES, registered with the same in-flight counter reads use.

    ``read_cursor`` was the only tracked path, but several writers took ``get_db(repo).cursor()``
    directly - the snapshot analysis persist, the refcount materialize, the in-process single-source
    build. The offline handover drains the counter and then closes the pooled connection, so an
    untracked writer could have the connection pulled out from under it mid-write. Given DuckDB's
    history in this app, that is not a race worth leaving open.

    Same contract as ``read_cursor``: the caller MUST close it.
    """
    return read_cursor(repo)


def close_all() -> None:
    with _LOCK:
        for con in _POOL.values():
            try:
                con.close()
            except Exception:
                pass
        _POOL.clear()


def _drain_timeout_default() -> float:
    """Generous by design. Ordinary queries here can run tens of seconds (the search-landing warm
    pass measured ~19s on the 18M-row repo), and losing a whole nightly run because one routine
    query was in flight is far worse than waiting a few minutes for it to finish."""
    try:
        from ..settings import load_settings
        return max(30.0, float(load_settings().get("kopialens", {}).get("index_drain_timeout", 300)))
    except Exception:  # noqa: BLE001
        return 300.0


@contextlib.contextmanager
def exclusive_offline(repo: str, reason: str = "indexing", drain_timeout: float | None = None):
    """Hand a repo's DuckDB file over to another PROCESS for the duration of the block.

    Drain, close, hand over, reopen - the file is released to an external owner, and the window is
    long, so in-process readers get ``RepoOffline`` instead of blocking. (This shared its
    choreography with the in-place compactor, removed with ``: immutable Parquet artifacts
    have no dead space to reclaim.)

    Sequence: take the dashboard build lock -> mark offline (new reads fail fast) -> wait for
    in-flight reads to drain -> CHECKPOINT + close the pooled connection -> ``gc.collect()`` so
    Windows actually releases the handle -> yield -> reopen. The connection is ALWAYS reopened,
    even if the block raises.
    """
    from ..core.metrics import _build_lock  # serialize vs dashboard build_global (lazy: import cycle)

    if drain_timeout is None:
        drain_timeout = _drain_timeout_default()
    blk = _build_lock(repo, "all")
    if not blk.acquire(timeout=drain_timeout):
        raise RuntimeError(f"repo {repo}: dashboard build still running, not taking it offline")
    try:
        with _MAINT_COND:
            if _offline.get(repo):
                raise RuntimeError(f"repo {repo} is already offline")
            _offline[repo] = {"reason": reason, "since": time.time()}
        try:
            deadline = time.time() + drain_timeout
            waited_from = time.time()
            with _MAINT_COND:
                # counts every tracked operation, reads and writes alike (see write_cursor)
                while _active_reads.get(repo, 0) > 0:
                    rem = deadline - time.time()
                    if rem <= 0:
                        outstanding = _active_reads.get(repo, 0)
                        raise TimeoutError(
                            f"repo {repo}: {outstanding} operation(s) still in flight after "
                            f"{drain_timeout:.0f}s - a long-running query is holding the database")
                    _MAINT_COND.wait(timeout=rem)
                con = _POOL.pop(repo, None)   # inside the lock: nothing may re-open it now
            waited = time.time() - waited_from
            if waited > 1:
                log.info("repo=%s waited %.0fs for in-flight queries before handover", repo, waited)
            if con is not None:
                try:
                    con.execute("CHECKPOINT")
                except Exception:  # noqa: BLE001 - best effort before handing the file over
                    pass
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
            gc.collect()  # DuckDB lags releasing the Windows file handle without this
            log.info("repo=%s offline (%s) - DuckDB handed over", repo, reason)
            yield
        finally:
            with _MAINT_COND:
                _offline.pop(repo, None)
                _MAINT_COND.notify_all()
            try:
                get_db(repo)  # reopen on the (possibly rewritten) file
                log.info("repo=%s back online", repo)
            except Exception as exc:  # noqa: BLE001
                log.error("repo=%s FAILED to reopen after offline window: %s", repo, exc)
    finally:
        blk.release()


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            repo VARCHAR,
            source VARCHAR,
            full_path VARCHAR,
            name VARCHAR,
            size UBIGINT,
            content_id VARCHAR,
            snapshot_id VARCHAR,
            start_time TIMESTAMP,
            ordinal BIGINT,
            mtime TIMESTAMP
        )
        """
    )
    try:  # migrate pre-existing files tables that lack the mtime column
        con.execute("ALTER TABLE files ADD COLUMN mtime TIMESTAMP")
    except Exception:
        pass  # column already present
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS indexed_snapshots (
            repo VARCHAR,
            snapshot_id VARCHAR,
            source VARCHAR,
            start_time TIMESTAMP,
            root_obj VARCHAR,
            indexed_at TIMESTAMP DEFAULT now(),
            file_count BIGINT,
            PRIMARY KEY (repo, snapshot_id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paths (
            repo VARCHAR,
            source VARCHAR,
            full_path VARCHAR,
            name VARCHAR,
            ext VARCHAR,
            size UBIGINT,
            versions BIGINT,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            in_latest BOOLEAN,
            content_id VARCHAR,
            physical UBIGINT,
            path_lc VARCHAR
        )
        """
    )
    for col, decl in (("content_id", "VARCHAR"), ("physical", "UBIGINT")):  # migrate older paths tables
        try:
            con.execute(f"ALTER TABLE paths ADD COLUMN {col} {decl}")
        except Exception:
            pass
    try:  # path_lc = lowercased full_path; powers the Excluded browser's fast prefix lookups
        con.execute("ALTER TABLE paths ADD COLUMN path_lc VARCHAR")
        con.execute("UPDATE paths SET path_lc = lower(full_path)")  # one-time backfill (no kopia re-walk)
        log.info("paths.path_lc added + backfilled")
    except Exception:
        pass  # column already present
    try:  # paths.exclusive: packed bytes held ONLY by this source's content. Forward-only -
          # NULL until the dashboard stage first fills it (materialize_exclusive); never a rebuild.
        con.execute("ALTER TABLE paths ADD COLUMN IF NOT EXISTS exclusive UBIGINT")
    except Exception:
        pass  # column already present

    # One-time first-seen backfill tracker (builder.backfill_first_seen). No PK/index on
    # purpose: tiny table, and per-source DELETEs must never hit DuckDB's ART index-delete bug.
    con.execute("CREATE TABLE IF NOT EXISTS first_seen_meta (repo VARCHAR, source VARCHAR, done_at TIMESTAMP)")

    # Materialized physical/dedup maps (populated from the toolkit engine artifacts each build).
    con.execute("CREATE TABLE IF NOT EXISTS content_sizes (repo VARCHAR, cid VARCHAR, packed UBIGINT)")
    con.execute("CREATE TABLE IF NOT EXISTS content_refs (repo VARCHAR, cid VARCHAR, refs BIGINT)")
    con.execute("CREATE TABLE IF NOT EXISTS indirect_phys (repo VARCHAR, oid VARCHAR, packed UBIGINT)")

    # Per-snapshot analysis (churn / physical / reclaimable) - one pre-warmed pass, persisted.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_analysis (
            repo VARCHAR,
            source VARCHAR,
            snapshot_id VARCHAR,
            churn UBIGINT,
            physical UBIGINT,
            reclaimable UBIGINT,
            snaphash VARCHAR,
            computed_at TIMESTAMP DEFAULT now()
        )
        -- No PRIMARY KEY / index on purpose: the per-source DELETE+INSERT in _persist must not
        -- delete rows out of an ART index (DuckDB's fatal index-delete bug). The table is small
        -- (~13k rows) so unindexed scans in read_analysis are cheap.
        """
    )
    # Migrate pre-existing DBs (compacted/old) whose snapshot_analysis still has the PRIMARY KEY:
    # recreate it index-free in place (DROP TABLE is wholesale, never a row-delete-from-index).
    try:
        has_pk = con.execute(
            "SELECT count(*) FROM duckdb_constraints() WHERE table_name='snapshot_analysis' "
            "AND constraint_type='PRIMARY KEY'"
        ).fetchone()[0]
        if has_pk:
            con.execute("CREATE TABLE _sa_new AS SELECT repo, source, snapshot_id, churn, physical, "
                        "reclaimable, snaphash, computed_at FROM snapshot_analysis")
            con.execute("DROP TABLE snapshot_analysis")
            con.execute("ALTER TABLE _sa_new RENAME TO snapshot_analysis")
            log.info("migrated snapshot_analysis -> dropped PRIMARY KEY (index-delete safety)")
    except Exception as exc:  # noqa: BLE001 - never block startup on the migration
        log.warning("snapshot_analysis PK migration skipped: %s", exc)

    # Index-free by design (see stamp_schema_version). Stamped only when ABSENT - an existing
    # mismatch must survive until the builder has actually rebuilt, or the signal is erased here.
    con.execute("CREATE TABLE IF NOT EXISTS kl_meta (key VARCHAR, value VARCHAR, set_at TIMESTAMP DEFAULT now())")
    stored = stored_schema_version(con)
    if stored is None:
        stamp_schema_version(con)
    elif stored != SCHEMA_VERSION:
        log.warning("index schema_version %s != %s - a full rebuild from the walk cache is due",
                    stored, SCHEMA_VERSION)

    con.execute("CREATE INDEX IF NOT EXISTS files_repo_source_path ON files(repo, source, full_path)")
    con.execute("CREATE INDEX IF NOT EXISTS files_snapshot ON files(repo, snapshot_id)")
    con.execute("CREATE INDEX IF NOT EXISTS paths_repo_source ON paths(repo, source)")
    con.execute("CREATE INDEX IF NOT EXISTS paths_content ON paths(repo, content_id)")  # copies count + shared_min
    con.execute("CREATE INDEX IF NOT EXISTS paths_path_lc ON paths(repo, path_lc)")  # Excluded browser prefix scans
    con.execute("CREATE INDEX IF NOT EXISTS content_sizes_cid ON content_sizes(repo, cid)")
    con.execute("CREATE INDEX IF NOT EXISTS content_refs_cid ON content_refs(repo, cid)")
    con.execute("CREATE INDEX IF NOT EXISTS indirect_phys_oid ON indirect_phys(repo, oid)")
