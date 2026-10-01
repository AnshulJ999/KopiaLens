"""Rebuild a source's file history from the tree, in one level pass ( plan 4a step 3).

This replaces the walk. ``builder._build_source`` descends kopia's directory objects one folder
at a time in Python and takes about five minutes on repo 1, plus six more for the first-seen
backfill; the same answer comes out of a handful of set operations over layer 1 in 5-9 seconds.
Proven identical on 19.07M rows (repo 1) and 18.79M rows (repo 2).

The shape, and why it is this shape:

    rank      snapshots of a source, newest first. Every location carries the newest rank that
              reaches it (``rmin``) and the oldest (``rmax``).
    locs      one row per (source, folder, PATH). Path-keyed, not object-keyed, because kopia
              stores identical directories once - one folder object legitimately sits at several
              paths, and each location needs its own rows. This is the same reason the old
              walker keyed its visited set on (object, path).
    rmin      the newest snapshot reaching a location, which is where its file rows are
              attributed. That reproduces the delta index exactly.
    rmax      the oldest snapshot reaching a location, which IS the file's first-seen date -
              computed, not backfilled. The old six-minute oldest-first backfill exists only
              because a delta index cannot see this; the tree can.

Output is ``files``-shaped rows in the scratch database, so everything downstream - the paths
aggregate, the transform, the per-source incremental mapping - is the code that already ships.
"""

from __future__ import annotations

import re
import time

from ..core import paths as pathrules
from ..core.applog import get_logger
from . import tree

log = get_logger("index")

MAX_DEPTH = 512          # kopia's tree is content-addressed and cannot contain a cycle; a guard

# How tied snapshots are ordered. Kopia checkpoint snapshots share a start time exactly (up to 12
# in one source on repo 2), so the tiebreak decides which of them a file version is credited to.
#   "id"      start_time DESC, then snapshot_id ASC - the rule. Within a tie group the
#             lowest snapshot id takes the newest rank. Which direction matters far less than
#             that it is fixed: it is reproducible and independent of kopia's listing order.
#   "listing" start_time, then the position in the live listing - what today's index does, kept so
#            the parity gate can separate "the tiebreak changed" from "something is wrong".
TIE_ID = "id"
TIE_LISTING = "listing"


def _seps(con, sources: list[str]) -> None:
    """(source, sep, root) per source. Separators come from the SOURCE's rules, never the host's -
    a repo can hold POSIX and Windows sources side by side."""
    import pyarrow as pa
    rows = [(s, pathrules.sep(s), pathrules.source_root(s)) for s in sources]
    bad = sorted({r[1] for r in rows if r[1] not in ("/", "\\")})
    if bad:
        raise RuntimeError(f"source separator(s) {bad!r} are not supported")
    con.register("_seps_arrow", pa.table({
        "source": pa.array([r[0] for r in rows], pa.string()),
        "sep": pa.array([r[1] for r in rows], pa.string()),
        "root": pa.array([r[2] for r in rows], pa.string())}))
    con.execute("CREATE OR REPLACE TABLE _seps AS SELECT source, sep, "
                "CASE WHEN right(root, 1) IN ('/', '\\') THEN root ELSE root || sep END AS root "
                "FROM _seps_arrow")
    con.unregister("_seps_arrow")


