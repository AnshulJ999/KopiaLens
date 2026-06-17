"""DuckDB schema + a per-repo pooled connection for the file index.

One read-write connection per repo is opened once and shared. Reads use thread-local
cursors (``read_cursor``) - DuckDB's recommended multi-thread pattern - so concurrent
searches and the background index build coexist via MVCC (readers see a consistent
snapshot). Opening the same DuckDB file more than once per process is what previously
risked a lock conflict between a query and a running build.
"""

from __future__ import annotations

import os
import threading

import duckdb

from config import DATA_DIR

from ..core.applog import get_logger

log = get_logger("duckdb")

_POOL: dict[str, duckdb.DuckDBPyConnection] = {}
_LOCK = threading.Lock()

# Bound DuckDB's buffer pool so two open repos can't grow to ~80% of system RAM (the default).
# DuckDB spills above this to the temp directory, so it only costs disk, never correctness.
MEMORY_LIMIT = os.environ.get("KOPIALENS_DUCKDB_MEMORY", "3GB")


def db_path(repo: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"kopialens_repo{repo}.duckdb")


def get_db(repo: str) -> duckdb.DuckDBPyConnection:
    """The shared read-write connection for a repo, created (and schema-initialised) once."""
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


def read_cursor(repo: str) -> duckdb.DuckDBPyConnection:
    """A thread-local cursor off the shared connection, for concurrent reads.

    Caller must ``.close()`` it when done (cheap; frees the cursor, not the pool)."""
    con = get_db(repo)
    with _LOCK:
        return con.cursor()


def close_all() -> None:
    with _LOCK:
        for con in _POOL.values():
            try:
                con.close()
            except Exception:
                pass
        _POOL.clear()


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
            physical UBIGINT
        )
        """
    )
    for col, decl in (("content_id", "VARCHAR"), ("physical", "UBIGINT")):  # migrate older paths tables
        try:
            con.execute(f"ALTER TABLE paths ADD COLUMN {col} {decl}")
        except Exception:
            pass

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
            computed_at TIMESTAMP DEFAULT now(),
            PRIMARY KEY (repo, snapshot_id)
        )
        """
    )

    con.execute("CREATE INDEX IF NOT EXISTS files_repo_source_path ON files(repo, source, full_path)")
    con.execute("CREATE INDEX IF NOT EXISTS files_snapshot ON files(repo, snapshot_id)")
    con.execute("CREATE INDEX IF NOT EXISTS paths_repo_source ON paths(repo, source)")
    con.execute("CREATE INDEX IF NOT EXISTS paths_content ON paths(repo, content_id)")  # copies count + shared_min
    con.execute("CREATE INDEX IF NOT EXISTS content_sizes_cid ON content_sizes(repo, cid)")
    con.execute("CREATE INDEX IF NOT EXISTS content_refs_cid ON content_refs(repo, cid)")
    con.execute("CREATE INDEX IF NOT EXISTS indirect_phys_oid ON indirect_phys(repo, oid)")
