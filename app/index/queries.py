"""DuckDB query layer for search, TreeSize, and time-travel views.

All reads go through a thread-local cursor off the shared pooled connection
(``read_cursor``) - no per-query file open or schema DDL.
"""

from __future__ import annotations

import os
import threading
import time

from .schema import read_cursor

_SEP = chr(92)  # backslash path separator (avoids source-literal escaping)


def _rows(cur, sql: str, params: list | None = None) -> list[dict]:
    cur.execute(sql, params or [])
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


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


# Whitelisted sort columns (server-side sort over the FULL filtered set, never the page).
_SEARCH_SORT = {
    "size": "size", "physical": "physical", "full_path": "full_path", "name": "name",
    "ext": "ext", "versions": "versions", "last_seen": "last_seen", "first_seen": "first_seen",
    "in_latest": "in_latest", "source": "source",
}


def _search_where(repo: str, query: str, deleted_only: bool, min_size: int,
                  source: str | None, ext: str | None, max_size: int = 0,
                  date_from: str | None = None, date_to: str | None = None,
                  min_versions: int = 0, shared_min: int = 0) -> tuple[list[str], list]:
    """Build the WHERE clause + params for a paths search. ``repo`` is prepended by the caller, so
    the first placeholder (``repo = ?``) is filled there; every condition added here appends its
    params in clause order. Multi-value source/ext accept comma-separated lists (-> IN)."""
    where = ["repo = ?"]
    params: list = []
    if query:
        where.append("full_path ILIKE ?")
        params.append(f"%{query}%")
    if min_size:
        where.append("size >= ?")
        params.append(int(min_size))
    if max_size:
        where.append("size <= ?")
        params.append(int(max_size))
    if deleted_only:
        where.append("in_latest = false")
    if source:
        srcs = [s for s in (x.strip() for x in source.split(",")) if s]
        if len(srcs) > 1:
            where.append("source IN (%s)" % ",".join("?" * len(srcs)))
            params.extend(srcs)
        else:
            where.append("source = ?")
            params.append(srcs[0] if srcs else source)
    if ext:
        exts = [e.lower().lstrip(".") for e in (x.strip() for x in ext.split(",")) if e]
        if exts:
            where.append("ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend(exts)
    if date_from:
        where.append("last_seen >= ?")
        params.append(date_from)
    if date_to:
        where.append("last_seen <= ?")
        params.append(date_to)
    if min_versions:
        where.append("versions >= ?")
        params.append(int(min_versions))
    if shared_min and shared_min > 1:  # content backing >= N distinct paths (exact-duplicate candidates)
        where.append(
            "content_id IN (SELECT content_id FROM paths WHERE repo = ? AND content_id IS NOT NULL "
            "GROUP BY content_id HAVING count(DISTINCT full_path) >= ?)"
        )
        params.extend([repo, int(shared_min)])
    return where, params


def _order_clause(sort: str, direction: str) -> str:
    col = _SEARCH_SORT.get(sort, "size")
    dirn = "ASC" if str(direction).lower() == "asc" else "DESC"
    return f"ORDER BY {col} {dirn}, last_seen DESC"


def search_paths(repo: str, query: str = "", deleted_only: bool = False, min_size: int = 0,
                 source: str | None = None, ext: str | None = None, limit: int = 500, offset: int = 0,
                 sort: str = "size", direction: str = "desc", confirm_exists: bool = False,
                 max_size: int = 0, date_from: str | None = None, date_to: str | None = None,
                 min_versions: int = 0, shared_min: int = 0, dedupe: bool = False) -> dict:
    """Search the pre-aggregated ``paths`` table. Returns the TRUE total count plus one page,
    with sort + filters applied in SQL over the entire matched set. ``dedupe`` collapses all
    path/source variations of the SAME content (group by content_id) into one content-aware row."""
    where, params = _search_where(repo, query, deleted_only, min_size, source, ext, max_size,
                                  date_from, date_to, min_versions, shared_min)
    where_sql = " AND ".join(where)
    cur = read_cursor(repo)
    try:
        if dedupe:
            total = cur.execute(
                f"SELECT count(DISTINCT content_id) FROM paths WHERE {where_sql} AND content_id IS NOT NULL",
                [repo, *params],
            ).fetchone()[0]
            rows = _rows(
                cur,
                f"""
                SELECT arg_max(source, last_seen) AS source, arg_max(full_path, last_seen) AS full_path,
                       arg_max(name, last_seen) AS name, arg_max(ext, last_seen) AS ext,
                       max(size) AS size, max(physical) AS physical, max(versions) AS versions,
                       min(first_seen) AS first_seen, max(last_seen) AS last_seen,
                       bool_or(in_latest) AS in_latest, content_id, count(DISTINCT full_path) AS copies
                FROM paths
                WHERE {where_sql} AND content_id IS NOT NULL
                GROUP BY content_id
                {_order_clause(sort, direction)}
                LIMIT ? OFFSET ?
                """,
                [repo, *params, limit, offset],
            )
        else:
            total = cur.execute(f"SELECT count(*) FROM paths WHERE {where_sql}", [repo, *params]).fetchone()[0]
            rows = _rows(
                cur,
                f"""
                SELECT source, full_path, name, ext, size, physical, versions, first_seen, last_seen, in_latest, content_id
                FROM paths
                WHERE {where_sql}
                {_order_clause(sort, direction)}
                LIMIT ? OFFSET ?
                """,
                [repo, *params, limit, offset],
            )
            # copies = how many distinct paths share each row's exact content (1 = unique). One
            # grouped query over just this page's content ids, then mapped back.
            page_cids = list({r["content_id"] for r in rows if r.get("content_id")})
            cp: dict[str, int] = {}
            for i in range(0, len(page_cids), 900):
                grp = page_cids[i:i + 900]
                if not grp:
                    continue
                q = ("SELECT content_id, count(*) FROM paths WHERE repo=? AND content_id IN (%s) "
                     "GROUP BY content_id" % ",".join("?" * len(grp)))
                for c, n in cur.execute(q, [repo, *grp]).fetchall():
                    cp[c] = n
            for row in rows:
                row["copies"] = cp.get(row.get("content_id"), 1)
    finally:
        cur.close()
    if confirm_exists:
        for row in rows:
            row["on_disk"] = os.path.exists(row["full_path"])
    return {"rows": rows, "total": total, "offset": offset, "limit": limit}


# ---- Search "landing view" cache (default empty-query view) — serve-stale + bg revalidate ----
_LANDING: dict[str, dict] = {}
_LANDING_TTL = 300.0


def warm_landing(repo: str, limit: int = 500) -> dict:
    res = search_paths(repo, limit=limit, sort="size", direction="desc")
    _LANDING[repo] = {"result": res, "ts": time.time(), "limit": limit}
    return res


def landing(repo: str, limit: int = 500) -> dict:
    c = _LANDING.get(repo)
    if c and c.get("limit") == limit:
        if time.time() - c["ts"] < _LANDING_TTL:
            return c["result"]
        threading.Thread(target=warm_landing, args=(repo, limit), daemon=True).start()  # serve-stale
        return c["result"]
    return warm_landing(repo, limit)


def search_export(repo: str, query: str = "", deleted_only: bool = False, min_size: int = 0,
                  source: str | None = None, ext: str | None = None,
                  sort: str = "size", direction: str = "desc", cap: int = 2_000_000,
                  max_size: int = 0, date_from: str | None = None, date_to: str | None = None,
                  min_versions: int = 0, shared_min: int = 0):
    """Yield every matching row (full filtered set) for CSV export - not limited to a page."""
    where, params = _search_where(repo, query, deleted_only, min_size, source, ext, max_size,
                                  date_from, date_to, min_versions, shared_min)
    where_sql = " AND ".join(where)
    cur = read_cursor(repo)
    cur.execute(
        f"SELECT source, full_path, name, ext, size, physical, versions, first_seen, last_seen, in_latest "
        f"FROM paths WHERE {where_sql} {_order_clause(sort, direction)} LIMIT {int(cap)}",
        [repo, *params],
    )
    return cur


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
    return search_paths(repo, query="", deleted_only=False, min_size=0, limit=limit)["rows"]


def snapshot_files(repo: str, snapshot_ids: list[str], limit: int = 40,
                   reclaimable_only: bool = False) -> list[dict]:
    """Largest files that are NEW in the given snapshots (delta index) - the 'what would be
    reclaimed' preview for the delete panel. reclaimable_only adds the content-refcount<=1 filter
    (content exclusive to this source = the precise reclaimable view)."""
    if not snapshot_ids:
        return []
    ph = ",".join("?" * len(snapshot_ids))
    params: list = [repo, *snapshot_ids]
    extra = ""
    if reclaimable_only:
        extra = " AND content_id IN (SELECT cid FROM content_refs WHERE repo=? AND refs<=1)"
        params.append(repo)
    cur = read_cursor(repo)
    try:
        return _rows(
            cur,
            f"""
            SELECT full_path, any_value(source) AS source, max(size) AS size
            FROM files WHERE repo=? AND snapshot_id IN ({ph}){extra}
            GROUP BY full_path ORDER BY size DESC LIMIT ?
            """,
            [*params, limit],
        )
    finally:
        cur.close()


def tree_children(repo: str, source: str, snapshot_id: str, prefix: str = "",
                  reclaimable_only: bool = False) -> list[dict]:
    """Immediate children under ``prefix`` for one snapshot, aggregated in SQL. reclaimable_only
    keeps only files whose content is exclusive to this source (global refcount <= 1) = the
    Reclaimable tree mode (what is freed if this snapshot is deleted)."""
    base = prefix.rstrip(_SEP)
    like = base + _SEP + "%" if base else "%"
    recl = (" AND content_id IN (SELECT cid FROM content_refs WHERE repo='%s' AND refs<=1)" % repo) if reclaimable_only else ""
    cur = read_cursor(repo)
    try:
        rows = _rows(
            cur,
            f"""
            WITH sub AS (
                SELECT size, mtime,
                       CASE WHEN ? = '' THEN full_path
                            ELSE substr(full_path, length(?) + 2) END AS remainder
                FROM files
                WHERE repo = ? AND source = ? AND snapshot_id = ? AND full_path LIKE ?{recl}
            )
            SELECT split_part(remainder, ?, 1) AS name,
                   sum(size) AS size,
                   count(*) AS files,
                   max(mtime) AS modified,
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
                "modified": str(r["modified"]) if r["modified"] else "",
                "type": "folder" if r["is_folder"] else "file",
            }
        )
    return children


def snapshots_for_path(repo: str, source: str, full_path: str) -> list[str]:
    return [row["snapshot_id"] for row in versions(repo, source, full_path)]


def paths_for_content(repo: str, content_id: str) -> list[dict]:
    """Every distinct (source, full_path) whose latest version IS this exact content object -
    i.e. all locations of an exact-duplicate file across sources/paths."""
    cur = read_cursor(repo)
    try:
        return _rows(
            cur,
            "SELECT DISTINCT source, full_path FROM files WHERE repo=? AND content_id=? "
            "ORDER BY source, full_path",
            [repo, content_id],
        )
    finally:
        cur.close()


def snapshots_for_content(repo: str, content_id: str) -> list[str]:
    """Every distinct snapshot id that references this content object (across all paths/sources).
    This is the full set that must be deleted to free the content everywhere."""
    cur = read_cursor(repo)
    try:
        rows = cur.execute(
            "SELECT DISTINCT snapshot_id FROM files WHERE repo=? AND content_id=?",
            [repo, content_id],
        ).fetchall()
    finally:
        cur.close()
    return [r[0] for r in rows]


def _source_root_path(source: str) -> str:
    return source.split(":", 1)[1] if ":" in source else source


def tree_children_live(repo: str, source: str, root_obj: str, prefix: str) -> list[dict]:
    """FULL point-in-time tree of one snapshot: walk the snapshot's directory objects via the
    warm dir cache (Kopia-accurate, complete - not the delta index). Folder sizes come from each
    entry's ``summ`` (no deep walk); per-file physical comes from the sizemap / indirect map."""
    from ..core.cache import cache_db, get_children  # local import: avoid core<-index cycle on load
    dbsql = cache_db(repo)
    root = _source_root_path(source)
    rel = prefix[len(root):] if prefix.startswith(root) else prefix
    oid = root_obj
    for seg in [s for s in rel.split(_SEP) if s]:
        entries = get_children(repo, dbsql, [oid]).get(oid, [])
        match = next((e for e in entries if e.get("type") == "d" and e.get("name") == seg), None)
        if not match:
            return []
        oid = match.get("obj")
    entries = get_children(repo, dbsql, [oid]).get(oid, [])

    from ..core.sizemap import load_sizemap
    smap = load_sizemap(repo)
    ind_objs = [e.get("obj") for e in entries if e.get("type") == "f" and (e.get("obj") or "").startswith("I")]
    ind_phys: dict[str, int] = {}
    if ind_objs:
        cur = read_cursor(repo)
        try:
            q = "SELECT oid, packed FROM indirect_phys WHERE repo=? AND oid IN (%s)" % ",".join("?" * len(ind_objs))
            for o, p in cur.execute(q, [repo, *ind_objs]).fetchall():
                ind_phys[o] = p
        finally:
            cur.close()

    base = prefix.rstrip(_SEP)
    children: list[dict] = []
    for e in entries:
        name = e.get("name", "")
        path = (base + _SEP + name) if base else name
        if e.get("type") == "d":
            summ = e.get("summ", {}) or {}
            children.append({
                "path": path, "name": name, "size": summ.get("size", 0) or 0,
                "files": summ.get("files", 0) or 0, "modified": e.get("mtime", "") or "",
                "type": "folder", "physical": None, "content_id": e.get("obj", ""),
            })
        elif e.get("type") == "f":
            obj = e.get("obj", "")
            phys = ind_phys.get(obj) if obj.startswith("I") else smap.get(obj, 0)
            children.append({
                "path": path, "name": name, "size": int(e.get("size", 0) or 0),
                "files": 1, "modified": e.get("mtime", "") or "",
                "type": "file", "physical": phys, "content_id": obj,
            })
    return children


def tree_children_paths(repo: str, source: str, prefix: str) -> list[dict]:
    """ALL-snapshots (deduped) tree for a source: aggregate the distinct ``paths`` rows under a
    prefix - the source's total file set across every snapshot, not a point-in-time view."""
    base = prefix.rstrip(_SEP)
    like = base + _SEP + "%" if base else "%"
    cur = read_cursor(repo)
    try:
        rows = _rows(
            cur,
            """
            WITH sub AS (
                SELECT size, physical, last_seen,
                       CASE WHEN ? = '' THEN full_path
                            ELSE substr(full_path, length(?) + 2) END AS remainder
                FROM paths
                WHERE repo = ? AND source = ? AND full_path LIKE ?
            )
            SELECT split_part(remainder, ?, 1) AS name,
                   sum(size) AS size,
                   sum(physical) AS physical,
                   count(*) AS files,
                   max(last_seen) AS modified,
                   max(CASE WHEN position(? IN remainder) > 0 THEN 1 ELSE 0 END) AS is_folder
            FROM sub
            WHERE remainder <> ''
            GROUP BY name
            ORDER BY size DESC
            """,
            [base, base, repo, source, like, _SEP, _SEP],
        )
    finally:
        cur.close()
    children = []
    for r in rows:
        name = r["name"]
        children.append({
            "path": (base + _SEP + name) if base else name,
            "name": name,
            "size": r["size"] or 0,
            "physical": r["physical"] or 0,
            "files": r["files"],
            "modified": str(r["modified"]) if r["modified"] else "",
            "type": "folder" if r["is_folder"] else "file",
        })
    return children