def snapshots(con, repo: str, srcs: dict, *, tie: str = TIE_ID) -> int:
    """``_rsnap``: every snapshot of the named sources, ranked newest-first, with its root folder.

    A snapshot whose root object is not in the tree is DROPPED with a warning rather than
    silently ignored: it means the sync could not read that root, and the source's rows would be
    quietly short otherwise.
    """
    import pyarrow as pa
    rows = []
    for key, snaps in srcs.items():
        for pos, s in enumerate(snaps):
            obj = (s.get("rootEntry") or {}).get("obj")
            if obj:
                rows.append((key, s["id"], s.get("startTime"), obj, pos))
    con.register("_snap_arrow", pa.table({
        "source": pa.array([r[0] for r in rows], pa.string()),
        "snapshot_id": pa.array([r[1] for r in rows], pa.string()),
        "start_time": pa.array([r[2] for r in rows], pa.string()),
        "root_obj": pa.array([r[3] for r in rows], pa.string()),
        "lpos": pa.array([r[4] for r in rows], pa.int64())}))
    order = "start_time DESC, snapshot_id" if tie == TIE_ID else "start_time DESC, lpos"
    con.execute(f"""CREATE OR REPLACE TABLE _rsnap AS
        SELECT a.source, a.snapshot_id, a.start_time::TIMESTAMP AS start_time, a.root_obj,
               d.dir_id,
               row_number() OVER (PARTITION BY a.source ORDER BY {order})::INTEGER AS rank
        FROM _snap_arrow a
        JOIN cid c ON c.content_id = a.root_obj
        JOIN tdir d ON d.cid_id = c.cid_id""")
    con.unregister("_snap_arrow")
    kept = con.execute("SELECT count(*) FROM _rsnap").fetchone()[0]
    if kept < len(rows):
        log.warning("repo=%s %d snapshot root(s) of %d are not in the tree - those snapshots "
                    "contribute nothing until the next sync reads them", repo, len(rows) - kept,
                    len(rows))
    return kept


def locations(con) -> tuple[int, int]:
    """``_locs``: every (source, folder, path) the ranked snapshots reach, with rmin/rmax.

    One breadth pass per depth, each an aggregate over whole sets - never a folder at a time.
    ``GROUP BY ALL`` collapses a folder reached by several snapshots into one row carrying the
    newest and oldest rank, which is what makes the delta attribution a join rather than a walk.
    """
    tree.links(con)
    con.execute("""CREATE OR REPLACE TABLE _lvl AS
        SELECT source, dir_id, '' AS path, min(rank) AS rmin, max(rank) AS rmax
        FROM _rsnap GROUP BY ALL""")
    con.execute("CREATE OR REPLACE TABLE _locs AS SELECT * FROM _lvl WHERE false")
    depth = 0
    while True:
        if not con.execute("SELECT count(*) FROM _lvl").fetchone()[0]:
            break
        if depth >= MAX_DEPTH:
            raise RuntimeError(f"tree deeper than {MAX_DEPTH} levels - refusing to loop")
        con.execute("INSERT INTO _locs SELECT * FROM _lvl")
        con.execute("""CREATE OR REPLACE TABLE _lvl AS
            SELECT l.source, g.child AS dir_id,
                   CASE WHEN l.path = '' THEN g.name ELSE l.path || sp.sep || g.name END AS path,
                   min(l.rmin) AS rmin, max(l.rmax) AS rmax
            FROM _lvl l JOIN tlink g USING (dir_id) JOIN _seps sp ON sp.source = l.source
            GROUP BY ALL""")
        depth += 1
    return depth, con.execute("SELECT count(*) FROM _locs").fetchone()[0]


def file_rows(con, repo: str) -> int:
    """Insert the source's file-version rows into the scratch ``files`` table.

    Each file entry is credited to the NEWEST snapshot that reaches its location (``rmin``),
    which is precisely what the delta index holds today.

    ``ordinal`` is written NULL. It was the walker's emission position, it was never rebuild-stable,
    and nothing anywhere reads it - no query, no parity probe, no UI. Numbering it deterministically
    cost a global sort of every file-version row (19M on repo 1) plus the largest column in the
    published artifact (36 MB), to produce a value with no consumer. The column stays so the schema
    is unchanged.
    """
    con.execute(f"""CREATE OR REPLACE TABLE _frows AS
        SELECT l.source,
               sp.root || CASE WHEN l.path = '' THEN e.name ELSE l.path || sp.sep || e.name END
                   AS full_path,
               e.name, coalesce(e.size, 0) AS size, e.cid_id, l.rmin, l.rmax,
               CASE WHEN e.mtime IS NULL THEN NULL
                    ELSE make_timestamp(e.mtime // 1000) END AS mtime,
               e.dir_id, e.pos
        FROM _locs l
        JOIN tent e USING (dir_id)
        JOIN _seps sp ON sp.source = l.source
        WHERE e.etype = {tree.TYPES.index('f')}""")
    con.execute("""INSERT INTO files
        (repo, source, full_path, name, size, content_id, snapshot_id, start_time, ordinal, mtime)
        SELECT ?, r.source, r.full_path, r.name, r.size, c.content_id,
               sn.snapshot_id, sn.start_time,
               NULL::BIGINT, r.mtime
        FROM _frows r
        JOIN _rsnap sn ON sn.source = r.source AND sn.rank = r.rmin
        LEFT JOIN cid c ON c.cid_id = r.cid_id""", [repo])
    return con.execute("SELECT count(*) FROM _frows").fetchone()[0]


