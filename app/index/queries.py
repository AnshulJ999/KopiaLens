"""DuckDB query layer for search, TreeSize, and time-travel views.

All reads go through a thread-local cursor off the shared pooled connection
(``read_cursor``) - no per-query file open or schema DDL.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from . import entries, resolve
from .store import read_cursor

_SEP = chr(92)  # backslash path separator (avoids source-literal escaping)

# LIKE wildcard escaping. `_` matches ANY single character and `%` matches anything, so a literal
# one in a user's query or folder name silently broadens the match: `Backup_2024` also matches
# `BackupX2024`. Harmless in a listing; not harmless in `remove_targets`, which enumerates what a
# chunked DELETE will remove. Verified live before this fix: searching for `_` returned all
# 5,193,822 paths in repo 1 rather than the ones containing an underscore.
#
# The escape character is `!`, deliberately NOT backslash - backslash is the path separator here,
# so using it would mean escaping every separator in every Windows path.
_LIKE_ESC = "!"
_ESC = " ESCAPE '" + _LIKE_ESC + "'"


def like_literal(value: str) -> str:
    """Quote a string so LIKE/ILIKE matches it literally (pair with the _ESC clause)."""
    for ch in (_LIKE_ESC, "%", "_"):
        value = value.replace(ch, _LIKE_ESC + ch)
    return value


def _rows(cur, sql: str, params: list | None = None) -> list[dict]:
    cur.execute(sql, params or [])
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def index_summary(repo: str) -> dict:
    cur = read_cursor(repo)
    try:
        # Every pathx row exists because at least one filev row references it (the builder
        # derives pathx from files), so distinct full_path over pathx equals distinct
        # full_path over versions - and stays a STRING count (two sources sharing a path
        # string count once), which a distinct path_id would not.
        row = cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM filev) AS rows,
                (SELECT COUNT(DISTINCT src_id) FROM filev) AS sources,
                (SELECT COUNT(DISTINCT snap_id) FROM filev) AS snapshots,
                (SELECT COUNT(DISTINCT full_path) FROM pathx) AS distinct_paths,
                (SELECT SUM(size) FROM filev) AS logical_sum
            """
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
# "stored"/"frees" order by the nightly ranked list () through a joined column, not pathx.
_SEARCH_SORT = {
    "size": "size", "physical": "physical", "full_path": "full_path", "name": "name",
    "ext": "ext", "versions": "versions", "last_seen": "last_seen", "first_seen": "first_seen",
    "in_latest": "in_latest", "source": "source",
    "stored": "ranked_value", "frees": "ranked_value",
}
_RANK_SORTS = ("stored", "frees")


def _split_exts(ext: str) -> list[str]:
    return [e.lower().lstrip(".") for e in (x.strip() for x in ext.split(",")) if e]


def _where(conds: list[str]) -> str:
    """Joined conditions, or a never-false placeholder when nothing filters - the rewritten
    queries select straight from pathx, and a site with no filters has no WHERE clause."""
    return " AND ".join(conds) if conds else "1 = 1"


def _src_ids(cur, source: str) -> list[int]:
    """Comma-separated source filter -> resolved src_ids (input-side lookup, never a join).
    A miss warns via resolve.src_id and drops out; an all-miss filter resolves to an empty
    list, which callers turn into an always-false condition - exactly what an unknown source
    matched before."""
    srcs = [s for s in (x.strip() for x in source.split(",")) if s]
    return [i for i in (resolve.src_id(cur, s) for s in srcs) if i is not None]


def _src_cond(cur, source: str, alias: str = "p") -> tuple[str, list]:
    """(condition, params) restricting one query to the given sources. Empty resolution yields
    a never-true condition so the result stays empty instead of erroring.

    Comma-splitting belongs ONLY to interfaces that document a multi-source filter (search).
    Callers taking a single source use ``_src_cond_exact`` - a source path may itself contain
    a comma."""
    ids = _src_ids(cur, source)
    if not ids:
        return "1 = 0", []
    return f"{alias}.src_id IN (%s)" % ",".join("?" * len(ids)), ids


def _src_cond_exact(cur, source: str, alias: str = "p") -> tuple[str, list]:
    """One source, matched exactly - never split on commas (`C:\\A, docs` is one valid source)."""
    sid = resolve.src_id(cur, source)
    if sid is None:
        return "1 = 0", []
    return f"{alias}.src_id = ?", [sid]


