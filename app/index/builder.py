"""DuckDB index builder.

Delta strategy: index each source's snapshots NEWEST-first with an accumulating
``seen_dirs`` set. The latest snapshot is indexed fully (powers the tree + the
``in_latest``/deleted-from-disk logic); older snapshots emit only their UNIQUE
(changed/deleted) files. Each unique directory-OID is walked once, so the index is
~one row per file-version (not files x snapshots). Reads the warm sqlite dir cache -
no kopia re-walk. Runs in a background thread so the UI never blocks.
"""

from __future__ import annotations

import threading
import time

import pyarrow as pa

from .schema import get_db, read_cursor
from ..core.cache import cache_db
from ..core.sources import sources_index
from ..core.walk import walk_new_visit

_JOBS: dict[str, dict] = {}


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
    job = _JOBS.get(repo, {})
    if job.get("building"):
        return {
            "repo": repo, "snapshots": total,
            "indexed_snapshots": job.get("indexed", 0),
            "files": None, "building": True, "error": None,
            "current_source": job.get("current_source"),
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


_FLUSH_ROWS = 50_000
_INSERT_SQL = (
    "INSERT INTO files SELECT repo, source, full_path, name, size, content_id, "
    "snapshot_id, CAST(start_time AS TIMESTAMP), ordinal FROM _ingest"
)
_FILE_INDEXES = (
    ("files_repo_source_path", "files(repo, source, full_path)"),
    ("files_snapshot", "files(repo, snapshot_id)"),
)


def _flush(con, rows: list[tuple]) -> None:
    """Bulk-insert a row batch via an Arrow table (~150x faster than executemany)."""
    if not rows:
        return
    c = list(zip(*rows))
    tbl = pa.table({
        "repo": c[0], "source": c[1], "full_path": c[2], "name": c[3],
        "size": c[4], "content_id": c[5], "snapshot_id": c[6], "start_time": c[7], "ordinal": c[8],
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


def _run_build(repo: str, source: str | None) -> None:
    job = _JOBS[repo]
    con = None
    full_build = source is None
    try:
        srcs = sources_index(repo)
        keys = [source] if source else sorted(srcs)
        con = get_db(repo).cursor()  # build writes via its own cursor off the shared pool
        db = cache_db(repo)
        if full_build:  # bulk-load without index maintenance, rebuild indexes once at the end
            _drop_indexes(con)
        job["total"] = sum(len(srcs[k]) for k in keys if k in srcs)
        job["indexed"] = 0
        for key in keys:
            if key not in srcs:
                continue
            job["current_source"] = key
            con.execute("DELETE FROM files WHERE repo=? AND source=?", [repo, key])
            con.execute("DELETE FROM indexed_snapshots WHERE repo=? AND source=?", [repo, key])
            seen_dirs: set[str] = set()
            snaps = sorted(srcs[key], key=lambda s: s.get("startTime", ""), reverse=True)  # newest first
            rows: list[tuple] = []  # per-source buffer, flushed in Arrow batches
            for snap in snaps:
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
            _flush(con, rows)  # flush this source's remaining rows
        con.commit()
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        job["error"] = str(exc)
    finally:
        if con is not None:
            try:
                if full_build:
                    _create_indexes(con)
                con.commit()
            except Exception:
                pass
            con.close()
        job["building"] = False
        job["finished_at"] = time.time()


def start_build(repo: str, source: str | None = None) -> dict:
    """Kick off a background index build (delta). Returns immediately; poll index_status."""
    if _JOBS.get(repo, {}).get("building"):
        return {"started": False, "reason": "already building", "repo": repo}
    _JOBS[repo] = {"building": True, "indexed": 0, "total": 0, "error": None, "current_source": None}
    threading.Thread(target=_run_build, args=(repo, source), daemon=True).start()
    return {"started": True, "repo": repo, "source": source}
