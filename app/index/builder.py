"""Incremental DuckDB index builder."""

from __future__ import annotations

import time

from .schema import connect, init_schema
from ..core.cache import cache_db
from ..core.sources import sources_index
from ..core.walk import walk


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


def index_status(repo: str) -> dict:
    srcs = sources_index(repo)
    total = sum(len(snaps) for snaps in srcs.values())
    con = connect(repo)
    init_schema(con)
    indexed = con.execute("SELECT COUNT(*) FROM indexed_snapshots WHERE repo=?", [repo]).fetchone()[0]
    files = con.execute("SELECT COUNT(*) FROM files WHERE repo=?", [repo]).fetchone()[0]
    last = con.execute(
        "SELECT max(indexed_at) FROM indexed_snapshots WHERE repo=?",
        [repo],
    ).fetchone()[0]
    con.close()
    return {"repo": repo, "snapshots": total, "indexed_snapshots": indexed, "missing_snapshots": total - indexed, "files": files, "last_indexed_at": last}


def build_index(repo: str, source: str | None = None, max_snapshots: int | None = None) -> dict:
    """Index missing snapshots. Use ``max_snapshots`` for cautious first runs."""

    srcs = sources_index(repo)
    targets = {source: srcs[source]} if source else srcs
    con = connect(repo)
    init_schema(con)
    already = {
        row[0]
        for row in con.execute("SELECT snapshot_id FROM indexed_snapshots WHERE repo=?", [repo]).fetchall()
    }
    db = cache_db(repo)
    started = time.time()
    indexed = 0
    inserted_files = 0

    for key in sorted(targets):
        for snap in targets[key]:
            if max_snapshots is not None and indexed >= max_snapshots:
                con.close()
                return {"repo": repo, "indexed": indexed, "files": inserted_files, "elapsed": time.time() - started, "limited": True}
            snap_id = snap["id"]
            if snap_id in already:
                continue
            root = snap.get("rootEntry", {}).get("obj")
            if not root:
                continue

            rows: list[tuple] = []
            ordinal = 0

            def visitor(parent: str, entry: dict) -> None:
                nonlocal ordinal
                if entry.get("type") != "f":
                    return
                rel = (parent + "\\" + entry.get("name", "")).lstrip("\\")
                rows.append(
                    (
                        repo,
                        key,
                        _full_path(key, rel),
                        entry.get("name", ""),
                        int(entry.get("size", 0) or 0),
                        entry.get("obj", ""),
                        snap_id,
                        snap.get("startTime", ""),
                        ordinal,
                    )
                )
                ordinal += 1
                if len(rows) >= 5000:
                    con.executemany("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                    rows.clear()

            walk(repo, [root], db, visitor=visitor)
            if rows:
                con.executemany("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            con.execute(
                "INSERT OR REPLACE INTO indexed_snapshots(repo, snapshot_id, source, start_time, root_obj, file_count) VALUES (?, ?, ?, ?, ?, ?)",
                [repo, snap_id, key, snap.get("startTime", ""), root, ordinal],
            )
            inserted_files += ordinal
            indexed += 1
    con.close()
    return {"repo": repo, "indexed": indexed, "files": inserted_files, "elapsed": time.time() - started, "limited": False}
