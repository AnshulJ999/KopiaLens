"""DuckDB query layer for search, TreeSize, and time-travel views.

All reads go through a thread-local cursor off the shared pooled connection
(``read_cursor``) - no per-query file open or schema DDL.
"""

from __future__ import annotations

import os
import threading
import time
import unicodedata
import uuid
from typing import NamedTuple

from config import DEMO_INSTANCE, is_demo

from ..core import paths as pathrules
from ..core.applog import get_logger
from . import entries, lanes, resolve
from .store import read_cursor

log = get_logger("queries")

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


def _text_match(query: str, alias: str = "p") -> tuple[str, list]:
    """``(sql, params)``: the path contains ``query``, case-insensitively, in either Unicode form -
    "café" typed as one character also finds a name stored decomposed (macOS), and back."""
    forms = list(dict.fromkeys(unicodedata.normalize(f, query) for f in ("NFC", "NFD")))
    sql = " OR ".join([f"{alias}.full_path ILIKE ?{_ESC}"] * len(forms))
    return f"({sql})", [f"%{like_literal(f)}%" for f in forms]


def _rows(cur, sql: str, params: list | None = None) -> list[dict]:
    cur.execute(sql, params or [])
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# Answers that paging and re-sorting repeat (a search's total, a folder list), kept per generation:
# a new generation is a new key, so nothing stale is ever served.
_MEMO: dict[tuple, object] = {}
_MEMO_MAX = 64
_MEMO_LOCK = threading.Lock()


def _memo_key(cur, *parts) -> tuple | None:
    gen = getattr(cur, "generation", None)
    ident = (getattr(gen, "repo", None), getattr(gen, "id", None), getattr(gen, "_mtime_ns", None))
    if None in ident:
        return None
    key = (*ident, *parts)
    try:
        hash(key)
    except TypeError:
        return None
    return key


def _memo(key: tuple | None, compute):
    if key is None:
        return compute()
    with _MEMO_LOCK:
        if key in _MEMO:
            _MEMO[key] = _MEMO.pop(key)     # most recently used last
            return _MEMO[key]
    value = compute()
    with _MEMO_LOCK:
        _MEMO[key] = value
        while len(_MEMO) > _MEMO_MAX:
            _MEMO.pop(next(iter(_MEMO)))
    return value


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

# The computer part of a source ("user@host:path"), case-folded: kopia lowercases its default
# hostname but older or other clients may not. Dedupe by path merges only within one computer.
_HOST_SQL = "lower(regexp_extract(s.source, '^[^@]*@([^:]*)', 1))"


def _per_file(where_sql: str, extra: str = "", joins: str = "") -> str:
    """SQL: the ``pathx`` rows matching ``where_sql`` folded to one row per FILE (). A path
    held by nested sources on one computer is one file; ``sources`` lists every source holding it
    and ``source`` is the first of them. Sizes are the newest version's (the holder that saw it last)."""
    return (f"SELECT {_HOST_SQL} AS host, p.full_path, min(s.source) AS source, "
            f"list_sort(list(DISTINCT s.source)) AS sources, arg_max(p.size, coalesce(p.last_seen, TIMESTAMP '1970-01-01')) AS size, "
            f"arg_max(p.physical, coalesce(p.last_seen, TIMESTAMP '1970-01-01')) AS physical, bool_or(p.in_latest) AS in_latest{extra} "
            f"FROM pathx p JOIN src s ON s.src_id = p.src_id {joins} WHERE {where_sql} "
            f"GROUP BY {_HOST_SQL}, p.full_path")


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
        cond, qp = _text_match(query)
        where.append(cond)
        params.extend(qp)
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


