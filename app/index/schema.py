"""DuckDB schema for the KopiaLens all-version file index."""

from __future__ import annotations

import os

import duckdb

from config import DATA_DIR


def db_path(repo: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"kopialens_repo{repo}.duckdb")


def connect(repo: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path(repo), read_only=read_only)
    con.execute("PRAGMA threads=4")
    return con


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
