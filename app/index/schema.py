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

_POOL: dict[str, duckdb.DuckDBPyConnection] = {}
_LOCK = threading.Lock()


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
            ordinal BIGINT
        )
        """
    )
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
    con.execute("CREATE INDEX IF NOT EXISTS files_repo_source_path ON files(repo, source, full_path)")
    con.execute("CREATE INDEX IF NOT EXISTS files_snapshot ON files(repo, snapshot_id)")
