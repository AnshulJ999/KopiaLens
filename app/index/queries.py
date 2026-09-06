"""DuckDB query layer for search, TreeSize, and time-travel views.

All reads go through a thread-local cursor off the shared pooled connection
(``read_cursor``) - no per-query file open or schema DDL.
"""

from __future__ import annotations

import os
import threading
import time

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


def _split_exts(ext: str) -> list[str]:
    return [e.lower().lstrip(".") for e in (x.strip() for x in ext.split(",")) if e]


def _search_where(repo: str, query: str, deleted_only: bool, min_size: int,
                  source: str | None, ext: str | None, max_size: int = 0,
                  date_from: str | None = None, date_to: str | None = None,
                  min_versions: int = 0, shared_min: int = 0,
                  ext_exclude: str | None = None, first_from: str | None = None) -> tuple[list[str], list]:
    """Build the WHERE clause + params for a paths search. ``repo`` is prepended by the caller, so
    the first placeholder (``repo = ?``) is filled there; every condition added here appends its
    params in clause order. Multi-value source/ext accept comma-separated lists (-> IN)."""
    where = ["repo = ?"]
    params: list = []
    if query:
        where.append("full_path ILIKE ?" + _ESC)
        params.append(f"%{like_literal(query)}%")
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
        exts = _split_exts(ext)
        if exts:
            where.append("ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend(exts)
    if ext_exclude:
        exx = _split_exts(ext_exclude)
        if exx:  # hide these extensions; keep ext-less rows (NULL/'')
            where.append("(ext IS NULL OR ext NOT IN (%s))" % ",".join("?" * len(exx)))
            params.extend(exx)
    if date_from:
        where.append("last_seen >= ?")
        params.append(date_from)
    if date_to:
        where.append("last_seen <= ?")
        params.append(date_to)
    if first_from:  # "recently backed up": path first appeared in a snapshot on/after this cutoff
        where.append("first_seen >= ?")
        params.append(first_from)
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
                 min_versions: int = 0, shared_min: int = 0, dedupe: bool = False,
                 ext_exclude: str | None = None, first_from: str | None = None) -> dict:
    """Search the pre-aggregated ``paths`` table. Returns the TRUE total count plus one page,
    with sort + filters applied in SQL over the entire matched set. ``dedupe`` collapses all
    path/source variations of the SAME content (group by content_id) into one content-aware row."""
    where, params = _search_where(repo, query, deleted_only, min_size, source, ext, max_size,
                                  date_from, date_to, min_versions, shared_min, ext_exclude, first_from)
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
                       max(exclusive) AS exclusive,
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
                SELECT source, full_path, name, ext, size, physical, exclusive, versions, first_seen, last_seen, in_latest, content_id
                FROM paths
                WHERE {where_sql}
                {_order_clause(sort, direction)}
                LIMIT ? OFFSET ?
                """,
                [repo, *params, limit, offset],
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
                q = ("SELECT content_id, count(*), count(DISTINCT source) FROM paths WHERE repo=? "
                     "AND content_id IN (%s) GROUP BY content_id" % ",".join("?" * len(grp)))
                for c, n, s in cur.execute(q, [repo, *grp]).fetchall():
                    cp[c] = n
                    cs[c] = s
            for row in rows:
                row["copies"] = cp.get(row.get("content_id"), 1)
                row["sources"] = cs.get(row.get("content_id"), 1)
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
                  min_versions: int = 0, shared_min: int = 0, ext_exclude: str | None = None,
                  first_from: str | None = None):
    """Yield every matching row (full filtered set) for CSV export - not limited to a page."""
    where, params = _search_where(repo, query, deleted_only, min_size, source, ext, max_size,
                                  date_from, date_to, min_versions, shared_min, ext_exclude, first_from)
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
    like = like_literal(base) + _SEP + "%" if base else "%"
    # Parameterized (was a raw `% repo` interpolation = SQL-injection surface). The bound param for the
    # reclaimable subquery slots in right after the `LIKE ?` placeholder, so insert it there when present.
    recl = " AND content_id IN (SELECT cid FROM content_refs WHERE repo=? AND refs<=1)" if reclaimable_only else ""
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
                WHERE repo = ? AND source = ? AND snapshot_id = ? AND full_path LIKE ?{_ESC}{recl}
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
            [base, base, repo, source, snapshot_id, like]
            + ([repo] if reclaimable_only else [])
            + [_SEP, _SEP],
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


def group_by_folder(repo: str, depth: int = 3, query: str = "", source: str | None = None,
                    ext: str | None = None, deleted_only: bool = False, min_size: int = 0,
                    min_files: int = 0, sort: str = "physical", direction: str = "desc",
                    limit: int = 500, ext_exclude: str | None = None) -> dict:
    """Bloat finder: roll the (filtered) `paths` up by the first `depth` path segments, summing
    physical + logical + file count per folder, ranked. Find backed-up folders that shouldn't be
    there. Reclaim (freeable) is computed on demand per folder (folder_detail), not here."""
    conds = ["repo=?"]
    params: list = [repo]
    if source:
        conds.append("source=?")
        params.append(source)
    if ext:
        exts = _split_exts(ext)
        if exts:
            conds.append("ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend(exts)
    if ext_exclude:
        exx = _split_exts(ext_exclude)
        if exx:
            conds.append("(ext IS NULL OR ext NOT IN (%s))" % ",".join("?" * len(exx)))
            params.extend(exx)
    if deleted_only:
        conds.append("in_latest=false")
    if query:
        conds.append("full_path ILIKE ?" + _ESC)
        params.append(f"%{like_literal(query)}%")
    where = " AND ".join(conds)
    order = {"physical": "phys", "logical": "logical", "files": "files"}.get(sort, "phys")
    dirn = "ASC" if direction == "asc" else "DESC"
    d = max(1, int(depth))
    cur = read_cursor(repo)
    try:
        # Group by the file's PARENT folder (drop the filename) capped at depth, so individual files
        # never masquerade as folders and `depth` means folder depth. + deleted-ratio & last-seen.
        rows = _rows(
            cur,
            f"""
            SELECT array_to_string(list_slice(string_split(full_path, ?), 1,
                       least({d}, len(string_split(full_path, ?)) - 1)), ?) AS folder,
                   sum(physical) AS phys, sum(coalesce(exclusive, 0)) AS excl,
                   sum(size) AS logical, count(*) AS files,
                   count(*) FILTER (WHERE NOT in_latest) AS deleted_files,
                   max(last_seen) AS last_seen
            FROM paths WHERE {where}
            GROUP BY folder
            HAVING sum(physical) >= {int(min_size)} AND count(*) >= {int(min_files)}
            ORDER BY {order} {dirn} LIMIT {int(limit)}
            """,
            [_SEP, _SEP, _SEP, *params],
        )
    finally:
        cur.close()
    return {"rows": rows, "depth": d}


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


def sources_for_content(repo: str, object_ids: list[str]) -> list[str]:
    """Every distinct source that references these content objects in ANY snapshot (all versions) -
    the complete set that a remove-files must scope so GC can actually free the content (a copy left
    in another source keeps it alive). Queries the `files` delta table (every source that holds the
    content has >=1 row), so it is COMPLETE - not latest-version-only. No content_id index on files,
    so it's a bounded columnar scan per build (~tens-to-low-hundreds of ms) - fine for a one-off
    Build action, not a hot path."""
    oids = list(dict.fromkeys(o for o in object_ids if o))
    if not oids:
        return []
    cur = read_cursor(repo)
    out: set[str] = set()
    try:
        for i in range(0, len(oids), 900):
            grp = oids[i:i + 900]
            q = "SELECT DISTINCT source FROM files WHERE repo=? AND content_id IN (%s)" % ",".join("?" * len(grp))
            for (s,) in cur.execute(q, [repo, *grp]).fetchall():
                out.add(s)
    finally:
        cur.close()
    return sorted(out)


def remove_targets(repo: str, *, query: str = "", deleted_only: bool = False, min_size: int = 0,
                   max_size: int = 0, source: str | None = None, ext: str | None = None,
                   ext_exclude: str | None = None, date_from: str | None = None,
                   date_to: str | None = None, min_versions: int = 0, shared_min: int = 0,
                   prefix: str | None = None, all_versions: bool = False, cap: int = 200000) -> dict:
    """Files matching the CURRENT view/filter (optionally scoped to a folder ``prefix``), ordered
    by physical size DESC, plus the object-ids to feed ``remove-files`` - biggest files first so a
    chunked run reclaims the most space soonest. ``all_versions`` expands each path to EVERY
    version's object; otherwise just the latest content. Used by the chunked folder/filter delete."""
    where, params = _search_where(repo, query, deleted_only, min_size, source, ext, max_size,
                                  date_from, date_to, min_versions, shared_min, ext_exclude)
    if prefix:
        base = prefix.rstrip(_SEP)
        where.append("(full_path = ? OR full_path LIKE ?" + _ESC + ")")
        params.extend([base, like_literal(base) + _SEP + "%"])
    where_sql = " AND ".join(where)
    cur = read_cursor(repo)
    try:
        # Unbounded totals for the SAME filter, so the UI can say "the 100 biggest files are 8.2 GiB
        # = 70% of this folder". Cheap (one indexed aggregate) and it is what makes a top-N delete a
        # judgement call rather than a guess - in a 10k-file folder a hundred files usually hold most
        # of the bytes.
        trow = cur.execute(
            f"SELECT count(*), COALESCE(sum(physical), 0), COALESCE(sum(size), 0) FROM paths WHERE {where_sql}",
            [repo, *params],
        ).fetchone() or (0, 0, 0)
        total_file_count, total_physical, total_logical = int(trow[0]), int(trow[1]), int(trow[2])
        files = _rows(
            cur,
            f"SELECT source, full_path, content_id, size, physical FROM paths WHERE {where_sql} "
            f"ORDER BY physical DESC NULLS LAST, size DESC LIMIT {int(cap)}",
            [repo, *params],
        )
        logical = sum(int(f["size"] or 0) for f in files)
        physical = sum(int(f["physical"] or 0) for f in files)
        oids: list[str] = []
        seen: set[str] = set()
        if all_versions:
            # Map (source, full_path) -> all version object-ids, queried per-source in chunks, then
            # emitted in the files' physical-desc order so the biggest files' objects come first.
            by_src: dict[str, list[str]] = {}
            for f in files:
                by_src.setdefault(f["source"], []).append(f["full_path"])
            ver: dict[tuple, list[str]] = {}
            for src, paths in by_src.items():
                for i in range(0, len(paths), 900):
                    grp = paths[i:i + 900]
                    q = ("SELECT full_path, content_id FROM files WHERE repo=? AND source=? "
                         "AND full_path IN (%s)" % ",".join("?" * len(grp)))
                    for fp, cid in cur.execute(q, [repo, src, *grp]).fetchall():
                        if cid:
                            ver.setdefault((src, fp), []).append(cid)
            for f in files:
                for cid in ver.get((f["source"], f["full_path"]), []):
                    if cid not in seen:
                        seen.add(cid)
                        oids.append(cid)
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