@lanes.heavy_read
def search_paths(repo: str, query: str = "", deleted_only: bool = False, min_size: int = 0,
                 source: str | None = None, ext: str | None = None, limit: int = 500, offset: int = 0,
                 sort: str = "size", direction: str = "desc", confirm_exists: bool = False,
                 max_size: int = 0, date_from: str | None = None, date_to: str | None = None,
                 min_versions: int = 0, shared_min: int = 0, dedupe: bool = False,
                 ext_exclude: str | None = None, first_from: str | None = None,
                 dedupe_by: str = "path") -> dict:
    """Search the pre-aggregated ``paths`` table. Returns the TRUE total count plus one page,
    with sort + filters applied in SQL over the entire matched set. ``dedupe`` merges rows:
    ``dedupe_by="path"`` gives one row per file path (nested sources hold the same file; the
    newest version's values are shown), ``"content"`` one row per identical content.

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
        value_sql = ", r.value AS ranked_value" if mat else ""
        where, params = _search_where(cur, query, deleted_only, min_size, source, ext, max_size,
                                      date_from, date_to, min_versions, shared_min, ext_exclude, first_from)
        where_sql = _where(where)
        by_path = dedupe and dedupe_by != "content"
        # Merged rows: the grouping carries numbers plus one representative row (the newest
        # version's path_id, the highest one on a tie, so every text column comes from the same
        # row on every run); text comes from that row once the page is picked. Text aggregates
        # over every group need all of it in memory. A text sort column is the exception: it is
        # aggregated, since the order needs it for every group.
        rank_sql = ", max(r.value) AS ranked_value" if mat else ""
        texts = {"source": "s.source", "full_path": "p.full_path", "name": "p.name", "ext": "p.ext"}
        tcol = _SEARCH_SORT.get(sort)

        def _text(col: str, row: str) -> str:
            return f"g.{col}" if tcol == col else f"{row}.{col}"

        def _sorted_text(keys: tuple) -> str:
            return f", arg_max({texts[tcol]}, p.last_seen) AS {tcol}" if tcol in texts and tcol not in keys else ""

        def _attach(order: str, extra: str = "") -> str:
            return (f"FROM g JOIN pathx rp ON rp.path_id = g.rep JOIN src rs ON rs.src_id = rp.src_id {extra}"
                    f" {order}")
        if by_path:
            total = _memo(_memo_key(cur, "total-path", where_sql, tuple(params)), lambda: cur.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT {_HOST_SQL}, p.full_path FROM pathx p "
                f"JOIN src s ON s.src_id = p.src_id WHERE {where_sql})", params).fetchone()[0])
            order = _order_clause(sort, direction, "full_path", nulls_last=bool(mat))
            rows = _rows(
                cur,
                f"""
                WITH g AS (
                    SELECT p.full_path, max(struct_pack(ls := coalesce(p.last_seen, TIMESTAMP '1970-01-01'), pid := p.path_id)).pid AS rep,
                           arg_max(p.size, p.last_seen) AS size, arg_max(p.physical, p.last_seen) AS physical,
                           max(p.exclusive) AS exclusive, max(p.versions) AS versions,
                           min(p.first_seen) AS first_seen, max(p.last_seen) AS last_seen,
                           bool_or(p.in_latest) AS in_latest {_sorted_text(("full_path",))} {rank_sql}
                    FROM pathx p
                    JOIN src s ON s.src_id = p.src_id
                    {join_sql}
                    WHERE {where_sql}
                    GROUP BY {_HOST_SQL}, p.full_path
                    {order}
                    LIMIT ? OFFSET ?)
                SELECT {_text("source", "rs")} AS source, g.full_path, {_text("name", "rp")} AS name,
                       {_text("ext", "rp")} AS ext, g.size, g.physical, g.exclusive, g.versions,
                       g.first_seen, g.last_seen, g.in_latest, c.content_id {", g.ranked_value" if mat else ""}
                {_attach(order, "LEFT JOIN cid c ON c.cid_id = rp.cid_id")}
                """,
                [*params, limit, offset],
            )
        elif dedupe:
            total = _memo(_memo_key(cur, "total-content", where_sql, tuple(params)), lambda: cur.execute(
                f"SELECT count(DISTINCT p.cid_id) FROM pathx p "
                f"WHERE {where_sql} AND p.cid_id IS NOT NULL",
                params,
            ).fetchone()[0])
            order = _order_clause(sort, direction, "content_id", nulls_last=bool(mat))
            if tcol == "full_path":
                # Sorting merged contents by path: the representative's path is joined while the page
                # is picked, so only the page's paths are held. Ties fall to cid_id: two contents
                # never share a path and its last-seen time (a path holds one content per snapshot).
                dirn = "ASC" if str(direction).lower() == "asc" else "DESC"
                rows = _rows(
                    cur,
                    f"""
                    WITH g AS (
                        SELECT p.cid_id, max(struct_pack(ls := coalesce(p.last_seen, TIMESTAMP '1970-01-01'), pid := p.path_id)).pid AS rep,
                               max(p.size) AS size, max(p.physical) AS physical, max(p.versions) AS versions,
                               max(p.exclusive) AS exclusive, min(p.first_seen) AS first_seen,
                               max(p.last_seen) AS last_seen, bool_or(p.in_latest) AS in_latest {rank_sql}
                        FROM pathx p
                        {join_sql}
                        WHERE {where_sql} AND p.cid_id IS NOT NULL
                        GROUP BY p.cid_id),
                    pg AS (
                        SELECT g.*, rp.full_path FROM g JOIN pathx rp ON rp.path_id = g.rep
                        ORDER BY full_path {dirn}, last_seen DESC, cid_id
                        LIMIT ? OFFSET ?)
                    SELECT rs.source, pg.full_path, rp.name, rp.ext, pg.size, pg.physical, pg.versions,
                           pg.exclusive, pg.first_seen, pg.last_seen, pg.in_latest, c.content_id, pg.cid_id
                           {", pg.ranked_value" if mat else ""}
                    FROM pg JOIN pathx rp ON rp.path_id = pg.rep JOIN src rs ON rs.src_id = rp.src_id
                    LEFT JOIN cid c ON c.cid_id = pg.cid_id
                    ORDER BY pg.full_path {dirn}, pg.last_seen DESC, pg.cid_id
                    """,
                    [*params, limit, offset],
                )
            else:
                rows = _rows(
                    cur,
                    f"""
                    WITH g AS (
                        SELECT p.cid_id, c.content_id, max(struct_pack(ls := coalesce(p.last_seen, TIMESTAMP '1970-01-01'), pid := p.path_id)).pid AS rep,
                               max(p.size) AS size, max(p.physical) AS physical, max(p.versions) AS versions,
                               max(p.exclusive) AS exclusive,
                               min(p.first_seen) AS first_seen, max(p.last_seen) AS last_seen,
                               bool_or(p.in_latest) AS in_latest {_sorted_text(())} {rank_sql}
                        FROM pathx p
                        JOIN src s ON s.src_id = p.src_id
                        LEFT JOIN cid c ON c.cid_id = p.cid_id
                        {join_sql}
                        WHERE {where_sql} AND p.cid_id IS NOT NULL
                        GROUP BY p.cid_id, c.content_id
                        {order}
                        LIMIT ? OFFSET ?)
                    SELECT {_text("source", "rs")} AS source, {_text("full_path", "rp")} AS full_path,
                           {_text("name", "rp")} AS name, {_text("ext", "rp")} AS ext, g.size, g.physical,
                           g.versions, g.exclusive, g.first_seen, g.last_seen, g.in_latest, g.content_id,
                           g.cid_id {", g.ranked_value" if mat else ""}
                    {_attach(order)}
                    """,
                    [*params, limit, offset],
                )
            # Copies and sources for the page's contents only, over the same filtered rows: counting
            # distinct paths for every content group holds every path in memory.
            ids = [r["cid_id"] for r in rows]
            counts = {cid: (n, srcs) for cid, n, srcs in cur.execute(
                f"SELECT p.cid_id, count(DISTINCT p.full_path), count(DISTINCT p.src_id) FROM pathx p "
                f"WHERE {where_sql} AND p.cid_id IN ({_id_list(ids)}) GROUP BY p.cid_id",
                params).fetchall()} if ids else {}
            for r in rows:
                r["copies"], r["sources"] = counts.get(r.pop("cid_id"), (1, 1))
        else:
            total = _memo(_memo_key(cur, "total", where_sql, tuple(params)), lambda: cur.execute(
                f"SELECT count(*) FROM pathx p WHERE {where_sql}", params).fetchone()[0])
            # The page is picked first and only its rows get a content id: joining cid to every
            # row first needs the whole cid table in memory.
            order = _order_clause(sort, direction, nulls_last=bool(mat))
            rows = _rows(
                cur,
                f"""
                SELECT t.source, t.full_path, t.name, t.ext, t.size, t.physical, t.exclusive,
                       t.versions, t.first_seen, t.last_seen, t.in_latest, c.content_id
                       {", t.ranked_value" if mat else ""}
                FROM (
                    SELECT s.source AS source, p.full_path, p.name, p.ext, p.size, p.physical, p.exclusive,
                           p.versions, p.first_seen, p.last_seen, p.in_latest, p.cid_id
                           {value_sql}
                    FROM pathx p
                    JOIN src s ON s.src_id = p.src_id
                    {join_sql}
                    WHERE {where_sql}
                    {order}
                    LIMIT ? OFFSET ?
                ) t
                LEFT JOIN cid c ON c.cid_id = t.cid_id
                {order}
                """,
                [*params, limit, offset],
            )
        if not dedupe or by_path:
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
    if confirm_exists and not DEMO_INSTANCE and not is_demo(repo):
        for row in rows:
            row["on_disk"] = os.path.exists(row["full_path"])
    return {"rows": rows, "total": total, "offset": offset, "limit": limit, "ranked": ranked_meta}


# ---- Search "landing view" cache (default empty-query view) — serve-stale + bg revalidate ----
# Keyed by sort as well as repo: the tab opens on the user's saved sort, not always on size.
_LANDING: dict[tuple, dict] = {}
_LANDING_TTL = 300.0
_LANDING_REFRESH: set[tuple] = set()   # keys with a stale refresh in flight
_LANDING_LOCK = threading.Lock()


def _refresh_landing(repo: str, limit: int, sort: str, direction: str) -> None:
    try:
        with lanes.background():
            warm_landing(repo, limit, sort, direction)
    except Exception as exc:  # noqa: BLE001 - a background refresh must never surface
        log.warning("repo=%s search landing refresh failed: %s", repo, exc)
    finally:
        with _LANDING_LOCK:
            _LANDING_REFRESH.discard((repo, limit, sort, direction))


def warm_landing(repo: str, limit: int = 500, sort: str = "size", direction: str = "desc") -> dict:
    res = search_paths(repo, limit=limit, sort=sort, direction=direction)
    _LANDING[(repo, sort, direction)] = {"result": res, "ts": time.time(), "limit": limit}
    return res


def landing(repo: str, limit: int = 500, sort: str = "size", direction: str = "desc") -> dict:
    c = _LANDING.get((repo, sort, direction))
    if c and c.get("limit") == limit:
        if time.time() - c["ts"] < _LANDING_TTL:
            return c["result"]
        key = (repo, limit, sort, direction)
        with _LANDING_LOCK:
            start = key not in _LANDING_REFRESH
            _LANDING_REFRESH.add(key)
        if start:
            threading.Thread(target=_refresh_landing, args=key, daemon=True).start()
        return c["result"]
    return warm_landing(repo, limit, sort, direction)


# The Search table's columns, in its order and with its titles (CSV export header).
EXPORT_HEADER = ["Path", "Size", "Physical (latest)", "Src", "Stored (all versions)",
                 "Frees (all versions)", "Ext", "Versions", "Copies", "Last seen", "First seen",
                 "On disk", "Source"]


def search_export(repo: str, query: str = "", deleted_only: bool = False, min_size: int = 0,
                  source: str | None = None, ext: str | None = None,
                  sort: str = "size", direction: str = "desc", cap: int = 2_000_000,
                  max_size: int = 0, date_from: str | None = None, date_to: str | None = None,
                  min_versions: int = 0, shared_min: int = 0, ext_exclude: str | None = None,
                  first_from: str | None = None, dedupe: bool = False, dedupe_by: str = "path"):
    """Every matching row (the full filtered set, not a page) in ``EXPORT_HEADER`` order.

    Stored/Frees come from the nightly ranked lists; a row outside them is left empty. Returns
    ``(cursor, registered)``: the caller unregisters those names and closes the cursor."""
    import pyarrow as pa

    from . import rankpaths
    cur = read_cursor(repo)
    registered: list[str] = []
    try:
        tag = uuid.uuid4().hex[:8]
        lists: dict[str, str] = {}
        for kind in _RANK_SORTS:
            values = rankpaths.ranked_values(repo, kind)
            if values:
                name = f"_export_{kind}_{tag}"
                cur.register(name, pa.table({
                    "key": pa.array(list(values), pa.int64()),
                    "value": pa.array(list(values.values()), pa.uint64())}))
                registered.append(name)
                lists[kind] = name
        if sort in _RANK_SORTS and sort not in lists:
            sort = "physical"      # the screen's own fallback when no ranked list exists yet
        where, params = _search_where(cur, query, deleted_only, min_size, source, ext, max_size,
                                      date_from, date_to, min_versions, shared_min, ext_exclude, first_from)
        where_sql = _where(where)
        st, fr = lists.get("stored"), lists.get("frees")
        st_join = f"LEFT JOIN {st} rs ON rs.key = p.path_id" if st else ""
        fr_join = f"LEFT JOIN {fr} rf ON rf.key = p.path_id" if fr else ""
        fr_val = "max(rf.value)" if fr else "NULL::UBIGINT"
        st_val = "max(rs.value)" if st else "NULL::UBIGINT"
        by_path = dedupe and dedupe_by != "content"
        if by_path:
            base = f"""
                SELECT arg_max(p.cid_id, p.last_seen) AS cid_id, arg_max(s.source, p.last_seen) AS source,
                       p.full_path, arg_max(p.ext, p.last_seen) AS ext,
                       arg_max(p.size, p.last_seen) AS size, arg_max(p.physical, p.last_seen) AS physical,
                       max(p.versions) AS versions, min(p.first_seen) AS first_seen, max(p.last_seen) AS last_seen,
                       bool_or(p.in_latest) AS in_latest, {st_val} AS stored_all, {fr_val} AS frees_all
                FROM pathx p JOIN src s ON s.src_id = p.src_id {st_join} {fr_join}
                WHERE {where_sql}
                GROUP BY {_HOST_SQL}, p.full_path"""
        elif dedupe:
            base = f"""
                SELECT p.cid_id, arg_max(s.source, p.last_seen) AS source,
                       arg_max(p.full_path, p.last_seen) AS full_path, arg_max(p.ext, p.last_seen) AS ext,
                       max(p.size) AS size, max(p.physical) AS physical, max(p.versions) AS versions,
                       min(p.first_seen) AS first_seen, max(p.last_seen) AS last_seen,
                       bool_or(p.in_latest) AS in_latest, {st_val} AS stored_all, {fr_val} AS frees_all,
                       count(DISTINCT p.src_id) AS sources, count(DISTINCT p.full_path) AS copies
                FROM pathx p JOIN src s ON s.src_id = p.src_id {st_join} {fr_join}
                WHERE {where_sql} AND p.cid_id IS NOT NULL
                GROUP BY p.cid_id"""
            counts, counts_join, src_col, copies_col = "", "", "m.sources", "m.copies"
        if not dedupe:
            base = f"""
                SELECT p.cid_id, s.source, p.full_path, p.ext, p.size, p.physical, p.versions,
                       p.first_seen, p.last_seen, p.in_latest, {"rs.value" if st else "NULL::UBIGINT"} AS stored_all,
                       {"rf.value" if fr else "NULL::UBIGINT"} AS frees_all
                FROM pathx p JOIN src s ON s.src_id = p.src_id {st_join} {fr_join}
                WHERE {where_sql}"""
        if not dedupe or by_path:
            # Src / Copies exactly as the table counts them: distinct sources and distinct paths
            # holding the same content anywhere in the repo.
            counts = """, cc AS (
                SELECT p2.cid_id, count(DISTINCT p2.src_id) AS sources, count(DISTINCT p2.full_path) AS copies
                FROM pathx p2 WHERE p2.cid_id IN (SELECT cid_id FROM m WHERE cid_id IS NOT NULL)
                GROUP BY p2.cid_id)"""
            counts_join = "LEFT JOIN cc ON cc.cid_id = m.cid_id"
            src_col, copies_col = "coalesce(cc.sources, 1)", "coalesce(cc.copies, 1)"
        order_col = {"stored": "stored_all", "frees": "frees_all"}.get(sort) or _SEARCH_SORT.get(sort, "size")
        if order_col == "name":
            order_col = "full_path"     # name is not an export column
        dirn = "ASC" if str(direction).lower() == "asc" else "DESC"
        nulls = " NULLS LAST" if sort in _RANK_SORTS else ""
        cur.execute(
            f"""
            WITH m AS ({base}){counts}
            SELECT m.full_path, m.size, m.physical, {src_col} AS sources, m.stored_all,
                   m.frees_all, m.ext, m.versions, {copies_col} AS copies,
                   m.last_seen, m.first_seen,
                   CASE WHEN m.in_latest THEN 'yes' ELSE 'deleted' END AS on_disk, m.source
            FROM m {counts_join}
            ORDER BY {order_col} {dirn}{nulls}, last_seen DESC, source, full_path
            LIMIT {int(cap)}
            """,
            params,
        )
    except Exception:
        for name in registered:
            try:
                cur.unregister(name)
            except Exception:  # noqa: BLE001 - already gone
                pass
        cur.close()
        raise
    return cur, registered


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
            SELECT sn.snapshot_id, sn.start_time, f.size, f.mtime, c.content_id
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
    recl = " AND f.cid_id IN (SELECT m.cid_id FROM crefs m WHERE m.refs <= 1)" if reclaimable_only else ""
    cur = read_cursor(repo)
    try:
        sid = resolve.src_id(cur, source)
        snid = resolve.snap_id(cur, snapshot_id)
        if sid is None or snid is None:  # unknown source/snapshot: no rows before, none now
            return []
        ch = _child_sql(cur, prefix)
        cond = f" AND {ch.scope}" if ch.scope else ""
        rows = _rows(
            cur,
            f"""
            SELECT {ch.name} AS name,
                   sum(f.size) AS size,
                   sum(coalesce(ip.packed, cs.packed, 0)) AS physical,
                   count(*) AS files,
                   max(f.mtime) AS modified,
                   max(p.versions) AS versions, max(p.last_seen) AS last_seen,
                   max(CASE WHEN {ch.folder} THEN 1 ELSE 0 END) AS is_folder
            FROM filev f
            JOIN pathx p ON p.path_id = f.path_id
            LEFT JOIN iphys ip ON ip.cid_id = f.cid_id
            LEFT JOIN csize cs ON cs.cid_id = f.cid_id
            {ch.join}
            WHERE p.src_id = ? AND f.snap_id = ?{cond}{recl}
            GROUP BY 1
            ORDER BY size DESC, name
            """,
            [sid, snid],
        )
    finally:
        cur.close()
    children = []
    for r in rows:
        name = r["name"]
        children.append(
            {
                "path": pathrules.child_path(ch.base, ch.sep, name),
                "name": name,
                "size": r["size"] or 0,
                "physical": r["physical"] or 0,
                "files": r["files"],
                "modified": str(r["modified"]) if r["modified"] else "",
                "versions": None if r["is_folder"] else r["versions"],
                "last_seen": str(r["last_seen"]) if r["last_seen"] else "",
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


def folder_file_filter(cur, query: str = "", source: str | None = None, ext: str | None = None,
                       ext_exclude: str | None = None, deleted_only: bool = False) -> tuple[list[str], list]:
    """The file filters a folder rollup applies (Search's text, source, type and deleted filters),
    over ``pathx p``. Shared by the folder list and the folder pricing so both count the same files."""
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
        cond, qp = _text_match(query)
        conds.append(cond)
        params.extend(qp)
    return conds, params


DEPTH_MODES = ("deepest", "nested", "own")


@lanes.heavy_read
def group_by_folder(repo: str, depth: int = 3, query: str = "", source: str | None = None,
                    ext: str | None = None, deleted_only: bool = False, min_size: int = 0,
                    min_files: int = 0, sort: str = "physical", direction: str = "desc",
                    limit: int = 500, ext_exclude: str | None = None, min_depth: int = 0,
                    loose: bool = False, mode: str = "deepest") -> dict:
    """Bloat finder: backed-up folders, ranked. ``mode`` picks how Min/Max depth choose the rows:

    - ``deepest``: whole folders (everything under them) at ``depth``, plus shallower folders with
      no subfolders; ``loose`` adds a row for the files directly in a shallower folder that has
      subfolders. ``min_depth`` hides rows above it.
    - ``nested``: every folder from ``min_depth`` to ``depth``, each with everything under it, so a
      folder and its subfolders can both be listed.
    - ``own``: every folder from ``min_depth`` to ``depth``, each with only the files directly in it.

    Levels count a network share like a drive letter (both level 1). Counts are per file: a path
    held by nested sources is one file (). ``phys`` adds up the files' own Physical - a
    ranking estimate, since identical pieces count again; the exact newest-versions Physical comes
    from ``physical.folder_detail`` ()."""
    cur = read_cursor(repo)
    try:
        conds, params = folder_file_filter(cur, query, source, ext, ext_exclude, deleted_only)
        mode = mode if mode in DEPTH_MODES else "deepest"
        d = max(1, int(depth))
        lo = max(1, int(min_depth or 1))
        off = _level_off_sql(cur, "p.src_id")
        if mode == "deepest":
            join = (f"{_rollup_join('p', d, 'g', off)} "
                    "LEFT JOIN (SELECT DISTINCT parent_id FROM dir) _gk ON _gk.parent_id = _gd.dir_id")
            # split: the file's folder is above `depth` and also has subfolders, so its own files
            # are only part of it. Such rows are marked loose, so they never get whole-folder values.
            split = "(_ga.anc_id IS NULL AND _gk.parent_id IS NOT NULL)"
            loose_sql = split
            if not loose:
                conds.append(f"NOT {split}")
            if lo > 1:
                conds.append(f"(_gd.depth - {off}) >= {lo}")
        elif mode == "own":
            join = "JOIN dir _gd ON _gd.dir_id = p.dir_id"
            conds.append(f"(_gd.depth - {off}) BETWEEN {lo} AND {d}")
            loose_sql = "true"    # each row is the files directly in the folder
        order = {"physical": "phys", "logical": "logical", "files": "files"}.get(sort, "phys")
        dirn = "ASC" if direction == "asc" else "DESC"
        key = _memo_key(cur, "folders", mode, lo, d, tuple(conds), tuple(params), order, dirn,
                        int(min_size), int(min_files), int(limit))
        if mode == "nested":
            build = lambda: _nested_rows(cur, conds, params, lo, d, order, dirn, min_size, min_files, limit)
        else:
            build = lambda: _group_rows(cur, conds, params, join, loose_sql, order, dirn,
                                        min_size, min_files, limit)
        rows = [dict(r) for r in _memo(key, build)]
    finally:
        cur.close()
    return {"rows": rows, "depth": d, "mode": mode}


def _level_off_sql(cur, src_col: str) -> str:
    """SQL: how many ``dir`` levels a source's folders sit below the level the user counts. A drive
    letter is level 1; a network share sits three deeper ('', the separator, the host), so its
    sources are shifted by 3 to put the share at level 1 too."""
    unc = []
    for src_id, key in cur.execute("SELECT src_id, source FROM src").fetchall():
        s = pathrules.sep(str(key))
        if pathrules.source_root(str(key)).startswith(s + s):
            unc.append(int(src_id))
    return f"(CASE WHEN {src_col} IN ({_id_list(unc)}) THEN 3 ELSE 0 END)" if unc else "0"


def _nested_rows(cur, conds, params, lo, d, order, dirn, min_size, min_files, limit) -> list[dict]:
    """``nested`` mode: every folder from level ``lo`` to ``d``, each with everything under it. Files
    are made unique first (one row per computer + path) and only then spread over their folders,
    so the fan-out carries numbers, not paths."""
    off = _level_off_sql(cur, "_gd.src_id")
    rows = _rows(
        cur,
        f"""
        WITH f AS (
            SELECT {_HOST_SQL} AS host, p.full_path, min(p.dir_id) AS dir_id, max(p.size) AS size,
                   max(p.physical) AS physical, bool_or(p.in_latest) AS in_latest,
                   max(p.last_seen) AS last_seen, min(p.first_seen) AS first_seen
            FROM pathx p JOIN src s ON s.src_id = p.src_id
            WHERE {_where(conds)} GROUP BY 1, 2),
        lv AS (SELECT a.dir_id, a.anc_id FROM anc a JOIN dir _gd ON _gd.dir_id = a.anc_id
               WHERE (_gd.depth - {off}) BETWEEN {int(lo)} AND {int(d)}),
        g AS (SELECT lv.anc_id, sum(f.physical) AS phys, sum(f.size) AS logical, count(*) AS files,
                     count(*) FILTER (WHERE NOT f.in_latest) AS deleted_files,
                     max(f.last_seen) AS last_seen, min(f.first_seen) AS first_seen
              FROM f JOIN lv ON lv.dir_id = f.dir_id GROUP BY lv.anc_id),
        ff AS (SELECT {_folder_name_sql(cur, 'g')} AS folder, sum(g.phys) AS phys, sum(g.logical) AS logical,
                      sum(g.files) AS files, sum(g.deleted_files) AS deleted_files,
                      max(g.last_seen) AS last_seen, min(g.first_seen) AS first_seen
               FROM g JOIN dir _gd ON _gd.dir_id = g.anc_id GROUP BY 1),
        ds AS (SELECT DISTINCT p.dir_id, p.src_id FROM pathx p JOIN src s ON s.src_id = p.src_id
               WHERE {_where(conds)}),
        fs AS (SELECT {_folder_name_sql(cur, 'g')} AS folder, count(DISTINCT ds.src_id) AS sources,
                      min(ds.src_id) AS src_one
               FROM ds JOIN lv ON lv.dir_id = ds.dir_id JOIN dir _gd ON _gd.dir_id = lv.anc_id GROUP BY 1)
        SELECT ff.folder, false AS loose, ff.phys, ff.logical, ff.files, ff.deleted_files, ff.last_seen,
               ff.first_seen, 0 AS excl, fs.sources, fs.src_one
        FROM ff JOIN fs USING (folder)
        WHERE ff.phys >= {int(min_size)} AND ff.files >= {int(min_files)}
        ORDER BY {order} {dirn}, folder LIMIT {int(limit)}
        """,
        [*params, *params],
    )
    names = dict(cur.execute("SELECT src_id, source FROM src").fetchall())
    for r in rows:
        one = r.pop("src_one")
        r["source"] = names.get(one) if r["sources"] == 1 else None
    return rows


def _group_rows(cur, conds, params, join, loose_sql, order, dirn, min_size, min_files, limit) -> list[dict]:
    """Rows keyed by numbers, not paths: ``dm`` gives every ``dir`` row the id of the first dir with
    its display name (``cfold``) and of the first dir with the same path on the same computer
    (``cdir``), so a file is (cdir, name) and a folder is cfold. Grouping by the path strings held
    every full path in memory at once (2 GB+ on a 5M-path repo). The totals and the source counts
    are separate statements: in one statement DuckDB holds both at once."""
    folder = _folder_name_sql(cur, "g")
    base = f"""
        WITH dm AS (
            SELECT _gd.dir_id,
                   min(_gd.dir_id) OVER (PARTITION BY {_HOST_SQL}, _gd.dir_path) AS cdir,
                   min(_gd.dir_id) OVER (PARTITION BY {folder}) AS cfold
            FROM dir _gd JOIN src s ON s.src_id = _gd.src_id),
        m AS (
            SELECT _fm.cfold AS fk, {loose_sql} AS loose, _pm.cdir, p.name, p.size, p.physical,
                   p.in_latest, p.last_seen, p.first_seen, p.src_id
            FROM pathx p JOIN src s ON s.src_id = p.src_id
            {join}
            JOIN dm _pm ON _pm.dir_id = p.dir_id
            JOIN dm _fm ON _fm.dir_id = _gd.dir_id
            WHERE {_where(conds)})"""
    tag = uuid.uuid4().hex[:12]
    ff, fs = f"_folders_ff_{tag}", f"_folders_fs_{tag}"
    try:
        cur.execute(
            f"""CREATE TEMP TABLE {ff} AS {base},
            pf AS (SELECT fk, loose, max(size) AS size, max(physical) AS physical,
                          bool_or(in_latest) AS in_latest, max(last_seen) AS last_seen,
                          min(first_seen) AS first_seen
                   FROM m GROUP BY fk, loose, cdir, name)
            SELECT fk, loose, sum(physical) AS phys, sum(size) AS logical, count(*) AS files,
                   count(*) FILTER (WHERE NOT in_latest) AS deleted_files,
                   max(last_seen) AS last_seen, min(first_seen) AS first_seen
            FROM pf GROUP BY fk, loose""",
            params,
        )
        cur.execute(
            f"""CREATE TEMP TABLE {fs} AS {base}
            SELECT fk, loose, count(DISTINCT src_id) AS sources, min(src_id) AS src_one
            FROM m GROUP BY fk, loose""",
            params,
        )
        rows = _rows(
            cur,
            f"""
            SELECT {folder} AS folder, ff.loose, ff.phys, ff.logical, ff.files, ff.deleted_files,
                   ff.last_seen, ff.first_seen, 0 AS excl, fs.sources, fs.src_one
            FROM {ff} ff JOIN {fs} fs USING (fk, loose) JOIN dir _gd ON _gd.dir_id = ff.fk
            WHERE ff.phys >= {int(min_size)} AND ff.files >= {int(min_files)}
            ORDER BY {order} {dirn}, folder, ff.loose LIMIT {int(limit)}
            """,
        )
    finally:
        for table in (ff, fs):
            try:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception:  # noqa: BLE001 - a temp-table cleanup must never fail the request
                pass
    names = dict(cur.execute("SELECT src_id, source FROM src").fetchall())
    for r in rows:
        one = r.pop("src_one")
        r["source"] = names.get(one) if r["sources"] == 1 else None
    return rows


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


def _file_rows(where_sql: str) -> str:
    """SQL: one ``pathx``-shaped row per FILE under ``where_sql`` (columns dir_id, src_id, source,
    full_path, name, ext, size, physical, versions, first_seen, last_seen, in_latest), for folder
    breakdowns: a path held by nested sources counts once ()."""
    return (f"SELECT p.full_path, any_value(p.dir_id) AS dir_id, min(p.src_id) AS src_id, "
            f"min(s.source) AS source, any_value(p.name) AS name, any_value(p.ext) AS ext, "
            f"max(p.size) AS size, max(p.physical) AS physical, max(p.versions) AS versions, "
            f"min(p.first_seen) AS first_seen, max(p.last_seen) AS last_seen, "
            f"bool_or(p.in_latest) AS in_latest "
            f"FROM pathx p JOIN src s ON s.src_id = p.src_id WHERE {where_sql} "
            f"GROUP BY {_HOST_SQL}, p.full_path")


def paths_named(repo: str, sources: list[str], names: list[str], cap: int = 500,
                biggest_first: bool = False, mark: list[tuple[str, str]] | None = None) -> dict:
    """Every file in ``sources`` whose name equals one of ``names`` - what kopia's
    ``remove-files --filename`` matches. ``{"rows": [{source, sources, full_path, size, physical,
    in_latest}], "total", "logical", "physical", "marked"}``, one row per file; the totals cover every
    match, and ``marked`` counts the matches that are one of the ``mark`` files."""
    empty = {"rows": [], "total": 0, "logical": 0, "physical": 0, "marked": 0}
    names = list(dict.fromkeys(n for n in names if n))
    if not names or not sources:
        return empty
    cur = read_cursor(repo)
    try:
        sids = [i for i in (resolve.src_id(cur, s) for s in sources) if i is not None]
        if not sids:
            return empty
        # Exact, case-sensitive: kopia compares names with Go's path.Match, so Config.json does not
        # match config.json.
        where = ("p.src_id IN (%s) AND p.name IN (%s)"
                 % (",".join("?" * len(sids)), ",".join("?" * len(names))))
        params = [*sids, *names]
        base = _per_file(where)
        mk, mparams = _mark_sql(mark)
        total, logical, phys, marked = cur.execute(
            f"SELECT count(*), sum(size), sum(physical), {mk} FROM ({base})", [*mparams, *params]).fetchone()
        order = ("physical DESC NULLS LAST, size DESC, source, full_path" if biggest_first
                 else "source, full_path")
        rows = _rows(
            cur,
            f"SELECT source, sources, full_path, size, physical, in_latest FROM ({base}) "
            f"ORDER BY {order} LIMIT {int(cap)}",
            params,
        )
    finally:
        cur.close()
    return {"rows": rows, "total": int(total or 0), "logical": int(logical or 0),
            "physical": int(phys or 0), "marked": int(marked or 0)}


def _mark_sql(mark: list[tuple[str, str]] | None) -> tuple[str, list]:
    """SQL counting the rows that are one of the ``mark`` files, each a (source, full path): the
    same path on another computer is another file (0 when none given)."""
    pairs = list(dict.fromkeys((pathrules.host(s), fp) for s, fp in (mark or []) if fp))
    if not pairs:
        return "0", []
    host = "lower(regexp_extract(source, '^[^@]*@([^:]*)', 1))"
    cond = " OR ".join(f"({host} = ? AND full_path = ?)" for _ in pairs)
    return f"count(*) FILTER (WHERE {cond})", [v for pair in pairs for v in pair]


def paths_for_contents(repo: str, object_ids: list[str], sources: list[str] | None = None,
                       cap: int = 500, mark: list[tuple[str, str]] | None = None) -> dict:
    """Every file holding any of these content objects in any version - what
    ``remove-files --object-id`` removes inside the scoped sources. One row per file, with
    ``sources`` listing the scoped sources that hold it."""
    oids = list(dict.fromkeys(o for o in object_ids if o))
    if not oids:
        return {"rows": [], "total": 0, "marked": 0}
    cur = read_cursor(repo)
    try:
        ids = sorted(set(resolve.cid_ids(cur, oids).values()))
        if not ids:
            return {"rows": [], "total": 0, "marked": 0}
        conds = ["f.cid_id IN (%s)" % ",".join("?" * len(ids))]
        params: list = list(ids)
        if sources is not None:
            sids = [i for i in (resolve.src_id(cur, s) for s in sources) if i is not None]
            if not sids:
                return {"rows": [], "total": 0, "marked": 0}
            conds.append("p.src_id IN (%s)" % ",".join("?" * len(sids)))
            params.extend(sids)
        where = ("p.path_id IN (SELECT f.path_id FROM filev f JOIN pathx p ON p.path_id = f.path_id "
                 "WHERE " + " AND ".join(conds) + ")")
        base = _per_file(where)
        mk, mparams = _mark_sql(mark)
        total, marked = cur.execute(f"SELECT count(*), {mk} FROM ({base})", [*mparams, *params]).fetchone()
        rows = _rows(
            cur,
            f"SELECT source, sources, full_path, size, physical, in_latest FROM ({base}) "
            f"ORDER BY source, full_path LIMIT {int(cap)}",
            params,
        )
    finally:
        cur.close()
    return {"rows": rows, "total": int(total or 0), "marked": int(marked or 0)}


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
    if prefix:
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
        # = 70% of this folder". Counted per file: nested sources hold one file once ().
        base = _per_file(where_sql, ", any_value(p.name) AS name, any_value(p.ext) AS ext, "
                                    "list(DISTINCT c.content_id) AS content_ids",
                         joins="LEFT JOIN cid c ON c.cid_id = p.cid_id")
        trow = cur.execute(
            f"SELECT count(*), COALESCE(sum(physical), 0), COALESCE(sum(size), 0) FROM ({base})",
            params,
        ).fetchone() or (0, 0, 0)
        total_file_count, total_physical, total_logical = int(trow[0]), int(trow[1]), int(trow[2])
        files = _rows(
            cur,
            f"SELECT host, source, sources, full_path, name, ext, content_ids, size, physical FROM ({base}) "
            f"ORDER BY physical DESC NULLS LAST, size DESC, source, full_path LIMIT {int(cap)}",
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
            spairs = [(sid_of[s], f["full_path"]) for f in files for s in f["sources"] if s in sid_of]
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
                for src in f["sources"]:
                    sid = sid_of.get(src)
                    pid = pid_of.get((sid, f["full_path"])) if sid is not None else None
                    for c in ver.get(pid, []) if pid is not None else []:
                        s = cstr.get(c)
                        if s and s not in seen:
                            seen.add(s)
                            oids.append(s)
        else:
            for f in files:
                for cid in f.get("content_ids") or []:
                    if cid and cid not in seen:
                        seen.add(cid)
                        oids.append(cid)
        # Basenames (deduped, biggest-files-first) for the --filename mechanism (mirrors the batch
        # panel's by-name removal). pairs = every matched (source, full_path) for on-demand detail.
        names: list[str] = []
        seenn: set[str] = set()
        for f in files:
            nm = f.pop("name")
            f.pop("content_ids", None)
            if nm and nm not in seenn:
                seenn.add(nm)
                names.append(nm)
        pairs = [(s, f["full_path"]) for f in files for s in f["sources"]]
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
        "files": files,                 # one row per file, physical-desc (callers slice previews)
        "sources": sorted({s for f in files for s in f["sources"]}),
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
        base = _per_file(where_sql)
        trow = cur.execute(
            f"SELECT count(*), COALESCE(sum(physical), 0), COALESCE(sum(size), 0) FROM ({base})",
            params,
        ).fetchone() or (0, 0, 0)
        total_file_count, total_physical, total_logical = int(trow[0]), int(trow[1]), int(trow[2])
        sources = [r[0] for r in cur.execute(
            f"SELECT DISTINCT s.source FROM pathx p JOIN src s ON s.src_id = p.src_id "
            f"WHERE {where_sql} ORDER BY s.source",
            params,
        ).fetchall()]
        pairs = [(src, fp) for srcs, fp in cur.execute(
            f"SELECT sources, full_path FROM ({base}) "
            f"ORDER BY physical DESC NULLS LAST, size DESC, source, full_path "
            f"LIMIT {int(detail_limit)}",
            params,
        ).fetchall() for src in srcs]
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


def _src_rules(cur) -> dict[int, tuple[str, bool]]:
    """``{src_id: (separator, case_sensitive)}``.

    Read from the generation's own ``src`` table, not ``sources_index``: this runs inside SQL
    helpers, and a listing call there could spawn kopia on a cold cache."""
    out: dict[int, tuple[str, bool]] = {}
    for src_id, key in cur.execute("SELECT src_id, source FROM src").fetchall():
        out[int(src_id)] = (pathrules.sep(str(key)), pathrules.case_sensitive(str(key)))
    return out


def _src_rule_ids(cur, sep: str | None = None) -> tuple[list[int], list[int]]:
    """(case-insensitive src_ids, case-sensitive src_ids), optionally only the sources whose
    paths are written with ``sep``."""
    ci: list[int] = []
    cs: list[int] = []
    for src_id, (s, sensitive) in _src_rules(cur).items():
        if sep is None or s == sep:
            (cs if sensitive else ci).append(src_id)
    return ci, cs


def _id_list(ids: list[int]) -> str:
    return ",".join(str(i) for i in ids)


def _in_ids(column: str, ids: list[int]) -> str:
    return f"{column} IN ({_id_list(ids)})" if ids else "false"


def _folder_dirs(cur, prefix: str) -> tuple[list[int], str, str | None]:
    """``(dir ids, stored path, separator)`` of the folder a prefix names. Only sources whose
    paths use the prefix's own separator are searched, each by its own case rule, so a POSIX
    ``case/`` never matches ``Case/``. The top of the tree is ``([], "", None)``."""
    base, sep = pathrules.folder_key(prefix)
    if sep is None or (not base and sep != pathrules.POSIX_SEP):
        return [], base, sep
    ci, cs = _src_rule_ids(cur, sep)
    return _match_dirs(cur, base, ci, cs), base, sep


_UNC_LEAD = "'" + pathrules.WIN_SEP * 2 + "'"     # SQL literal: a UNC path's leading separators


class _Children(NamedTuple):
    scope: str          # rows under the folder ("" = no restriction)
    join: str           # the joins `name` and `folder` read
    name: str           # SQL: the row's child name inside the folder
    folder: str         # SQL: true when that child is a folder
    base: str
    sep: str | None


def _child_sql(cur, prefix: str, scope_alias: str = "p", row_alias: str | None = None) -> _Children:
    """How rows under ``prefix`` group into its immediate children - from the folder tree, never
    by cutting ``full_path``. A row's child is its ancestor folder one level below ``prefix``, or
    the row's own file when it sits directly inside. ``row_alias`` names the relation carrying
    ``dir_id``/``src_id``/``name`` when that differs from the scoped one (a deduping CTE).

    At the top of the tree that level is each source's root: a drive ("C:"), a UNC share
    ("\\\\host\\share", four levels down in ``dir``) or the POSIX root ("/")."""
    a = row_alias or scope_alias
    ids, base, sep = _folder_dirs(cur, prefix)
    if sep is None:
        depth = f"CASE WHEN starts_with({a}.full_path, {_UNC_LEAD}) THEN 4 ELSE 1 END"
        name = f"coalesce(CASE WHEN _cd.dir_path = '' THEN '/' ELSE _cd.dir_path END, {a}.name)"
        scope = ""
    elif not ids:
        return _Children("1 = 0", "", f"{a}.name", "false", base, sep)
    else:
        depth = str(int(cur.execute(
            f"SELECT max(depth) FROM dir WHERE dir_id IN ({_id_list(ids)})").fetchone()[0]) + 1)
        name = f"coalesce(_cd.name, {a}.name)"
        scope = (f"{scope_alias}.dir_id IN (SELECT x.dir_id FROM anc x WHERE x.anc_id IN "
                 f"({_id_list(ids)}))")
    join = (f"LEFT JOIN anc _ca ON _ca.dir_id = {a}.dir_id AND _ca.depth = {depth} "
            f"LEFT JOIN dir _cd ON _cd.dir_id = _ca.anc_id")
    return _Children(scope, join, name, "_cd.dir_id IS NOT NULL", base, sep)


def _rollup_join(alias: str, depth: int, tag: str, off: str = "") -> str:
    """Join ``_<tag>d``: each row's folder capped at ``depth`` (its ancestor that many levels
    down, or its own folder when shallower) - a folder rollup with no path cutting. ``off`` is an
    optional per-row level shift (``_level_off_sql``)."""
    want = f"{int(depth)} + {off}" if off and off != "0" else str(int(depth))
    return (f"LEFT JOIN anc _{tag}a ON _{tag}a.dir_id = {alias}.dir_id AND _{tag}a.depth = {want} "
            f"JOIN dir _{tag}d ON _{tag}d.dir_id = coalesce(_{tag}a.anc_id, {alias}.dir_id)")


def _folder_name_sql(cur, tag: str) -> str:
    """The display path of a ``_rollup_join`` folder; the POSIX root is stored as ''."""
    posix = [sid for sid, (s, _c) in _src_rules(cur).items() if s == pathrules.POSIX_SEP]
    if not posix:
        return f"_{tag}d.dir_path"
    return (f"CASE WHEN _{tag}d.dir_path = '' AND {_in_ids(f'_{tag}d.src_id', posix)} "
            f"THEN '/' ELSE _{tag}d.dir_path END")


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
    ids, _base, sep = _folder_dirs(cur, prefix)
    if sep is None:
        return "", []
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
    has that shape (checked), and the folder answer is the one a folder panel wants."""
    folder_ids, base, sep = _folder_dirs(cur, prefix)
    if sep is None:
        return "", []
    if folder_ids:
        return (f"{alias}.dir_id IN (SELECT a.dir_id FROM anc a WHERE a.anc_id IN "
                f"({_id_list(folder_ids)}))", [])
    if not include_path:
        return "1 = 0", []
    parent, _, name = base.rpartition(sep)
    if not name or (not parent and sep != pathrules.POSIX_SEP):
        return "1 = 0", []
    ci, cs = _src_rule_ids(cur, sep)
    parent_ids = _match_dirs(cur, parent, ci, cs)
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
            f"SELECT count(*), COALESCE(sum(f.physical), 0), COALESCE(sum(f.size), 0), "
            f"  min(f.first_seen), max(f.last_seen), count(*) FILTER (WHERE NOT f.in_latest), "
            f"  COALESCE(sum(f.versions), 0), "
            f"  (SELECT count(DISTINCT p.src_id) FROM pathx p WHERE {where}) "
            f"FROM ({_file_rows(where)}) f",
            params + params,
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
    cur = read_cursor(repo)
    try:
        ch, where, params = _folder_scope(cur, prefix, source, row_alias="dp")
        return _rows(
            cur,
            f"SELECT {ch.name} AS child, "
            f"  COALESCE(sum(dp.physical), 0) AS physical, count(*) AS files, "
            f"  bool_or({ch.folder}) AS is_folder "
            f"FROM ({_file_rows(where)}) dp {ch.join} GROUP BY child "
            f"ORDER BY physical DESC, child LIMIT {int(limit)}",
            params,
        )
    finally:
        cur.close()


