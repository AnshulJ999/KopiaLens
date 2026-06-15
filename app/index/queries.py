"""DuckDB query layer for search, TreeSize, and time-travel views.

All reads go through a thread-local cursor off the shared pooled connection
(``read_cursor``) - no per-query file open or schema DDL.
"""

from __future__ import annotations

import os

from .schema import read_cursor

_SEP = chr(92)  # backslash path separator (avoids source-literal escaping)


def _rows(cur, sql: str, params: list | None = None) -> list[dict]:
    cur.execute(sql, params or [])
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def latest_snapshots_cte() -> str:
    return """
    WITH latest AS (
        SELECT repo, source, snapshot_id
        FROM (
            SELECT repo, source, snapshot_id, start_time,
                   row_number() OVER (PARTITION BY repo, source ORDER BY start_time DESC) AS rn
            FROM indexed_snapshots
        )
        WHERE rn = 1
    )
    """


def index_summary(repo: str) -> dict:
    cur = read_cursor(repo)
    try:
        row = cur.execute(
            """
            SELECT
                COUNT(*) AS rows,
                COUNT(DISTINCT source) AS sources,
                COUNT(DISTINCT snapshot_id) AS snapshots,
                COUNT(DISTINCT full_path) AS distinct_paths,
                SUM(size) AS logical_sum
            FROM files
            WHERE repo=?
            """,
            [repo],
        ).fetchone()
    finally:
        cur.close()
    return {
        "rows": row[0] or 0,
        "sources": row[1] or 0,
        "snapshots": row[2] or 0,
        "distinct_paths": row[3] or 0,
        "logical_sum": row[4] or 0,
    }


def search_paths(repo: str, query: str = "", deleted_only: bool = False, min_size: int = 0, limit: int = 200, confirm_exists: bool = False) -> list[dict]:
    cur = read_cursor(repo)
    where = ["f.repo = ?"]
    params: list = [repo]
    if query:
        where.append("f.full_path ILIKE ?")
        params.append(f"%{query}%")
    if min_size:
        where.append("f.size >= ?")
        params.append(min_size)
    having = "HAVING in_latest = false" if deleted_only else ""
    params.append(limit)
    try:
        rows = _rows(
            cur,
            latest_snapshots_cte()
            + f"""
            SELECT
                f.source,
                f.full_path,
                any_value(f.name) AS name,
                max(f.size) AS size,
                count(DISTINCT f.snapshot_id) AS versions,
                min(f.start_time) AS first_seen,
                max(f.start_time) AS last_seen,
                bool_or(f.snapshot_id = latest.snapshot_id) AS in_latest
            FROM files f
            LEFT JOIN latest ON latest.repo=f.repo AND latest.source=f.source
            WHERE {" AND ".join(where)}
            GROUP BY f.source, f.full_path
            {having}
            ORDER BY size DESC, last_seen DESC
            LIMIT ?
            """,
            params,
        )
    finally:
        cur.close()
    if confirm_exists:
        for row in rows:
            row["on_disk"] = os.path.exists(row["full_path"])
    return rows


def versions(repo: str, source: str, full_path: str) -> list[dict]:
    cur = read_cursor(repo)
    try:
        return _rows(
            cur,
            """
            SELECT snapshot_id, start_time, size, content_id
            FROM files
            WHERE repo=? AND source=? AND full_path=?
            ORDER BY start_time DESC
            """,
            [repo, source, full_path],
        )
    finally:
        cur.close()


def largest_files(repo: str, limit: int = 200) -> list[dict]:
    return search_paths(repo, query="", deleted_only=False, min_size=0, limit=limit)


def tree_children(repo: str, source: str, snapshot_id: str, prefix: str = "") -> list[dict]:
    """Immediate children under ``prefix`` for one snapshot, aggregated in SQL."""
    base = prefix.rstrip(_SEP)
    like = base + _SEP + "%" if base else "%"
    cur = read_cursor(repo)
    try:
        rows = _rows(
            cur,
            """
            WITH sub AS (
                SELECT size,
                       CASE WHEN ? = '' THEN full_path
                            ELSE substr(full_path, length(?) + 2) END AS remainder
                FROM files
                WHERE repo = ? AND source = ? AND snapshot_id = ? AND full_path LIKE ?
            )
            SELECT split_part(remainder, ?, 1) AS name,
                   sum(size) AS size,
                   count(*) AS files,
                   max(CASE WHEN position(? IN remainder) > 0 THEN 1 ELSE 0 END) AS is_folder
            FROM sub
            WHERE remainder <> ''
            GROUP BY name
            ORDER BY size DESC
            """,
            [base, base, repo, source, snapshot_id, like, _SEP, _SEP],
        )
    finally:
        cur.close()
    children = []
    for r in rows:
        name = r["name"]
        children.append(
            {
                "path": (base + _SEP + name) if base else name,
                "name": name,
                "size": r["size"] or 0,
                "files": r["files"],
                "type": "folder" if r["is_folder"] else "file",
            }
        )
    return children


def snapshots_for_path(repo: str, source: str, full_path: str) -> list[str]:
    return [row["snapshot_id"] for row in versions(repo, source, full_path)]