def _prefix_params(prefix: str) -> tuple[str, str]:
    """(exact_lc, like_lc) for matching a folder and everything under it via the path_lc index."""
    base = prefix.rstrip(_SEP).lower()
    return base, like_literal(base) + _SEP + "%"


def folder_sources(repo: str, prefix: str) -> list[dict]:
    """Every source that backs up this folder, with what each one holds.

    A folder living inside two sources (e.g. ``G:\\Apps\\X`` covered by both the ``G:\\Apps`` and
    ``G:\\`` sources) is snapshotted twice, so deleting it from one source leaves it in the other.
    That is the single most confusing thing about folder deletion, hence surfacing it up front."""
    exact, like = _prefix_params(prefix)
    cur = read_cursor(repo)
    try:
        return _rows(
            cur,
            "SELECT source, count(*) AS files, COALESCE(sum(physical), 0) AS physical, "
            "  COALESCE(sum(size), 0) AS logical, min(first_seen) AS first_seen, "
            "  max(last_seen) AS last_seen, "
            "  count(*) FILTER (WHERE NOT in_latest) AS deleted_files "
            "FROM paths WHERE repo = ? AND (path_lc = ? OR path_lc LIKE ?" + _ESC + ") "
            "GROUP BY source ORDER BY physical DESC",
            [repo, exact, like],
        )
    finally:
        cur.close()