def _search_where(cur, query: str, deleted_only: bool, min_size: int,
                  source: str | None, ext: str | None, max_size: int = 0,
                  date_from: str | None = None, date_to: str | None = None,
                  min_versions: int = 0, shared_min: int = 0,
                  ext_exclude: str | None = None, first_from: str | None = None) -> tuple[list[str], list]:
    """Build the WHERE clause + params for a pathx search, in the caller's alias scheme
    (``pathx p``). Source filters resolve to src_ids up front; every condition appends its
    params in clause order. Multi-value source/ext accept comma-separated lists (-> IN)."""
    where: list[str] = []
    params: list = []
    if query:
        where.append("p.full_path ILIKE ?" + _ESC)
        params.append(f"%{like_literal(query)}%")
    if min_size:
        where.append("p.size >= ?")
        params.append(int(min_size))
    if max_size:
        where.append("p.size <= ?")
        params.append(int(max_size))
    if deleted_only:
        where.append("p.in_latest = false")
    if source:
        cond, sp = _src_cond(cur, source)
        where.append(cond)
        params.extend(sp)
    if ext:
        exts = _split_exts(ext)
        if exts:
            where.append("p.ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend(exts)
    if ext_exclude:
        exx = _split_exts(ext_exclude)
        if exx:  # hide these extensions; keep ext-less rows (NULL/'')
            where.append("(p.ext IS NULL OR p.ext NOT IN (%s))" % ",".join("?" * len(exx)))
            params.extend(exx)
    if date_from:
        where.append("p.last_seen >= ?")
        params.append(date_from)
    if date_to:
        where.append("p.last_seen <= ?")
        params.append(date_to)
    if first_from:  # "recently backed up": path first appeared in a snapshot on/after this cutoff
        where.append("p.first_seen >= ?")
        params.append(first_from)
    if min_versions:
        where.append("p.versions >= ?")
        params.append(int(min_versions))
    if shared_min and shared_min > 1:  # content backing >= N distinct paths (exact-duplicate candidates)
        where.append(
            # DISTINCT full_path, not path_id: pathx holds one row per (source, path), so a file
            # covered by two overlapping sources is two rows but ONE location. Counting rows made
            # a third of repo1's paths look like duplicates, and this filter feeds the delete plan.
            "p.cid_id IN (SELECT p2.cid_id FROM pathx p2 WHERE p2.cid_id IS NOT NULL "
            "GROUP BY p2.cid_id HAVING count(DISTINCT p2.full_path) >= ?)"
        )
        params.append(int(shared_min))
    return where, params


# Every ordered query ends on a column that is unique in its own output, so tied sort keys resolve
# the same way on every run. Without it a top-N list silently changes membership between identical
# calls. Row-level queries use (source, full_path); grouped queries use their group key.
def _order_clause(sort: str, direction: str, tiebreak: str = "source, full_path",
                  nulls_last: bool = False) -> str:
    col = _SEARCH_SORT.get(sort, "size")
    dirn = "ASC" if str(direction).lower() == "asc" else "DESC"
    tail = " NULLS LAST" if nulls_last else ""
    return f"ORDER BY {col} {dirn}{tail}, last_seen DESC, {tiebreak}"


def search_paths(repo: str, query: str = "", deleted_only: bool = False, min_size: int = 0,
                 source: str | None = None, ext: str | None = None, limit: int = 500, offset: int = 0,
                 sort: str = "size", direction: str = "desc", confirm_exists: bool = False,
                 max_size: int = 0, date_from: str | None = None, date_to: str | None = None,
                 min_versions: int = 0, shared_min: int = 0, dedupe: bool = False,
                 ext_exclude: str | None = None, first_from: str | None = None) -> dict:
    """Search the pre-aggregated ``paths`` table. Returns the TRUE total count plus one page,
    with sort + filters applied in SQL over the entire matched set. ``dedupe`` collapses all
    path/source variations of the SAME content (group by content id) into one content-aware row.

    Sorting by ``stored``/``frees`` uses the nightly ranked list (): ranked rows come first
    (each carrying its exact value in ``stored_all_versions``/``frees_all_versions``), unranked
    rows after them. With no exact list yet the sort falls back to physical and ``ranked`` says so.
    """
    cur = read_cursor(repo)
    ranked_meta = {"available": False, "kind": sort if sort in _RANK_SORTS else None, "count": 0}
    mat = None
    try:
        if sort in _RANK_SORTS:
            from . import rankpaths
            uuid_tag = uuid.uuid4().hex[:8]
            values = rankpaths.ranked_values(repo, sort)
            if values:
                import pyarrow as pa
                mat = f"_ranked_values_{uuid_tag}"
                cur.register(mat, pa.table({
                    "key": pa.array(list(values), pa.int64()),
                    "value": pa.array([values[k] for k in values], pa.uint64())}))
                ranked_meta = {"available": True, "kind": sort, "count": len(values)}
            else:
                sort = "physical"      # nothing ranked yet: the closest meaningful order
        join_sql = f"LEFT JOIN {mat} r ON r.key = p.path_id" if mat else ""
        if mat and sort == "frees":
            join_sql = f"LEFT JOIN {mat} r ON r.key = p.cid_id"
        value_sql = ", r.value AS ranked_value" if mat else ""
        where, params = _search_where(cur, query, deleted_only, min_size, source, ext, max_size,
                                      date_from, date_to, min_versions, shared_min, ext_exclude, first_from)
        where_sql = _where(where)
        if dedupe:
            total = cur.execute(
                f"SELECT count(DISTINCT p.cid_id) FROM pathx p "
                f"WHERE {where_sql} AND p.cid_id IS NOT NULL",
                params,
            ).fetchone()[0]
            rows = _rows(
                cur,
                f"""
                SELECT arg_max(s.source, p.last_seen) AS source, arg_max(p.full_path, p.last_seen) AS full_path,
                       arg_max(p.name, p.last_seen) AS name, arg_max(p.ext, p.last_seen) AS ext,
                       max(p.size) AS size, max(p.physical) AS physical, max(p.versions) AS versions,
                       max(p.exclusive) AS exclusive,
                       min(p.first_seen) AS first_seen, max(p.last_seen) AS last_seen,
                       bool_or(p.in_latest) AS in_latest, c.content_id, count(DISTINCT p.full_path) AS copies
                       {", max(r.value) AS ranked_value" if mat else ""}
                FROM pathx p
                JOIN src s ON s.src_id = p.src_id
                LEFT JOIN cid c ON c.cid_id = p.cid_id
                {join_sql}
                WHERE {where_sql} AND p.cid_id IS NOT NULL
                GROUP BY p.cid_id, c.content_id
                {_order_clause(sort, direction, "c.content_id", nulls_last=bool(mat))}
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
        else:
            total = cur.execute(f"SELECT count(*) FROM pathx p WHERE {where_sql}", params).fetchone()[0]
            rows = _rows(
                cur,
                f"""
                SELECT s.source AS source, p.full_path, p.name, p.ext, p.size, p.physical, p.exclusive,
                       p.versions, p.first_seen, p.last_seen, p.in_latest, c.content_id
                       {value_sql}
                FROM pathx p
                JOIN src s ON s.src_id = p.src_id
                LEFT JOIN cid c ON c.cid_id = p.cid_id
                {join_sql}
                WHERE {where_sql}
                {_order_clause(sort, direction, nulls_last=bool(mat))}
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
            # copies = how many distinct paths share each row's exact content (1 = unique); sources =
            # across how many SOURCES (KL10 - within-source copies keep nothing alive elsewhere). One
            # grouped query over just this page's content ids, then mapped back.
            page_cids = list({r["content_id"] for r in rows if r.get("content_id")})
            cp: dict[str, int] = {}
            cs: dict[str, int] = {}
            for i in range(0, len(page_cids), 900):
                grp = page_cids[i:i + 900]
                if not grp:
                    continue
                # DISTINCT full_path, matching the Copies column's own promise ("at N paths") and
                # the shared_min filter. count(*) counted pathx ROWS, so one file covered by two
                # overlapping sources read as 2 copies. How many SOURCES hold it is the Src column.
                q = ("SELECT c.content_id, count(DISTINCT p.full_path), count(DISTINCT p.src_id) "
                     "FROM pathx p JOIN cid c ON c.cid_id = p.cid_id WHERE c.content_id IN (%s) "
                     "GROUP BY c.content_id" % ",".join("?" * len(grp)))
                for c, n, s in cur.execute(q, grp).fetchall():
                    cp[c] = n
                    cs[c] = s
            for row in rows:
                row["copies"] = cp.get(row.get("content_id"), 1)
                row["sources"] = cs.get(row.get("content_id"), 1)
        if mat:
            field = "stored_all_versions" if ranked_meta["kind"] == "stored" else "frees_all_versions"
            for row in rows:
                value = row.pop("ranked_value", None)
                row[field] = int(value) if value is not None else None
    finally:
        if mat:
            try:
                cur.unregister(mat)
            except Exception:  # noqa: BLE001 - already gone / connection closing
                pass
        cur.close()
    if confirm_exists:
        for row in rows:
            row["on_disk"] = os.path.exists(row["full_path"])
    return {"rows": rows, "total": total, "offset": offset, "limit": limit, "ranked": ranked_meta}


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
                  min_versions: int = 0, shared_min: int = 0, ext_exclude: str | None = None,
                  first_from: str | None = None):
    """Yield every matching row (full filtered set) for CSV export - not limited to a page."""
    cur = read_cursor(repo)
    where, params = _search_where(cur, query, deleted_only, min_size, source, ext, max_size,
                                  date_from, date_to, min_versions, shared_min, ext_exclude, first_from)
    where_sql = _where(where)
    cur.execute(
        f"SELECT s.source AS source, p.full_path, p.name, p.ext, p.size, p.physical, "
        f"p.versions, p.first_seen, p.last_seen, p.in_latest "
        f"FROM pathx p JOIN src s ON s.src_id = p.src_id "
        f"WHERE {where_sql} {_order_clause(sort, direction)} LIMIT {int(cap)}",
        params,
    )
    return cur


def versions(repo: str, source: str, full_path: str) -> list[dict]:
    cur = read_cursor(repo)
    try:
        sid = resolve.src_id(cur, source)
        pid = resolve.path_id(cur, sid, full_path) if sid is not None else None
        if pid is None:  # unknown source or path: nothing matched before, nothing now
            return []
        return _rows(
            cur,
            """
            SELECT sn.snapshot_id, sn.start_time, f.size, c.content_id
            FROM filev f
            JOIN snap sn ON sn.snap_id = f.snap_id
            LEFT JOIN cid c ON c.cid_id = f.cid_id
            WHERE f.path_id = ?
            ORDER BY sn.start_time DESC, sn.snapshot_id DESC
            """,
            [pid],
        )
    finally:
        cur.close()


def versions_many(repo: str, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], list[dict]]:
    """Batched ``versions``: one path resolution per source group, one filev scan per 900 paths.

 ``selection_facts`` priced a 500-file selection at one full 5.3M-row pathx scan PER FILE
    PER HOLDER - a thousand scans, minutes before the delete panel could open. Rows come
    back in the same order ``versions`` returns: snapshot start DESC, snapshot id DESC."""
    want = list(dict.fromkeys((s, p) for s, p in pairs if s and p))
    out: dict[tuple[str, str], list[dict]] = {k: [] for k in want}
    if not want:
        return out
    cur = read_cursor(repo)
    try:
        sid_of = resolve.src_map(cur)   # path_ids takes integer source ids, not source keys
        pid_of = resolve.path_ids(cur, [(sid_of[s], p) for s, p in want if s in sid_of])
        if not pid_of:
            return out
        src_of = {sid: s for s, sid in sid_of.items()}
        by_pid = {pid: (src_of[sid], fp) for (sid, fp), pid in pid_of.items()}
        pids = sorted(by_pid)
        grouped: dict[int, list[dict]] = {pid: [] for pid in pids}
        for i in range(0, len(pids), 900):
            grp = pids[i:i + 900]
            q = ("SELECT f.path_id, sn.snapshot_id, sn.start_time, f.size, c.content_id "
                 "FROM filev f JOIN snap sn ON sn.snap_id = f.snap_id "
                 "LEFT JOIN cid c ON c.cid_id = f.cid_id "
                 "WHERE f.path_id IN (%s) "
                 "ORDER BY sn.start_time DESC, sn.snapshot_id DESC"
                 % ",".join("?" * len(grp)))
            for pid, snapshot_id, start_time, size, content_id in cur.execute(q, grp).fetchall():
                grouped[pid].append({"snapshot_id": snapshot_id, "start_time": start_time,
                                     "size": size, "content_id": content_id})
        for pid, key in by_pid.items():
            out[key] = grouped[pid]
    finally:
        cur.close()
    return out


