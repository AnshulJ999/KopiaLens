"""DuckDB query layer for search, TreeSize, and time-travel views."""

from __future__ import annotations

import os

from .schema import connect, init_schema


def _rows(con, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
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
    con = connect(repo, read_only=False)
    init_schema(con)
    row = con.execute(
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
    con.close()
    return {
        "rows": row[0] or 0,
        "sources": row[1] or 0,
        "snapshots": row[2] or 0,
        "distinct_paths": row[3] or 0,
        "logical_sum": row[4] or 0,
    }


def search_paths(repo: str, query: str = "", deleted_only: bool = False, min_size: int = 0, limit: int = 200, confirm_exists: bool = False) -> list[dict]:
    con = connect(repo, read_only=False)
    init_schema(con)
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
    rows = _rows(
        con,
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
    con.close()
    if confirm_exists:
        for row in rows:
            row["on_disk"] = os.path.exists(row["full_path"])
    return rows


def versions(repo: str, source: str, full_path: str) -> list[dict]:
    con = connect(repo, read_only=False)
    init_schema(con)
    rows = _rows(
        con,
        """
        SELECT snapshot_id, start_time, size, content_id
        FROM files
        WHERE repo=? AND source=? AND full_path=?
        ORDER BY start_time DESC
        """,
        [repo, source, full_path],
    )
    con.close()
    return rows


def largest_files(repo: str, limit: int = 200) -> list[dict]:
    return search_paths(repo, query="", deleted_only=False, min_size=0, limit=limit)


def tree_children(repo: str, source: str, snapshot_id: str, prefix: str = "") -> list[dict]:
    con = connect(repo, read_only=False)
    init_schema(con)
    base = prefix.rstrip("\\")
    like = base + "\\%" if base else "%"
    rows = _rows(
        con,
        """
        SELECT full_path, size
        FROM files
        WHERE repo=? AND source=? AND snapshot_id=? AND full_path LIKE ?
        """,
        [repo, source, snapshot_id, like],
    )
    con.close()

    children: dict[str, dict] = {}
    for row in rows:
        path = row["full_path"]
        remainder = path[len(base) :].lstrip("\\") if base else path
        if not remainder:
            continue
        head, sep, _tail = remainder.partition("\\")
        child_path = f"{base}\\{head}" if base else head
        item = children.setdefault(child_path, {"path": child_path, "name": head, "size": 0, "files": 0, "type": "folder" if sep else "file"})
        item["size"] += row["size"] or 0
        item["files"] += 1
        if sep:
            item["type"] = "folder"
    return sorted(children.values(), key=lambda item: item["size"], reverse=True)


def snapshots_for_path(repo: str, source: str, full_path: str) -> list[str]:
    return [row["snapshot_id"] for row in versions(repo, source, full_path)]
