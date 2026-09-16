"""Layer 2 of the unified store: the object maps ( section 3).

Three maps, all keyed on ``cid_id`` against the generation's shared dictionary:

    csize   packed size per content, streamed from ``kopia content list -l``
    crefs   how many SOURCES reach each content - the delete-safety number
    iphys   packed footprint of a split object (the sum of its pieces)

These replace the pickles (``sizemap_repoN.pkl``, ``refcount_repoN_all.pkl``, ``srcsets_repoN/``),
which loaded whole at 400-500 MB of RAM each and duplicated what the index already held.

``crefs`` is the one to be careful with. It decides what counts as freeable, so a content id that
falls out of the map does not read as "unknown" - it reads as ZERO references, which means "safe
to delete". Every query here is therefore written to over-count rather than miss: files AND
directories contribute, a split object contributes its own index content as well as every piece,
and the snapshot roots are included explicitly.
"""

from __future__ import annotations

import time

from ..core.applog import get_logger
from . import tree

log = get_logger("index")


def source_reach(con, out: str = "_reach_src") -> int:
    """(src, cid_id) for every object each source can reach - the basis of the refcount.

    One recursive query for ALL sources at once. The app does this today by walking each source
    separately in Python and unioning sets, at 67 seconds and 3.3 GB on repo 1; the same answer
    as a set operation measured 2.7 seconds.
    """
    tree.links(con)
    f, d = tree.TYPES.index("f"), tree.TYPES.index("d")
    con.execute(f"""CREATE OR REPLACE TABLE {out} AS
        WITH RECURSIVE r(src, dir_id) AS (
            SELECT src, dir_id FROM _reach_roots
            UNION
            SELECT r.src, g.child FROM r JOIN tlink g USING (dir_id)),
        objs AS (
            SELECT DISTINCT r.src, e.cid_id, e.ind, e.ind_cid
            FROM r JOIN tent e USING (dir_id)
            WHERE e.etype IN ({f}, {d}) AND e.cid_id IS NOT NULL)
        SELECT DISTINCT src, cid_id FROM (
            -- a plain object contributes itself
            SELECT src, cid_id FROM objs WHERE NOT ind
            -- a split object contributes its index content ...
            UNION ALL SELECT src, ind_cid FROM objs WHERE ind AND ind_cid IS NOT NULL
            -- ... and every piece behind it
            UNION ALL SELECT o.src, p.part_cid FROM objs o
                      JOIN tpart p ON p.iobj_cid = o.cid_id WHERE o.ind
            -- and the snapshot roots themselves, which no entry points at
            UNION ALL SELECT src, cid_id FROM _reach_roots)
        WHERE cid_id IS NOT NULL""")
    return con.execute(f"SELECT count(*) FROM {out}").fetchone()[0]


def refcount(con, out: str = "crefs_new") -> int:
    """``crefs``: how many sources reach each content id."""
    con.execute(f"""CREATE OR REPLACE TABLE {out} AS
        SELECT cid_id, count(*)::INTEGER AS refs FROM _reach_src GROUP BY cid_id""")
    return con.execute(f"SELECT count(*) FROM {out}").fetchone()[0]


def indirect_phys(con, out: str = "iphys_new") -> int:
    """``iphys``: a split object's packed footprint - the sum of the pieces it owns.

    A piece the size map does not know about contributes 0 rather than dropping the whole object
    from the map, because a missing iphys row reads as zero physical size for a file that may be
    gigabytes.
    """
    con.execute(f"""CREATE OR REPLACE TABLE {out} AS
        SELECT p.iobj_cid AS cid_id, sum(coalesce(s.packed, 0))::UBIGINT AS packed
        FROM tpart p LEFT JOIN csize_new s ON s.cid_id = p.part_cid
        GROUP BY p.iobj_cid""")
    missing = con.execute(f"""SELECT count(*) FROM tpart p
        LEFT JOIN csize_new s ON s.cid_id = p.part_cid WHERE s.cid_id IS NULL""").fetchone()[0]
    if missing:
        log.warning("%d split-object piece(s) have no size in the chunk map - those objects read "
                    "smaller than they are until the next run picks the sizes up", missing)
    return con.execute(f"SELECT count(*) FROM {out}").fetchone()[0]