def largest_files(repo: str, limit: int = 200) -> list[dict]:
    return search_paths(repo, query="", deleted_only=False, min_size=0, limit=limit)["rows"]


def snapshot_files(repo: str, snapshot_ids: list[str], limit: int = 40,
                   reclaimable_only: bool = False) -> list[dict]:
    """Largest files that are NEW in the given snapshots (delta index) - the 'what would be
    reclaimed' preview for the delete panel. reclaimable_only adds the content-refcount<=1 filter
    (content exclusive to this source = the precise reclaimable view)."""
    if not snapshot_ids:
        return []
    cur = read_cursor(repo)
    try:
        sids = [i for i in (resolve.snap_id(cur, s) for s in snapshot_ids) if i is not None]
        if not sids:
            return []
        ph = ",".join("?" * len(sids))
        extra = ""
        if reclaimable_only:
            extra = " AND f.cid_id IN (SELECT m.cid_id FROM crefs m WHERE m.refs <= 1)"
        return _rows(
            cur,
            f"""
            SELECT p.full_path, any_value(s.source) AS source, max(f.size) AS size
            FROM filev f
            JOIN pathx p ON p.path_id = f.path_id
            JOIN src s ON s.src_id = p.src_id
            WHERE f.snap_id IN ({ph}){extra}
            GROUP BY p.full_path ORDER BY size DESC, p.full_path LIMIT ?
            """,
            [*sids, limit],
        )
    finally:
        cur.close()