def _folder_scope(cur, prefix: str, source: str | None,
                  row_alias: str | None = None) -> tuple[_Children, str, list]:
    """(children grouping, WHERE, params) for the rows under a folder, optionally one source.
    ``row_alias`` names a per-file relation the grouping reads (``_file_rows``)."""
    ch = _child_sql(cur, prefix, row_alias=row_alias)
    conds = [ch.scope] if ch.scope else []
    params: list = []
    if source:
        cond, sp = _src_cond_exact(cur, source)
        conds.append(cond)
        params.extend(sp)
    return ch, _where(conds), params


def folder_file_types(repo: str, prefix: str, source: str | None = None, limit: int = 8) -> list[dict]:
    """Files under a folder grouped by extension, biggest first; the rest rolled into ``other``."""
    cur = read_cursor(repo)
    try:
        _ch, where, params = _folder_scope(cur, prefix, source)
        rows = _rows(
            cur,
            f"SELECT COALESCE(f.ext, '') AS ext, count(*) AS files, "
            f"  COALESCE(sum(f.physical), 0) AS physical, COALESCE(sum(f.size), 0) AS logical "
            f"FROM ({_file_rows(where)}) f "
            f"GROUP BY 1 ORDER BY physical DESC, ext",
            params,
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
    cur = read_cursor(repo)
    try:
        ch, where, params = _folder_scope(cur, prefix, source, row_alias="dp")
        return _rows(
            cur,
            f"SELECT child, bool_or(is_dir) AS is_folder, count(*) AS files, "
            f"  COALESCE(sum(physical), 0) AS physical, COALESCE(sum(size), 0) AS logical, "
            f"  COALESCE(sum(versions), 0) AS versions, max(last_seen) AS last_seen, "
            f"  count(*) FILTER (WHERE NOT in_latest) AS deleted_files, min(source) AS source "
            f"FROM (SELECT {ch.name} AS child, {ch.folder} AS is_dir, "
            f"        dp.physical, dp.size, dp.versions, dp.last_seen, dp.in_latest, dp.source "
            f"      FROM ({_file_rows(where)}) dp {ch.join}) "
            f"GROUP BY child ORDER BY is_folder DESC, physical DESC, child LIMIT {int(limit)}",
            params,
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
        if prefix:
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
    sep = pathrules.sep(source)
    rel = prefix[len(root):] if prefix.startswith(root) else prefix
    oid = root_obj
    for seg in [s for s in rel.split(sep) if s]:
        kids = entries.children_of(repo, [oid]).get(oid, [])
        match = next((e for e in kids if e.get("type") == "d" and e.get("name") == seg), None)
        if not match:
            return []
        oid = match.get("obj")
    kids = entries.children_of(repo, [oid]).get(oid, [])
    phys = _physical_for_objects(repo, [e.get("obj") for e in kids
                                        if e.get("type") == "f" and e.get("obj")])

    base = prefix.rstrip(sep)
    children: list[dict] = []
    for e in kids:
        name = e.get("name", "")
        path = pathrules.child_path(base, sep, name) if prefix else name
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


def _tree_rows(rows: list[dict], ch: _Children, physical: bool) -> list[dict]:
    out = []
    for r in rows:
        name = r["name"]
        out.append({
            "path": pathrules.child_path(ch.base, ch.sep, name), "name": name,
            "size": r["size"] or 0, "physical": (r["physical"] or 0) if physical else None,
            "files": r["files"],
            "modified": str(r["modified"]) if r["modified"] else "",
            "versions": None if r["is_folder"] else r.get("versions"),
            "last_seen": str(r["last_seen"]) if r.get("last_seen") else "",
            "type": "folder" if r["is_folder"] else "file",
        })
    return out


def tree_children_all(repo: str, prefix: str, latest_only: bool = False, deleted_only: bool = False,
                      min_size: int = 0, ext: str | None = None, ext_exclude: str | None = None) -> list[dict]:
    """ALL-SOURCES aggregate tree: every source's paths merged under a prefix, **DEDUPED by
    full_path** (a path backed by several sources counts once). ``latest_only`` keeps only paths in
    their source's latest snapshot (= 'latest snapshot of each source' view, A#2); else all-snapshots
    aggregate (A#1). Node sizes are per-location; the true repo-wide deduped total is the dashboard's
    `union_total` (siblings can overlap on shared content). Top level = merged source roots."""
    cur = read_cursor(repo)
    try:
        where, params = _search_where(cur, "", deleted_only, min_size, None, ext, 0, None, None, 0, 0, ext_exclude)
        if latest_only:
            where.append("p.in_latest = true")
        ch = _child_sql(cur, prefix, row_alias="dp")
        if ch.scope:
            where.append(ch.scope)
        rows = _rows(
            cur,
            f"""
            WITH dp AS (
                SELECT p.full_path, any_value(p.dir_id) AS dir_id, any_value(p.src_id) AS src_id,
                       any_value(p.name) AS name, max(p.size) AS size, max(p.physical) AS physical,
                       max(p.last_seen) AS last_seen, max(p.versions) AS versions
                FROM pathx p WHERE {_where(where)} GROUP BY p.full_path
            )
            SELECT {ch.name} AS name,
                   sum(dp.size) AS size, sum(dp.physical) AS physical,
                   count(*) AS files, max(dp.last_seen) AS modified,
                   max(dp.versions) AS versions, max(dp.last_seen) AS last_seen,
                   max(CASE WHEN {ch.folder} THEN 1 ELSE 0 END) AS is_folder
            FROM dp {ch.join} GROUP BY 1 ORDER BY size DESC, name
            """,
            params,
        )
    finally:
        cur.close()
    return _tree_rows(rows, ch, physical=True)


def tree_children_snaps(repo: str, snapshot_ids: list[str], prefix: str = "") -> list[dict]:
    """Immediate children under a prefix across a SET of snapshots (e.g. each source's latest), from
    the delta `files` table, deduped by full_path. Powers the all-sources 'changed' view."""
    if not snapshot_ids:
        return []
    cur = read_cursor(repo)
    try:
        sids = [i for i in (resolve.snap_id(cur, s) for s in snapshot_ids) if i is not None]
        if not sids:  # none of the given snapshot ids exist: no rows before, none now
            return []
        ph = ",".join("?" * len(sids))
        ch = _child_sql(cur, prefix, row_alias="dp")
        cond = f" AND {ch.scope}" if ch.scope else ""
        rows = _rows(
            cur,
            f"""
            WITH dp AS (
                SELECT p.full_path, any_value(p.dir_id) AS dir_id, any_value(p.src_id) AS src_id,
                       any_value(p.name) AS name, max(f.size) AS size, max(f.mtime) AS mtime,
                       max(coalesce(ip.packed, cs.packed, 0)) AS physical,
                       max(p.versions) AS versions, max(p.last_seen) AS last_seen
                FROM filev f
                JOIN pathx p ON p.path_id = f.path_id
                LEFT JOIN iphys ip ON ip.cid_id = f.cid_id
                LEFT JOIN csize cs ON cs.cid_id = f.cid_id
                WHERE f.snap_id IN ({ph}){cond}
                GROUP BY p.full_path
            )
            SELECT {ch.name} AS name, sum(dp.size) AS size, sum(dp.physical) AS physical, count(*) AS files,
                   max(dp.mtime) AS modified, max(dp.versions) AS versions, max(dp.last_seen) AS last_seen,
                   max(CASE WHEN {ch.folder} THEN 1 ELSE 0 END) AS is_folder
            FROM dp {ch.join} GROUP BY 1 ORDER BY size DESC, name
            """,
            sids,
        )
    finally:
        cur.close()
    return _tree_rows(rows, ch, physical=True)


def tree_children_paths(repo: str, source: str, prefix: str, deleted_only: bool = False,
                        min_size: int = 0, ext: str | None = None,
                        ext_exclude: str | None = None) -> list[dict]:
    """ALL-snapshots (deduped) tree for a source: aggregate the distinct ``paths`` rows under a
    prefix - the source's total file set across every snapshot, not a point-in-time view. Optional
    filters (deleted-from-disk / >= size / ext include+exclude) keep only matching descendants, so
    folders with no match drop out (e.g. 'folders containing deleted files')."""
    cur = read_cursor(repo)
    try:
        sid = resolve.src_id(cur, source)
        if sid is None:  # unknown source: no rows before, none now
            return []
        ch = _child_sql(cur, prefix)
        conds = ["p.src_id = ?"] + ([ch.scope] if ch.scope else [])
        params: list = [sid]
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
        rows = _rows(
            cur,
            f"""
            SELECT {ch.name} AS name,
                   sum(p.size) AS size,
                   sum(p.physical) AS physical,
                   count(*) AS files,
                   max(p.last_seen) AS modified,
                   max(p.versions) AS versions, max(p.last_seen) AS last_seen,
                   max(CASE WHEN {ch.folder} THEN 1 ELSE 0 END) AS is_folder
            FROM pathx p
            {ch.join}
            WHERE {_where(conds)}
            GROUP BY 1
            ORDER BY size DESC, name
            """,
            params,
        )
    finally:
        cur.close()
    return _tree_rows(rows, ch, physical=True)