def chunk_sizes(con, repo: str, alloc, out: str = "csize_new") -> int:
    """``csize``: packed size per content, streamed from ``kopia content list -l``.

    Streamed rather than buffered: the whole output plus a splitlines copy plus a dict was the
    old shape and it held hundreds of MB. Content ids new to the generation are allocated here,
    because a chunk the dictionary does not carry cannot be joined to anything afterwards.
    """
    from ..core.kopia import kopia_lines
    from . import ids

    t0 = time.perf_counter()
    con.execute("CREATE OR REPLACE TABLE _csize_raw (content_id VARCHAR, packed UBIGINT)")
    rows, n = [], 0
    for line in kopia_lines(repo, ["content", "list", "-l"]):
        fields = line.split()
        if len(fields) < 2:
            continue
        # The packed size is the RIGHT half of the one `original+packed` token on the line, not
        # a fixed column. Same rule core.sizemap has always used; reading field 1 instead gives
        # a map of plausible-looking wrong numbers.
        for token in fields:
            left, sep, right = token.partition("+")
            if sep and left.isdigit() and right.isdigit():
                rows.append((fields[0], int(right)))
                break
        if len(rows) >= 100_000:
            con.executemany("INSERT INTO _csize_raw VALUES (?, ?)", rows)
            n += len(rows)
            rows = []
    if rows:
        con.executemany("INSERT INTO _csize_raw VALUES (?, ?)", rows)
        n += len(rows)
    if not n:
        # An empty listing is never a legitimate answer for a repo that holds data, and a size
        # map that is silently empty makes every physical figure read as zero.
        raise RuntimeError(f"repo {repo}: 'content list -l' produced no usable rows")

    ids.anti_join(con, "_csize_newid", "SELECT DISTINCT content_id FROM _csize_raw",
                  "SELECT content_id FROM cid", ["content_id"])
    alloc.assign(con, "_csize_alloc", "SELECT content_id FROM _csize_newid", "content_id",
                 "cid_id", id_col="cid_id")
    con.execute("INSERT INTO cid SELECT cid_id, content_id FROM _csize_alloc")
    con.execute("INSERT INTO cid_rev SELECT content_id, cid_id FROM _csize_alloc")
    con.execute(f"""CREATE OR REPLACE TABLE {out} AS
        SELECT c.cid_id, r.packed FROM _csize_raw r JOIN cid_rev c USING (content_id)""")
    con.execute("DROP TABLE _csize_raw")
    kept = con.execute(f"SELECT count(*) FROM {out}").fetchone()[0]
    log.info("repo=%s chunk sizes: %d listed, %d mapped (%.1fs)", repo, n, kept,
             time.perf_counter() - t0)
    return kept


def roots_table(con, out: str = "_reach_roots") -> None:
    """(src, cid_id, dir_id) per source root, from ``_rsnap`` - the input to the reach query."""
    con.execute(f"""CREATE OR REPLACE TABLE {out} AS
        SELECT DISTINCT dense_rank() OVER (ORDER BY sn.source) AS src, d.cid_id, sn.dir_id
        FROM _rsnap sn JOIN tdir d ON d.dir_id = sn.dir_id""")


def build(con, repo: str, alloc, *, with_sizes: bool = True) -> dict:
    """The three maps, in dependency order. ``_rsnap`` must already exist (layer3.snapshots)."""
    t0 = time.perf_counter()
    stats: dict = {}
    if with_sizes:
        stats["csize"] = chunk_sizes(con, repo, alloc)
    roots_table(con)
    stats["reach_pairs"] = source_reach(con)
    stats["crefs"] = refcount(con)
    stats["iphys"] = indirect_phys(con)
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    log.info("repo=%s layer 2 maps: %s", repo, stats)
    return stats
