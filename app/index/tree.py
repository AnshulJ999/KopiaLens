"""Layer 1 of the unified store: the object tree ( section 3).

This replaces the zstd SQLite walk cache. Same information - what kopia says is inside every
folder object - held as three Parquet tables inside the generation instead of a separate
writable database, so it is immutable, encrypted with everything else, pinned per generation,
and above all queryable in BULK.

    tdir   one row per folder object: dense walk-order id, its object, and the row range its
           entries occupy in ``tent`` (the sidecar that makes a point read a row range)
    tent   one row per directory entry, sorted by (dir_id, pos) - kopia's own order
    tpart  one row per piece of a split object

Object ids are integers (``cid_id``) against the generation's shared ``cid`` dictionary, not
strings: measured at 311 -> 218 MB for the entries and 10.9 -> 2.0 MB for the piece lists, and
the dictionary is shared with layer 3 so it costs nothing extra.

**Never ask this store SQLite-shaped questions.** Fetching one folder at a time into Python
dicts measured 6-10x SLOWER than SQLite doing the same thing. Every consumer works on whole
sets: all levels, all snapshots, all sources at once.

``tdir.dir_id`` is layer-1-internal and MAY be reassigned when a merge rewrites the tree in walk
order. ``cid_id`` never is - layer 3 references it.
"""

from __future__ import annotations

from ..core.applog import get_logger

log = get_logger("tree")

# Entry type and mode are dictionary-encoded exactly as the verified bench codec did it; an
# unexpected value falls through to `extra` rather than being lost or silently mapped.
TYPES = ("f", "d", "s")
MODES = ("0666", "0777", "0555", "0444")

TDIR_COLS = "dir_id, cid_id, row_lo, row_hi, ind"
TENT_COLS = ("dir_id, pos, name, etype, mode, size, mtime, cid_id, ind, ind_cid, child, "
             "s_size, s_files, s_symlinks, s_dirs, s_maxtime, s_numfailed, s_incomplete, "
             "s_numignorederrors, s_errors, extra")
TPART_COLS = "iobj_cid, part_cid, ord"

# tpart is keyed on the FULL I-prefixed object id, the same value an entry carries in `cid_id`.
# `tent.ind_cid` is a different thing - the object id with the leading I stripped, which is the
# index content and counts as a chunk in its own right (it is also tpart ordinal 0). Joining
# tpart on ind_cid matches nothing at all, and silently reads every split file as having no
# chunks, i.e. zero physical size.

# kopia writes Go RFC3339Nano (UTC, trailing fraction zeros trimmed). Stored as int64
# nanoseconds and rendered back identically - a round trip the bench verified rather than
# assumed, which is why the formatting is spelled out here instead of using strftime.
TS_TO_NS = (
    "CASE WHEN {s} IS NULL THEN NULL "
    "WHEN try_strptime({s}, ['%Y-%m-%dT%H:%M:%S.%nZ', '%Y-%m-%dT%H:%M:%SZ']) IS NULL THEN NULL "
    "ELSE epoch_ns(try_strptime({s}, ['%Y-%m-%dT%H:%M:%S.%nZ', '%Y-%m-%dT%H:%M:%SZ'])) END"
)


def ts_to_ns(expr: str) -> str:
    return TS_TO_NS.format(s=expr)


def ns_to_ts(expr: str) -> str:
    """int64 ns -> the exact string kopia wrote (trailing zeros of the fraction trimmed)."""
    return (
        "CASE WHEN {e} IS NULL THEN NULL ELSE "
        "strftime(make_timestamp({e} // 1000), '%Y-%m-%dT%H:%M:%S') || "
        "CASE WHEN {e} % 1000000000 = 0 THEN '' "
        "ELSE '.' || rtrim(lpad((({e} % 1000000000))::VARCHAR, 9, '0'), '0') END || 'Z' END"
    ).format(e=expr)