def folder_summary(repo: str, prefix: str, source: str | None = None) -> dict:
    """Cheap, instantly-available folder facts (no chunk walk): counts, sizes, dates, versions.
    The expensive freeable/shared numbers come separately from ``physical.folder_detail`` so the
    panel can render immediately and fill those in afterwards."""
    exact, like = _prefix_params(prefix)
    conds = ["repo = ?", "(path_lc = ? OR path_lc LIKE ?" + _ESC + ")"]
    params: list = [repo, exact, like]
    if source:
        conds.append("source = ?")
        params.append(source)
    where = " AND ".join(conds)
    cur = read_cursor(repo)
    try:
        row = cur.execute(
            f"SELECT count(*), COALESCE(sum(physical), 0), COALESCE(sum(size), 0), "
            f"  min(first_seen), max(last_seen), count(*) FILTER (WHERE NOT in_latest), "
            f"  COALESCE(sum(versions), 0), count(DISTINCT source) "
            f"FROM paths WHERE {where}",
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
    _, like = _prefix_params(prefix)
    conds = ["repo = ?", "path_lc LIKE ?" + _ESC]
    params: list = [repo, like]
    if source:
        conds.append("source = ?")
        params.append(source)
    where = " AND ".join(conds)
    cur = read_cursor(repo)
    try:
        return _rows(
            cur,
            f"SELECT list_extract(string_split(substr(full_path, ?), ?), 1) AS child, "
            f"  COALESCE(sum(physical), 0) AS physical, count(*) AS files "
            f"FROM paths WHERE {where} GROUP BY child "
            f"ORDER BY physical DESC LIMIT {int(limit)}",
            [len(base) + 2, _SEP, *params],
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
    where, params = _search_where(repo, "", deleted_only, min_size, source, ext, 0,
                                  None, None, 0, 0, ext_exclude)
    base = prefix.rstrip(_SEP)
    if base:
        where.append("(full_path = ? OR full_path LIKE ?" + _ESC + ")")
        params.extend([base, like_literal(base) + _SEP + "%"])
    where_sql = " AND ".join(where)
    cur = read_cursor(repo)
    try:
        rows = _rows(
            cur,
            f"SELECT source, full_path AS path, full_path AS name, size, physical, "
            f"last_seen AS modified, content_id FROM paths WHERE {where_sql} "
            f"ORDER BY physical DESC NULLS LAST, size DESC LIMIT {int(limit)}",
            [repo, *params],
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


def tree_children_all(repo: str, prefix: str, latest_only: bool = False, deleted_only: bool = False,
                      min_size: int = 0, ext: str | None = None, ext_exclude: str | None = None) -> list[dict]:
    """ALL-SOURCES aggregate tree: every source's paths merged under a prefix, **DEDUPED by
    full_path** (a path backed by several sources counts once). ``latest_only`` keeps only paths in
    their source's latest snapshot (= 'latest snapshot of each source' view, A#2); else all-snapshots
    aggregate (A#1). Node sizes are per-location; the true repo-wide deduped total is the dashboard's
    `union_total` (siblings can overlap on shared content). Top level = merged drive roots."""
    where, params = _search_where(repo, "", deleted_only, min_size, None, ext, 0, None, None, 0, 0, ext_exclude)
    if latest_only:
        where.append("in_latest = true")
    base = prefix.rstrip(_SEP)
    if base:
        where.append("full_path LIKE ?" + _ESC)
        params.append(like_literal(base) + _SEP + "%")
    where_sql = " AND ".join(where)
    cur = read_cursor(repo)
    try:
        rows = _rows(
            cur,
            f"""
            WITH dp AS (
                SELECT full_path, max(size) AS size, max(physical) AS physical, max(last_seen) AS last_seen
                FROM paths WHERE {where_sql} GROUP BY full_path
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
            FROM sub WHERE remainder <> '' GROUP BY name ORDER BY size DESC
            """,
            [repo, *params, base, base,
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
    like = like_literal(base) + _SEP + "%" if base else "%"
    ph = ",".join("?" * len(snapshot_ids))
    cur = read_cursor(repo)
    try:
        rows = _rows(
            cur,
            f"""
            WITH dp AS (
                SELECT full_path, max(size) AS size, max(mtime) AS mtime
                FROM files WHERE repo=? AND snapshot_id IN ({ph}) AND full_path LIKE ?{_ESC}
                GROUP BY full_path
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
            FROM sub WHERE remainder <> '' GROUP BY name ORDER BY size DESC
            """,
            [repo, *snapshot_ids, like, base, base,
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
    like = like_literal(base) + _SEP + "%" if base else "%"
    conds = ["repo = ?", "source = ?", "full_path LIKE ?" + _ESC]
    params: list = [repo, source, like]
    if deleted_only:
        conds.append("in_latest = false")
    if min_size:
        conds.append("size >= ?")
        params.append(int(min_size))
    if ext:
        exts = _split_exts(ext)
        if exts:
            conds.append("ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend(exts)
    if ext_exclude:
        exx = _split_exts(ext_exclude)
        if exx:
            conds.append("(ext IS NULL OR ext NOT IN (%s))" % ",".join("?" * len(exx)))
            params.extend(exx)
    where_sql = " AND ".join(conds)
    cur = read_cursor(repo)
    try:
        rows = _rows(
            cur,
            f"""
            WITH sub AS (
                SELECT size, physical, last_seen,
                       CASE WHEN ? = '' THEN full_path
                            ELSE substr(full_path, length(?) + 2) END AS remainder
                FROM paths
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
            ORDER BY size DESC
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