def tree_children(repo: str, source: str, snapshot_id: str, prefix: str = "",
                  reclaimable_only: bool = False) -> list[dict]:
    """Immediate children under ``prefix`` for one snapshot, aggregated in SQL. reclaimable_only
    keeps only files whose content is exclusive to this source (global refcount <= 1) = the
    Reclaimable tree mode (what is freed if this snapshot is deleted)."""
    base = prefix.rstrip(_SEP)
    recl = " AND f.cid_id IN (SELECT m.cid_id FROM crefs m WHERE m.refs <= 1)" if reclaimable_only else ""
    cur = read_cursor(repo)
    try:
        sid = resolve.src_id(cur, source)
        snid = resolve.snap_id(cur, snapshot_id)
        if sid is None or snid is None:  # unknown source/snapshot: no rows before, none now
            return []
        scope, sp = scope_sql(cur, prefix, include_path=False)
        cond = f" AND {scope}" if scope else ""
        rows = _rows(
            cur,
            f"""
            WITH sub AS (
                SELECT f.size, f.mtime,
                       CASE WHEN ? = '' THEN p.full_path
                            ELSE substr(p.full_path, length(?) + 2) END AS remainder
                FROM filev f
                JOIN pathx p ON p.path_id = f.path_id
                WHERE p.src_id = ? AND f.snap_id = ?{cond}{recl}
            )
            SELECT split_part(remainder, ?, 1) AS name,
                   sum(size) AS size,
                   count(*) AS files,
                   max(mtime) AS modified,
                   max(CASE WHEN position(? IN remainder) > 0 THEN 1 ELSE 0 END) AS is_folder
            FROM sub
            WHERE remainder <> ''
            GROUP BY name
            ORDER BY size DESC, name
            """,
            [base, base, sid, snid, *sp, _SEP, _SEP],
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


def paths_for_content(repo: str, content_id: str) -> list[dict]:
    """Every distinct (source, full_path) whose latest version IS this exact content object -
    i.e. all locations of an exact-duplicate file across sources/paths."""
    cur = read_cursor(repo)
    try:
        c = resolve.cid_id(cur, content_id)
        if c is None:  # unknown content: no locations before, none now
            return []
        return _rows(
            cur,
            "SELECT DISTINCT s.source, p.full_path FROM filev f "
            "JOIN pathx p ON p.path_id = f.path_id "
            "JOIN src s ON s.src_id = p.src_id "
            "WHERE f.cid_id=? ORDER BY s.source, p.full_path",
            [c],
        )
    finally:
        cur.close()


def group_by_folder(repo: str, depth: int = 3, query: str = "", source: str | None = None,
                    ext: str | None = None, deleted_only: bool = False, min_size: int = 0,
                    min_files: int = 0, sort: str = "physical", direction: str = "desc",
                    limit: int = 500, ext_exclude: str | None = None) -> dict:
    """Bloat finder: roll the (filtered) `paths` up by the first `depth` path segments, summing
    physical + logical + file count per folder, ranked. Find backed-up folders that shouldn't be
    there. Reclaim (freeable) is computed on demand per folder (folder_detail), not here."""
    cur = read_cursor(repo)
    try:
        conds: list[str] = []
        params: list = []
        if source:
            cond, sp = _src_cond_exact(cur, source)
            conds.append(cond)
            params.extend(sp)
        if ext:
            exts = _split_exts(ext)
            if exts:
                conds.append("p.ext IN (%s)" % ",".join("?" * len(exts)))
                params.extend(exts)
        if ext_exclude:
            exx = _split_exts(ext_exclude)
            if exx:
                conds.append("(p.ext IS NULL OR p.ext NOT IN (%s))" % ",".join("?" * len(exx)))
                params.extend(exx)
        if deleted_only:
            conds.append("p.in_latest=false")
        if query:
            conds.append("p.full_path ILIKE ?" + _ESC)
            params.append(f"%{like_literal(query)}%")
        where = _where(conds)
        order = {"physical": "phys", "logical": "logical", "files": "files"}.get(sort, "phys")
        dirn = "ASC" if direction == "asc" else "DESC"
        d = max(1, int(depth))
        # Group by the file's PARENT folder (drop the filename) capped at depth, so individual files
        # never masquerade as folders and `depth` means folder depth. + deleted-ratio & last-seen.
        rows = _rows(
            cur,
            f"""
            SELECT array_to_string(list_slice(string_split(p.full_path, ?), 1,
                       least({d}, len(string_split(p.full_path, ?)) - 1)), ?) AS folder,
                   sum(p.physical) AS phys, sum(coalesce(p.exclusive, 0)) AS excl,
                   sum(p.size) AS logical, count(*) AS files,
                   count(*) FILTER (WHERE NOT p.in_latest) AS deleted_files,
                   max(p.last_seen) AS last_seen
            FROM pathx p WHERE {where}
            GROUP BY folder
            HAVING sum(p.physical) >= {int(min_size)} AND count(*) >= {int(min_files)}
            ORDER BY {order} {dirn}, folder LIMIT {int(limit)}
            """,
            [_SEP, _SEP, _SEP, *params],
        )
    finally:
        cur.close()
    return {"rows": rows, "depth": d}


def holders_for_paths(repo: str, full_paths: list[str]) -> dict[str, list[str]]:
    """``{full_path: [source, ...]}`` - every source that backs up each exact path."""
    want = list(dict.fromkeys(p for p in full_paths if p))
    out: dict[str, list[str]] = {p: [] for p in want}
    if not want:
        return out
    cur = read_cursor(repo)
    try:
        for i in range(0, len(want), 900):
            grp = want[i:i + 900]
            q = ("SELECT DISTINCT p.full_path, s.source FROM pathx p JOIN src s ON s.src_id = p.src_id "
                 "WHERE p.full_path IN (%s)" % ",".join("?" * len(grp)))
            for fp, src in cur.execute(q, grp).fetchall():
                out[fp].append(src)
    finally:
        cur.close()
    for v in out.values():
        v.sort()
    return out


def paths_named(repo: str, sources: list[str], names: list[str], cap: int = 500,
                biggest_first: bool = False) -> dict:
    """Every path in ``sources`` whose file name equals one of ``names`` - what kopia's
    ``remove-files --filename`` matches. ``{"rows": [{source, full_path, size, physical,
    in_latest}], "total", "logical", "physical"}``; the totals cover every match, not just ``rows``."""
    empty = {"rows": [], "total": 0, "logical": 0, "physical": 0}
    names = list(dict.fromkeys(n for n in names if n))
    if not names or not sources:
        return empty
    cur = read_cursor(repo)
    try:
        sids = [i for i in (resolve.src_id(cur, s) for s in sources) if i is not None]
        if not sids:
            return empty
        where = ("p.src_id IN (%s) AND lower(p.name) IN (%s)"
                 % (",".join("?" * len(sids)), ",".join("?" * len(names))))
        params = [*sids, *[n.lower() for n in names]]
        total, logical, phys = cur.execute(
            f"SELECT count(*), sum(p.size), sum(p.physical) FROM pathx p WHERE {where}", params).fetchone()
        order = ("p.physical DESC NULLS LAST, p.size DESC, s.source, p.full_path" if biggest_first
                 else "s.source, p.full_path")
        rows = _rows(
            cur,
            f"SELECT s.source, p.full_path, p.size, p.physical, p.in_latest FROM pathx p "
            f"JOIN src s ON s.src_id = p.src_id WHERE {where} "
            f"ORDER BY {order} LIMIT {int(cap)}",
            params,
        )
    finally:
        cur.close()
    return {"rows": rows, "total": int(total or 0), "logical": int(logical or 0),
            "physical": int(phys or 0)}


def paths_for_contents(repo: str, object_ids: list[str], sources: list[str] | None = None,
                       cap: int = 500) -> dict:
    """Every (source, path) holding any of these content objects in any version - what
    ``remove-files --object-id`` removes inside the scoped sources."""
    oids = list(dict.fromkeys(o for o in object_ids if o))
    if not oids:
        return {"rows": [], "total": 0}
    cur = read_cursor(repo)
    try:
        ids = sorted(set(resolve.cid_ids(cur, oids).values()))
        if not ids:
            return {"rows": [], "total": 0}
        conds = ["f.cid_id IN (%s)" % ",".join("?" * len(ids))]
        params: list = list(ids)
        if sources is not None:
            sids = [i for i in (resolve.src_id(cur, s) for s in sources) if i is not None]
            if not sids:
                return {"rows": [], "total": 0}
            conds.append("p.src_id IN (%s)" % ",".join("?" * len(sids)))
            params.extend(sids)
        sub = ("SELECT DISTINCT p.src_id, p.full_path, p.size, p.in_latest FROM filev f "
               "JOIN pathx p ON p.path_id = f.path_id WHERE " + " AND ".join(conds))
        total = cur.execute(f"SELECT count(*) FROM ({sub})", params).fetchone()[0]
        rows = _rows(
            cur,
            f"SELECT s.source, x.full_path, x.size, x.in_latest FROM ({sub}) x "
            f"JOIN src s ON s.src_id = x.src_id ORDER BY s.source, x.full_path LIMIT {int(cap)}",
            params,
        )
    finally:
        cur.close()
    return {"rows": rows, "total": int(total or 0)}


def sources_for_content(repo: str, object_ids: list[str]) -> list[str]:
    """Every distinct source that references these content objects in ANY snapshot (all versions) -
    the complete set that a remove-files must scope so GC can actually free the content (a copy left
    in another source keeps it alive). Queries the `filev` delta table (every source that holds the
    content has >=1 row), so it is COMPLETE - not latest-version-only. No index on filev.cid_id,
    so it's a bounded columnar scan per build (~tens-to-low-hundreds of ms) - fine for a one-off
    Build action, not a hot path."""
    oids = list(dict.fromkeys(o for o in object_ids if o))
    if not oids:
        return []
    cur = read_cursor(repo)
    out: set[str] = set()
    try:
        id_of = resolve.cid_ids(cur, oids)
        ids = sorted(set(id_of.values()))
        for i in range(0, len(ids), 900):
            grp = ids[i:i + 900]
            q = ("SELECT DISTINCT p.src_id FROM filev f "
                 "JOIN pathx p ON p.path_id = f.path_id "
                 "WHERE f.cid_id IN (%s)" % ",".join("?" * len(grp)))
            for (s,) in cur.execute(q, grp).fetchall():
                name = resolve.source_of(cur, s)
                if name:
                    out.add(name)
    finally:
        cur.close()
    return sorted(out)


def _remove_where(cur, *, query: str, deleted_only: bool, min_size: int, source: str | None,
                  ext: str | None, max_size: int, date_from: str | None, date_to: str | None,
                  min_versions: int, shared_min: int, ext_exclude: str | None,
                  first_from: str | None, prefix: str | None) -> tuple[str, list]:
    """Shared WHERE for remove_targets / remove_targets_summary - one home so the two can
    never disagree about what the plan matches."""
    where, params = _search_where(cur, query, deleted_only, min_size, source, ext, max_size,
                                  date_from, date_to, min_versions, shared_min, ext_exclude,
                                  first_from=first_from)
    if prefix and prefix.rstrip(_SEP):
        scope, sp = scope_sql(cur, prefix, include_path=True)
        if scope:
            where.append(scope)
            params.extend(sp)
    return _where(where), params


def remove_targets(repo: str, *, query: str = "", deleted_only: bool = False, min_size: int = 0,
                   max_size: int = 0, source: str | None = None, ext: str | None = None,
                   ext_exclude: str | None = None, date_from: str | None = None,
                   date_to: str | None = None, min_versions: int = 0, shared_min: int = 0,
                   first_from: str | None = None,
                   prefix: str | None = None, all_versions: bool = False, cap: int = 200000) -> dict:
    """Files matching the CURRENT view/filter (optionally scoped to a folder ``prefix``), ordered
    by physical size DESC, plus the object-ids to feed ``remove-files`` - biggest files first so a
    chunked run reclaims the most space soonest. ``all_versions`` expands each path to EVERY
    version's object; otherwise just the latest content. Used by the chunked folder/filter delete."""
    cur = read_cursor(repo)
    try:
        where_sql, params = _remove_where(
            cur, query=query, deleted_only=deleted_only, min_size=min_size, source=source,
            ext=ext, max_size=max_size, date_from=date_from, date_to=date_to,
            min_versions=min_versions, shared_min=shared_min, ext_exclude=ext_exclude,
            first_from=first_from, prefix=prefix)
        # Unbounded totals for the SAME filter, so the UI can say "the 100 biggest files are 8.2 GiB
        # = 70% of this folder". Cheap (one indexed aggregate) and it is what makes a top-N delete a
        # judgement call rather than a guess - in a 10k-file folder a hundred files usually hold most
        # of the bytes.
        trow = cur.execute(
            f"SELECT count(*), COALESCE(sum(p.physical), 0), COALESCE(sum(p.size), 0) "
            f"FROM pathx p WHERE {where_sql}",
            params,
        ).fetchone() or (0, 0, 0)
        total_file_count, total_physical, total_logical = int(trow[0]), int(trow[1]), int(trow[2])
        files = _rows(
            cur,
            f"SELECT s.source AS source, p.full_path, c.content_id, p.size, p.physical FROM pathx p "
            f"JOIN src s ON s.src_id = p.src_id LEFT JOIN cid c ON c.cid_id = p.cid_id "
            f"WHERE {where_sql} "
            f"ORDER BY p.physical DESC NULLS LAST, p.size DESC, s.source, p.full_path LIMIT {int(cap)}",
            params,
        )
        logical = sum(int(f["size"] or 0) for f in files)
        physical = sum(int(f["physical"] or 0) for f in files)
        oids: list[str] = []
        seen: set[str] = set()
        if all_versions:
            # Map (source, full_path) -> all version object-ids: one batched path resolution per
            # source group (never a per-path loop - the KL47 regression shape), then one filev scan
            # per 900 path ids. Emitted in the files' physical-desc order so the biggest files'
            # objects come first.
            sid_of = resolve.src_map(cur)
            spairs = [(sid_of[s], fp) for s, fp in ((f["source"], f["full_path"]) for f in files)
                      if s in sid_of]
            pid_of = resolve.path_ids(cur, spairs)
            ver: dict[int, list[int]] = {}
            pids = list(pid_of.values())
            for i in range(0, len(pids), 900):
                grp = pids[i:i + 900]
                q = "SELECT path_id, cid_id FROM filev WHERE path_id IN (%s)" % ",".join("?" * len(grp))
                for pid, c in cur.execute(q, grp).fetchall():
                    if c is not None:
                        ver.setdefault(pid, []).append(c)
            cstr = resolve.content_ids(cur, sorted({c for v in ver.values() for c in v}))
            for f in files:
                sid = sid_of.get(f["source"])
                pid = pid_of.get((sid, f["full_path"])) if sid is not None else None
                for c in ver.get(pid, []) if pid is not None else []:
                    s = cstr.get(c)
                    if s and s not in seen:
                        seen.add(s)
                        oids.append(s)
        else:
            for f in files:
                cid = f.get("content_id")
                if cid and cid not in seen:
                    seen.add(cid)
                    oids.append(cid)
        # Basenames (deduped, biggest-files-first) for the --filename mechanism (mirrors the batch
        # panel's by-name removal). pairs = every matched (source, full_path) for on-demand detail.
        names: list[str] = []
        seenn: set[str] = set()
        for f in files:
            nm = f["full_path"].rsplit(_SEP, 1)[-1].rsplit("/", 1)[-1]
            if nm and nm not in seenn:
                seenn.add(nm)
                names.append(nm)
        pairs = [(f["source"], f["full_path"]) for f in files]
    finally:
        cur.close()
    return {
        "file_count": len(files),
        "total_file_count": total_file_count,   # the whole matched set, before the top-N cap
        "total_physical": total_physical,
        "total_logical": total_logical,
        "capped": total_file_count > len(files),
        "logical": logical,
        "physical": physical,
        "object_ids": oids,             # deduped, biggest-files-first
        "names": names,                 # deduped basenames, biggest-files-first
        "pairs": pairs,                 # all (source, full_path) - server-side only (detail/batch)
        "files": files[:200],           # preview (already physical-desc)
        "sources": sorted({f["source"] for f in files}),
    }


def remove_targets_summary(repo: str, *, query: str = "", deleted_only: bool = False,
                           min_size: int = 0, max_size: int = 0, source: str | None = None,
                           ext: str | None = None, ext_exclude: str | None = None,
                           date_from: str | None = None, date_to: str | None = None,
                           min_versions: int = 0, shared_min: int = 0, first_from: str | None = None,
                           prefix: str | None = None, cap: int = 200000,
                           detail_limit: int = 5000) -> dict:
    """ The cheap pre-read /actions/remove-detail needs: uncapped totals, the distinct
    sources of the WHOLE match (the folder-vs-selection decision) and only the top
    ``detail_limit`` (source, full_path) pairs the detail walk actually uses. Never builds the
    full top-``cap`` row set, never expands versions or names - the 200k-row materialization that
    blew DuckDB's memory ceiling on a broad folder (observed 2026-09-09) is gone from this path."""
    cur = read_cursor(repo)
    try:
        where_sql, params = _remove_where(
            cur, query=query, deleted_only=deleted_only, min_size=min_size, source=source,
            ext=ext, max_size=max_size, date_from=date_from, date_to=date_to,
            min_versions=min_versions, shared_min=shared_min, ext_exclude=ext_exclude,
            first_from=first_from, prefix=prefix)
        trow = cur.execute(
            f"SELECT count(*), COALESCE(sum(p.physical), 0), COALESCE(sum(p.size), 0) "
            f"FROM pathx p WHERE {where_sql}",
            params,
        ).fetchone() or (0, 0, 0)
        total_file_count, total_physical, total_logical = int(trow[0]), int(trow[1]), int(trow[2])
        sources = [r[0] for r in cur.execute(
            f"SELECT DISTINCT s.source FROM pathx p JOIN src s ON s.src_id = p.src_id "
            f"WHERE {where_sql} ORDER BY s.source",
            params,
        ).fetchall()]
        pairs = [(r[0], r[1]) for r in cur.execute(
            f"SELECT s.source, p.full_path FROM pathx p JOIN src s ON s.src_id = p.src_id "
            f"WHERE {where_sql} "
            f"ORDER BY p.physical DESC NULLS LAST, p.size DESC, s.source, p.full_path "
            f"LIMIT {int(detail_limit)}",
            params,
        ).fetchall()]
    finally:
        cur.close()
    return {
        "total_file_count": total_file_count,
        "total_physical": total_physical,
        "total_logical": total_logical,
        "capped": total_file_count > cap,
        "sources": sources,
        "pairs": pairs,
    }


def _src_rule_ids(cur) -> tuple[list[int], list[int]]:
    """(case-insensitive src_ids, case-sensitive src_ids).

    Read from the generation's own ``src`` table, not ``sources_index``: this runs inside SQL
    helpers, and a listing call there could spawn kopia on a cold cache."""
    from ..core import paths
    ci: list[int] = []
    cs: list[int] = []
    for src_id, key in cur.execute("SELECT src_id, source FROM src").fetchall():
        (cs if paths.case_sensitive(str(key)) else ci).append(int(src_id))
    return ci, cs


def _id_list(ids: list[int]) -> str:
    return ",".join(str(i) for i in ids)


def _match_dirs(cur, path: str, ci: list[int], cs: list[int]) -> list[int]:
    """The ``dir`` ids whose ``dir_path`` IS ``path``, each source matched by its own case rule."""
    parts: list[str] = []
    params: list = []
    if ci:
        parts.append(f"(lower(dir_path) = ? AND src_id IN ({_id_list(ci)}))")
        params.append(path.lower())
    if cs:
        parts.append(f"(dir_path = ? AND src_id IN ({_id_list(cs)}))")
        params.append(path)
    if not parts:
        return []
    return [int(r[0]) for r in cur.execute(
        f"SELECT dir_id FROM dir WHERE {' OR '.join(parts)}", params).fetchall()]


def _name_match(alias: str, name: str, ci: list[int], cs: list[int]) -> tuple[str, list]:
    """``(sql, params)``: a pathx row's own NAME, under its source's case rule."""
    parts: list[str] = []
    params: list = []
    if ci:
        parts.append(f"(lower({alias}.name) = ? AND {alias}.src_id IN ({_id_list(ci)}))")
        params.append(name.lower())
    if cs:
        parts.append(f"({alias}.name = ? AND {alias}.src_id IN ({_id_list(cs)}))")
        params.append(name)
    return ("(" + " OR ".join(parts) + ")", params) if parts else ("1 = 0", [])


def dir_scope_sql(cur, prefix: str, alias: str = "p") -> tuple[str, list]:
    """``(sql, params)``: ``{alias}.dir_id`` is ``prefix`` itself or anywhere under it.

    The folder is found by an equality on ``dir`` (664k rows) and its subtree comes from the
    ``anc`` closure table, so no string work touches the 5.3M-row ``pathx`` column: measured
    ~110 ms against ~205-360 ms for ``lower(full_path) LIKE 'prefix%'`` on a 125k-row subtree,
    and the folder panel asks five of these per open. Case follows each SOURCE's rule
    (``core.paths``): folded where the source folds, exact where it does not."""
    base = prefix.rstrip("\\/")
    if not base:
        return "", []
    ci, cs = _src_rule_ids(cur)
    ids = _match_dirs(cur, base, ci, cs)
    if not ids:
        return "1 = 0", []          # no such folder in this repo: nothing is under it
    return (f"{alias}.dir_id IN (SELECT a.dir_id FROM anc a WHERE a.anc_id IN ({_id_list(ids)}))",
            [])


def scope_sql(cur, prefix: str, *, include_path: bool = True,
              alias: str = "p") -> tuple[str, list]:
    """``(sql, params)``: a ``pathx`` row named ``prefix`` or under it.

    ``include_path`` adds the path's own row, which the old ``full_path = ? OR LIKE ?`` sites
    relied on. The two shapes are mutually exclusive: ``pathx`` holds no folder rows (verified
    against the build), so a prefix that resolves to a folder scopes to its subtree and anything
    else is matched as (parent folder, name). Keeping them as ONE branch is deliberate - an OR of
    the two costs a full ``pathx`` scan (~500 ms) for an answer that cannot differ otherwise. A
    file at the same path as a folder can only exist ACROSS sources and is not covered; no repo
    has that shape (checked), and the folder answer is the one a folder panel wants.

    POSIX prefixes stay on the old scan shape: the build stores POSIX ``dir`` paths without their
    leading separator, so the closure table cannot identify them yet (```` is the
    cross-platform pass)."""
    base = prefix.rstrip("\\/")
    if not base:
        return "", []
    if base.startswith("/"):
        like = like_literal(base.lower()) + "/" + "%"
        return (f"((lower({alias}.full_path) = ? OR lower({alias}.full_path) LIKE ?{_ESC}))",
                [base.lower(), like])
    ci, cs = _src_rule_ids(cur)
    folder_ids = _match_dirs(cur, base, ci, cs)
    if folder_ids:
        return (f"{alias}.dir_id IN (SELECT a.dir_id FROM anc a WHERE a.anc_id IN "
                f"({_id_list(folder_ids)}))", [])
    if not include_path:
        return "1 = 0", []
    parent, _, name = base.rpartition(_SEP)
    parent_ids = _match_dirs(cur, parent, ci, cs) if parent and name else []
    if not parent_ids:
        return "1 = 0", []
    name_sql, name_params = _name_match(alias, name, ci, cs)
    return (f"{alias}.dir_id IN ({_id_list(parent_ids)}) AND {name_sql}", name_params)


def folder_sources(repo: str, prefix: str) -> list[dict]:
    """Every source that backs up this folder, with what each one holds.

    A folder living inside two sources (e.g. ``G:\\Apps\\X`` covered by both the ``G:\\Apps`` and
    ``G:\\`` sources) is snapshotted twice, so deleting it from one source leaves it in the other.
    That is the single most confusing thing about folder deletion, hence surfacing it up front."""
    cur = read_cursor(repo)
    try:
        where, params = scope_sql(cur, prefix, include_path=True)
        return _rows(
            cur,
            "SELECT s.source AS source, count(*) AS files, COALESCE(sum(p.physical), 0) AS physical, "
            "  COALESCE(sum(p.size), 0) AS logical, min(p.first_seen) AS first_seen, "
            "  max(p.last_seen) AS last_seen, "
            "  count(*) FILTER (WHERE NOT p.in_latest) AS deleted_files "
            "FROM pathx p JOIN src s ON s.src_id = p.src_id "
            f"WHERE {where or '1 = 1'} "
            "GROUP BY s.source ORDER BY physical DESC, s.source",
            params,
        )
    finally:
        cur.close()


def folder_summary(repo: str, prefix: str, source: str | None = None) -> dict:
    """Cheap, instantly-available folder facts (no chunk walk): counts, sizes, dates, versions.
    The expensive freeable/shared numbers come separately from ``physical.folder_detail`` so the
    panel can render immediately and fill those in afterwards."""
    cur = read_cursor(repo)
    try:
        scope, params = scope_sql(cur, prefix, include_path=True)
        conds = [scope] if scope else []
        if source:
            cond, sp = _src_cond_exact(cur, source)
            conds.append(cond)
            params.extend(sp)
        where = _where(conds)
        row = cur.execute(
            f"SELECT count(*), COALESCE(sum(p.physical), 0), COALESCE(sum(p.size), 0), "
            f"  min(p.first_seen), max(p.last_seen), count(*) FILTER (WHERE NOT p.in_latest), "
            f"  COALESCE(sum(p.versions), 0), count(DISTINCT p.src_id) "
            f"FROM pathx p WHERE {where}",
            params,
        ).fetchone() or (0, 0, 0, None, None, 0, 0, 0)
    finally:
        cur.close()
    return {
        "prefix": prefix, "files": int(row[0]), "physical": int(row[1]), "logical": int(row[2]),
        "first_seen": str(row[3]) if row[3] else None, "last_seen": str(row[4]) if row[4] else None,
        "deleted_files": int(row[5]), "versions": int(row[6]), "source_count": int(row[7]),
    }


def folder_top_children(repo: str, prefix: str, limit: int = 5, source: str | None = None) -> list[dict]:
    """The biggest immediate children of a folder, by physical - answers "what is actually big in
    here" without making the user drill down."""
    base = prefix.rstrip(_SEP)
    cur = read_cursor(repo)
    try:
        scope, params = scope_sql(cur, prefix, include_path=False)
        conds = [scope] if scope else []
        if source:
            cond, sp = _src_cond_exact(cur, source)
            conds.append(cond)
            params.extend(sp)
        where = _where(conds)
        return _rows(
            cur,
            f"SELECT list_extract(string_split(substr(p.full_path, ?), ?), 1) AS child, "
            f"  COALESCE(sum(p.physical), 0) AS physical, count(*) AS files "
            f"FROM pathx p WHERE {where} GROUP BY child "
            f"ORDER BY physical DESC, child LIMIT {int(limit)}",
            [len(base) + 2, _SEP, *params],
        )
    finally:
        cur.close()


def _folder_scope(cur, prefix: str, source: str | None) -> tuple[str, list]:
    scope, params = scope_sql(cur, prefix, include_path=False)
    conds = [scope] if scope else []
    if source:
        cond, sp = _src_cond_exact(cur, source)
        conds.append(cond)
        params.extend(sp)
    return _where(conds), params


def folder_file_types(repo: str, prefix: str, source: str | None = None, limit: int = 8) -> list[dict]:
    """Files under a folder grouped by extension, biggest first; the rest rolled into ``other``."""
    cur = read_cursor(repo)
    try:
        where, params = _folder_scope(cur, prefix, source)
        base = f"list_extract(string_split(p.full_path, ?), -1)"
        rows = _rows(
            cur,
            f"SELECT ext, count(*) AS files, COALESCE(sum(physical), 0) AS physical, "
            f"  COALESCE(sum(size), 0) AS logical "
            f"FROM (SELECT CASE WHEN len(string_split({base}, '.')) > 1 "
            f"        THEN lower(list_extract(string_split({base}, '.'), -1)) ELSE '' END AS ext, "
            f"        p.physical, p.size FROM pathx p WHERE {where}) "
            f"GROUP BY ext ORDER BY physical DESC, ext",
            [_SEP, _SEP, *params],
        )
    finally:
        cur.close()
    if len(rows) <= limit:
        return rows
    head, tail = rows[:limit - 1], rows[limit - 1:]
    head.append({"ext": None, "files": sum(r["files"] for r in tail),
                 "physical": sum(r["physical"] for r in tail), "logical": sum(r["logical"] for r in tail)})
    return head


def folder_contents(repo: str, prefix: str, source: str | None = None, limit: int = 500) -> list[dict]:
    """Everything directly inside a folder: one row per child folder or file, folders first,
    each with size, file count, versions, last backup and a source to open it from."""
    base = prefix.rstrip(_SEP)
    cur = read_cursor(repo)
    try:
        where, params = _folder_scope(cur, prefix, source)
        return _rows(
            cur,
            f"SELECT child, bool_or(strpos(rest, ?) > 0) AS is_folder, count(*) AS files, "
            f"  COALESCE(sum(physical), 0) AS physical, COALESCE(sum(size), 0) AS logical, "
            f"  COALESCE(sum(versions), 0) AS versions, max(last_seen) AS last_seen, "
            f"  count(*) FILTER (WHERE NOT in_latest) AS deleted_files, min(source) AS source "
            f"FROM (SELECT substr(p.full_path, ?) AS rest, "
            f"        list_extract(string_split(substr(p.full_path, ?), ?), 1) AS child, "
            f"        p.physical, p.size, p.versions, p.last_seen, p.in_latest, s.source "
            f"      FROM pathx p JOIN src s ON s.src_id = p.src_id WHERE {where}) "
            f"GROUP BY child ORDER BY is_folder DESC, physical DESC, child LIMIT {int(limit)}",
            [_SEP, len(base) + 2, len(base) + 2, _SEP, *params],
        )
    finally:
        cur.close()


def flat_files(repo: str, source: str, prefix: str = "", deleted_only: bool = False,
               min_size: int = 0, ext: str | None = None, ext_exclude: str | None = None,
               limit: int = 20000) -> list[dict]:
    """Recursive FLAT file list under a prefix (collapse the folder hierarchy - the Tree's
    'just show me the files' view, like Search but anchored to a tree location). Paths-based, so it
    works in any tree mode; ordered biggest-physical-first. ``name`` carries the full path so the
    flat table shows where each file lives."""
    cur = read_cursor(repo)
    try:
        where, params = _search_where(cur, "", deleted_only, min_size, source, ext, 0,
                                      None, None, 0, 0, ext_exclude)
        if prefix.rstrip(_SEP):
            scope, sp = scope_sql(cur, prefix, include_path=True)
            if scope:
                where.append(scope)
                params.extend(sp)
        where_sql = _where(where)
        rows = _rows(
            cur,
            f"SELECT s.source AS source, p.full_path AS path, p.full_path AS name, p.size, p.physical, "
            f"p.last_seen AS modified, c.content_id FROM pathx p "
            f"JOIN src s ON s.src_id = p.src_id LEFT JOIN cid c ON c.cid_id = p.cid_id "
            f"WHERE {where_sql} "
            f"ORDER BY p.physical DESC NULLS LAST, p.size DESC, s.source, p.full_path LIMIT {int(limit)}",
            params,
        )
    finally:
        cur.close()
    for r in rows:
        r["type"] = "file"
        r["files"] = 1
        r["modified"] = str(r["modified"]) if r.get("modified") else ""
    return rows


def _source_root_path(source: str) -> str:
    return source.split(":", 1)[1] if ":" in source else source


def _physical_for_objects(repo: str, objs: list[str]) -> dict[str, int]:
    """Packed size per file object, from the generation's maps: `csize` for a plain object,
    `iphys` for a split one (its index content alone would read as a few bytes)."""
    objs = [o for o in dict.fromkeys(objs) if o]
    if not objs:
        return {}
    cur = read_cursor(repo)
    try:
        id_of = resolve.cid_ids(cur, objs)
        plain = sorted({c for o, c in id_of.items() if not o.startswith("I")})
        split = sorted({c for o, c in id_of.items() if o.startswith("I")})
        packed: dict[int, int] = {}
        for table, ids in (("csize", plain), ("iphys", split)):
            for i in range(0, len(ids), 900):
                grp = ids[i:i + 900]
                if not grp:
                    continue
                q = (f"SELECT cid_id, packed FROM {table} WHERE cid_id IN (%s)"
                     % ",".join("?" * len(grp)))
                for c, p in cur.execute(q, grp).fetchall():
                    packed[c] = p
        rev = {c: o for o, c in id_of.items()}
        return {rev[c]: p for c, p in packed.items() if c in rev}
    finally:
        cur.close()


def tree_children_live(repo: str, source: str, root_obj: str, prefix: str) -> list[dict]:
    """FULL point-in-time tree of one snapshot: the snapshot's directory objects come from the
    generation's tree, with a kopia fallback for folders newer than the last sync (Kopia-accurate,
    complete - not the delta index). Folder sizes come from each entry's own summary (no deep
    walk); per-file physical comes from the generation's `csize`/`iphys` maps."""
    root = _source_root_path(source)
    rel = prefix[len(root):] if prefix.startswith(root) else prefix
    oid = root_obj
    for seg in [s for s in rel.split(_SEP) if s]:
        kids = entries.children_of(repo, [oid]).get(oid, [])
        match = next((e for e in kids if e.get("type") == "d" and e.get("name") == seg), None)
        if not match:
            return []
        oid = match.get("obj")
    kids = entries.children_of(repo, [oid]).get(oid, [])
    phys = _physical_for_objects(repo, [e.get("obj") for e in kids
                                        if e.get("type") == "f" and e.get("obj")])

    base = prefix.rstrip(_SEP)
    children: list[dict] = []
    for e in kids:
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
            children.append({
                "path": path, "name": name, "size": int(e.get("size", 0) or 0),
                "files": 1, "modified": e.get("mtime", "") or "",
                "type": "file", "physical": phys.get(obj, 0), "content_id": obj,
            })
    return children


def tree_children_all(repo: str, prefix: str, latest_only: bool = False, deleted_only: bool = False,
                      min_size: int = 0, ext: str | None = None, ext_exclude: str | None = None) -> list[dict]:
    """ALL-SOURCES aggregate tree: every source's paths merged under a prefix, **DEDUPED by
    full_path** (a path backed by several sources counts once). ``latest_only`` keeps only paths in
    their source's latest snapshot (= 'latest snapshot of each source' view, A#2); else all-snapshots
    aggregate (A#1). Node sizes are per-location; the true repo-wide deduped total is the dashboard's
    `union_total` (siblings can overlap on shared content). Top level = merged drive roots."""
    cur = read_cursor(repo)
    try:
        where, params = _search_where(cur, "", deleted_only, min_size, None, ext, 0, None, None, 0, 0, ext_exclude)
        if latest_only:
            where.append("p.in_latest = true")
        base = prefix.rstrip(_SEP)
        if base:
            scope, sp = scope_sql(cur, prefix, include_path=False)
            if scope:
                where.append(scope)
                params.extend(sp)
        where_sql = _where(where)
        rows = _rows(
            cur,
            f"""
            WITH dp AS (
                SELECT p.full_path, max(p.size) AS size, max(p.physical) AS physical, max(p.last_seen) AS last_seen
                FROM pathx p WHERE {where_sql} GROUP BY p.full_path
            ), sub AS (
                SELECT size, physical, last_seen,
                       CASE WHEN ? = '' THEN full_path ELSE substr(full_path, length(?) + 2) END AS remainder
                FROM dp
            )
            SELECT CASE
                     -- UNC root: split_part returns an empty name before the leading separator,
                     -- which used to lump every UNC source into one blank-named top node. Name it
                     -- by its first two segments (host + share) instead.
                     WHEN starts_with(remainder, ?) THEN
                       ? || split_part(substr(remainder, 3), ?, 1) || ? || split_part(substr(remainder, 3), ?, 2)
                     ELSE split_part(remainder, ?, 1)
                   END AS name,
                   sum(size) AS size, sum(physical) AS physical,
                   count(*) AS files, max(last_seen) AS modified,
                   max(CASE WHEN position(? IN remainder) > 0 THEN 1 ELSE 0 END) AS is_folder
            FROM sub WHERE remainder <> '' GROUP BY name ORDER BY size DESC, name
            """,
            [*params, base, base,
             _SEP * 2, _SEP * 2, _SEP, _SEP, _SEP, _SEP, _SEP],
        )
    finally:
        cur.close()
    out = []
    for r in rows:
        name = r["name"]
        out.append({
            "path": (base + _SEP + name) if base else name, "name": name,
            "size": r["size"] or 0, "physical": r["physical"] or 0, "files": r["files"],
            "modified": str(r["modified"]) if r["modified"] else "",
            "type": "folder" if r["is_folder"] else "file",
        })
    return out


def tree_children_snaps(repo: str, snapshot_ids: list[str], prefix: str = "") -> list[dict]:
    """Immediate children under a prefix across a SET of snapshots (e.g. each source's latest), from
    the delta `files` table, deduped by full_path. Powers the all-sources 'changed' view."""
    if not snapshot_ids:
        return []
    base = prefix.rstrip(_SEP)
    cur = read_cursor(repo)
    try:
        sids = [i for i in (resolve.snap_id(cur, s) for s in snapshot_ids) if i is not None]
        if not sids:  # none of the given snapshot ids exist: no rows before, none now
            return []
        ph = ",".join("?" * len(sids))
        scope, sp = scope_sql(cur, prefix, include_path=False)
        cond = f" AND {scope}" if scope else ""
        rows = _rows(
            cur,
            f"""
            WITH dp AS (
                SELECT p.full_path, max(f.size) AS size, max(f.mtime) AS mtime
                FROM filev f
                JOIN pathx p ON p.path_id = f.path_id
                WHERE f.snap_id IN ({ph}){cond}
                GROUP BY p.full_path
            ), sub AS (
                SELECT size, mtime,
                       CASE WHEN ? = '' THEN full_path ELSE substr(full_path, length(?) + 2) END AS remainder
                FROM dp
            )
            SELECT CASE
                     -- UNC root: split_part returns an empty name before the leading separator,
                     -- which used to lump every UNC source into one blank-named node (same fix
                     -- as tree_children_all). Name it by host + share instead.
                     WHEN starts_with(remainder, ?) THEN
                       ? || split_part(substr(remainder, 3), ?, 1) || ? || split_part(substr(remainder, 3), ?, 2)
                     ELSE split_part(remainder, ?, 1)
                   END AS name, sum(size) AS size, count(*) AS files,
                   max(mtime) AS modified,
                   max(CASE WHEN position(? IN remainder) > 0 THEN 1 ELSE 0 END) AS is_folder
            FROM sub WHERE remainder <> '' GROUP BY name ORDER BY size DESC, name
            """,
            [*sids, *sp, base, base,
             _SEP * 2, _SEP * 2, _SEP, _SEP, _SEP, _SEP, _SEP],
        )
    finally:
        cur.close()
    out = []
    for r in rows:
        name = r["name"]
        out.append({
            "path": (base + _SEP + name) if base else name, "name": name,
            "size": r["size"] or 0, "physical": None, "files": r["files"],
            "modified": str(r["modified"]) if r["modified"] else "",
            "type": "folder" if r["is_folder"] else "file",
        })
    return out


def tree_children_paths(repo: str, source: str, prefix: str, deleted_only: bool = False,
                        min_size: int = 0, ext: str | None = None,
                        ext_exclude: str | None = None) -> list[dict]:
    """ALL-snapshots (deduped) tree for a source: aggregate the distinct ``paths`` rows under a
    prefix - the source's total file set across every snapshot, not a point-in-time view. Optional
    filters (deleted-from-disk / >= size / ext include+exclude) keep only matching descendants, so
    folders with no match drop out (e.g. 'folders containing deleted files')."""
    base = prefix.rstrip(_SEP)
    cur = read_cursor(repo)
    try:
        sid = resolve.src_id(cur, source)
        if sid is None:  # unknown source: no rows before, none now
            return []
        scope, sp = scope_sql(cur, prefix, include_path=False)
        conds = ["p.src_id = ?"] + ([scope] if scope else [])
        params: list = [sid, *sp]
        if deleted_only:
            conds.append("p.in_latest = false")
        if min_size:
            conds.append("p.size >= ?")
            params.append(int(min_size))
        if ext:
            exts = _split_exts(ext)
            if exts:
                conds.append("p.ext IN (%s)" % ",".join("?" * len(exts)))
                params.extend(exts)
        if ext_exclude:
            exx = _split_exts(ext_exclude)
            if exx:
                conds.append("(p.ext IS NULL OR p.ext NOT IN (%s))" % ",".join("?" * len(exx)))
                params.extend(exx)
        where_sql = _where(conds)
        rows = _rows(
            cur,
            f"""
            WITH sub AS (
                SELECT p.size, p.physical, p.last_seen,
                       CASE WHEN ? = '' THEN p.full_path
                            ELSE substr(p.full_path, length(?) + 2) END AS remainder
                FROM pathx p
                WHERE {where_sql}
            )
            SELECT CASE
                     -- UNC root: name it by host + share (same fix as tree_children_all).
                     WHEN starts_with(remainder, ?) THEN
                       ? || split_part(substr(remainder, 3), ?, 1) || ? || split_part(substr(remainder, 3), ?, 2)
                     ELSE split_part(remainder, ?, 1)
                   END AS name,
                   sum(size) AS size,
                   sum(physical) AS physical,
                   count(*) AS files,
                   max(last_seen) AS modified,
                   max(CASE WHEN position(? IN remainder) > 0 THEN 1 ELSE 0 END) AS is_folder
            FROM sub
            WHERE remainder <> ''
            GROUP BY name
            ORDER BY size DESC, name
            """,
            [base, base, *params, _SEP * 2, _SEP * 2, _SEP, _SEP, _SEP, _SEP, _SEP],
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