def tree_first_seen(con) -> None:
    """``_tree_seen``: the real first-seen date per path, from the OLDEST snapshot reaching it.

    This is what removes the backfill, and it also fixes a live bug. Today a file that is new to
    a source and appears in two snapshots added by one nightly, inside a folder that did not
    otherwise change, is dated to the LATER of the two: the delta rows skip the unchanged folder
    and the oldest-first backfill only runs for sources new to the index. Reading the date off
    rmax cannot make that mistake - it does not depend on which rows the delta emitted.
    """
    con.execute("""CREATE OR REPLACE TABLE _tree_seen AS
        SELECT r.source, r.full_path, min(sn.start_time) AS first_seen
        FROM _frows r JOIN _rsnap sn ON sn.source = r.source AND sn.rank = r.rmax
        GROUP BY ALL""")


_BYTES_PER_PATH_ROW = 400       # memory budget per file row in the paths aggregate


def _paths_batch_rows() -> int:
    """File rows per paths-aggregate statement, from the DuckDB memory limit (1 GB -> ~2.6M)."""
    from .store import resolve_limits
    limit, _threads = resolve_limits()
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?)i?B?\s*", str(limit).upper())
    mult = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
    total = float(m.group(1)) * mult[m.group(2)] if m else 512 * 2**20
    return max(250_000, int(total // _BYTES_PER_PATH_ROW))


def refresh_paths(con, repo: str, keys: list[str]) -> None:
    """Rewrite the ``paths`` aggregate (one row per distinct path) for the rebuilt sources, a
    group of sources per statement. One source per statement rescanned the whole ``files`` table
    per source; all sources in one statement ran repo 2 out of memory at 3 GB.

    first_seen is STICKY: the older of the published date and the tree's, so a known date never
    moves forward. ``size``/``content_id`` are the LATEST version's. Dates are keyed on each
    source's identity expression: on a case-sensitive source two case-distinct files are two
    paths and must not share a date.
    """
    if not keys:
        return
    wanted = set(keys)
    rows = {s: n for s, n in con.execute(
        "SELECT source, count(*) FROM files WHERE repo = ? GROUP BY source", [repo]).fetchall()
        if s in wanted}
    budget = _paths_batch_rows()
    batch: list[str] = []
    size = 0
    for key in sorted(keys):
        n = rows.get(key, 0)
        if batch and size + n > budget:
            _refresh_paths_batch(con, repo, batch)
            batch, size = [], 0
        batch.append(key)
        size += n
    _refresh_paths_batch(con, repo, batch)


def _refresh_paths_batch(con, repo: str, keys: list[str]) -> None:
    import pyarrow as pa
    con.register("_rk_arrow", pa.table({
        "source": pa.array(keys, pa.string()),
        "cs": pa.array([pathrules.case_sensitive(k) for k in keys], pa.bool_())}))
    con.execute("CREATE OR REPLACE TEMP TABLE _rk AS SELECT source, cs FROM _rk_arrow")
    con.unregister("_rk_arrow")

    def ident(col: str) -> str:
        return f"CASE WHEN k.cs THEN {col} ELSE lower({col}) END"

    con.execute(f"""CREATE OR REPLACE TABLE _old_seen_all AS
        SELECT source, ident, min(first_seen) AS first_seen FROM (
            SELECT p.source, {ident('p.full_path')} AS ident, p.first_seen
            FROM paths p JOIN _rk k USING (source)
            WHERE p.repo = ? AND p.first_seen IS NOT NULL
            UNION ALL
            SELECT t.source, {ident('t.full_path')} AS ident, t.first_seen
            FROM _tree_seen t JOIN _rk k USING (source)
            WHERE t.first_seen IS NOT NULL)
        GROUP BY source, ident""", [repo])
    con.execute("""CREATE OR REPLACE TABLE _latest AS
        SELECT source, arg_max(snapshot_id, start_time) AS sid FROM indexed_snapshots
        WHERE repo = ? AND source IN (SELECT source FROM _rk) GROUP BY source""", [repo])
    con.execute("DELETE FROM paths WHERE repo = ? AND source IN (SELECT source FROM _rk)", [repo])
    con.execute(f"""INSERT INTO paths (repo, source, full_path, name, ext, size, versions,
                                       first_seen, last_seen, in_latest, content_id, physical, path_lc)
        SELECT t.repo, t.source, t.full_path, t.name,
               CASE WHEN contains(t.name, '.')
                    THEN lower(list_extract(string_split(t.name, '.'), -1)) ELSE '' END AS ext,
               t.size, t.versions,
               least(coalesce(o.first_seen, t.first_seen), t.first_seen) AS first_seen,
               t.last_seen, t.in_latest, t.content_id, 0, lower(t.full_path)
        FROM (
            SELECT f.repo, f.source, f.full_path, any_value(f.name) AS name,
                   arg_max(f.size, f.start_time) AS size,
                   -- a path has at most one row per snapshot (one entry per name per folder),
                   -- so count(*) is the version count; a DISTINCT aggregate cannot spill
                   count(*) AS versions, min(f.start_time) AS first_seen,
                   max(f.start_time) AS last_seen,
                   bool_or(f.snapshot_id = coalesce(l.sid, '')) AS in_latest,
                   arg_max(f.content_id, f.start_time) AS content_id
            FROM files f
            LEFT JOIN _latest l ON l.source = f.source
            WHERE f.repo = ? AND f.source IN (SELECT source FROM _rk)
            GROUP BY f.repo, f.source, f.full_path) t
        JOIN _rk k ON k.source = t.source
        LEFT JOIN _old_seen_all o ON o.source = t.source AND o.ident = {ident('t.full_path')}""",
                [repo])
    for t in ("_rk", "_old_seen_all", "_latest"):
        con.execute(f"DROP TABLE IF EXISTS {t}")


def indexed_snapshots(con, repo: str) -> None:
    """Record what was indexed, per snapshot - the drift detector the classifier reads."""
    con.execute("""INSERT INTO indexed_snapshots
        (repo, snapshot_id, source, start_time, root_obj, indexed_at, file_count)
        SELECT ?, sn.snapshot_id, sn.source, sn.start_time, sn.root_obj, now(),
               coalesce(fc.n, 0)
        FROM _rsnap sn
        LEFT JOIN (SELECT sn2.snapshot_id, count(*) AS n
                   FROM _frows r JOIN _rsnap sn2
                     ON sn2.source = r.source AND sn2.rank = r.rmin
                   GROUP BY 1) fc ON fc.snapshot_id = sn.snapshot_id""", [repo])


def build(con, repo: str, srcs: dict, *, tie: str = TIE_ID) -> dict:
    """Rebuild the given sources' history from the tree. ``srcs`` is ``sources_index``-shaped.

    The caller has already deleted these sources' scratch rows (a rebuild is whole-source, which
    is what makes retention expiry a non-case: there is no append path to get wrong).
    """
    t0 = time.perf_counter()
    _seps(con, sorted(srcs))
    n_snaps = snapshots(con, repo, srcs, tie=tie)
    t_s = time.perf_counter()
    depth, n_locs = locations(con)
    t_l = time.perf_counter()
    n_rows = file_rows(con, repo)
    tree_first_seen(con)
    indexed_snapshots(con, repo)
    stats = {"sources": len(srcs), "snapshots": n_snaps, "depth": depth, "locations": n_locs,
             "file_rows": n_rows, "s_levels": round(t_l - t_s, 1),
             "s_rows": round(time.perf_counter() - t_l, 1),
             "s_total": round(time.perf_counter() - t0, 1)}
    log.info("repo=%s layer 3 from tree: %s", repo, stats)
    return stats