def create_tables(con) -> None:
    """The scratch-side tree tables. Column order matches the published artifacts exactly."""
    con.execute("""CREATE TABLE IF NOT EXISTS tdir (
        dir_id INTEGER, cid_id INTEGER, row_lo BIGINT, row_hi BIGINT, ind BOOLEAN)""")
    con.execute("""CREATE TABLE IF NOT EXISTS tent (
        dir_id INTEGER, pos INTEGER, name VARCHAR, etype TINYINT, mode TINYINT,
        size BIGINT, mtime BIGINT, cid_id INTEGER, ind BOOLEAN, ind_cid INTEGER,
        child INTEGER, s_size BIGINT, s_files BIGINT, s_symlinks BIGINT, s_dirs BIGINT,
        s_maxtime BIGINT, s_numfailed BIGINT, s_incomplete VARCHAR,
        s_numignorederrors BIGINT, s_errors VARCHAR, extra VARCHAR)""")
    con.execute("""CREATE TABLE IF NOT EXISTS tpart (
        iobj_cid INTEGER, part_cid INTEGER, ord INTEGER)""")


def links(con, out: str = "tlink", fold: bool = False, *, if_missing: bool = False) -> None:
    """Materialize the folder -> child links table the recursive queries walk.

    Derived from ``tent`` at load rather than stored (the plan's call): it is ~184 MB in RAM on
    repo 1 against a stored copy that would have to be merged and published like everything
    else. ``fold`` adds the lowercase name used by case-insensitive sources.

    ``if_missing`` reuses a table an earlier stage of the same build already made. ``tent`` does
    not change between layer 3 and layer 2 (the tree merge is finished before either runs), so
    rebuilding it for the second caller was a second 19M-row scan for an identical table.
    """
    if if_missing and con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [out]).fetchone()[0]:
        return
    lname = ", lower(name) AS lname" if fold else ""
    con.execute(f"CREATE OR REPLACE TABLE {out} AS SELECT dir_id, child, name{lname} "
                f"FROM tent WHERE child IS NOT NULL")


def reach(con, roots_table: str, out: str, *, by: str = "") -> None:
    """Every folder reachable from a set of roots, as one recursive query over the links table.

    ``roots_table`` has ``dir_id`` plus whatever grouping columns ``by`` names (``src``,
    ``snap_id``, ...). The result carries those columns through, so one query answers for every
    source or snapshot at once instead of a Python loop per source - which is the entire point
    of this layer.
    """
    cols = (by + ", ") if by else ""
    con.execute(f"""CREATE OR REPLACE TABLE {out} AS
        WITH RECURSIVE r({cols}dir_id) AS (
            SELECT {cols}dir_id FROM {roots_table}
            UNION
            SELECT {('r.' + by.replace(', ', ', r.')) + ', ' if by else ''}l.child
            FROM r JOIN tlink l USING (dir_id))
        SELECT * FROM r""")


def row_ranges(con, dir_ids_table: str, out: str) -> None:
    """The ``tent`` row ranges for a set of folders - the point-read path.

    Reads go through DuckDB, never pyarrow: pyarrow cannot read DuckDB-encrypted Parquet at all
    (no footer key in its API), so a pyarrow fast path would work in testing and then fail the
    moment a user turns encryption on. ``tent`` is written in ``dir_id`` order, so the row-group
    statistics prune this to the groups that actually hold the range.
    """
    con.execute(f"""CREATE OR REPLACE TABLE {out} AS
        SELECT d.dir_id, d.row_lo, d.row_hi FROM tdir d
        JOIN {dir_ids_table} q ON q.dir_id = d.dir_id""")


def entries_for(con, dir_ids_table: str, cols: str = "*"):
    """Entries of the named folders, read by row range rather than by scanning ``tent``."""
    return con.execute(f"""SELECT {cols} FROM tent e
        WHERE e.dir_id IN (SELECT dir_id FROM {dir_ids_table})
        ORDER BY e.dir_id, e.pos""")
