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
from ..core.kopia import kopia_lines
from . import tree
from .store import read_cursor

log = get_logger("index")


def source_reach(con, out: str = "_reach_src") -> int:
    """(src, cid_id) for every object each source can reach - the basis of the refcount.

    One recursive query for ALL sources at once. The app does this today by walking each source
    separately in Python and unioning sets, at 67 seconds and 3.3 GB on repo 1; the same answer
    as a set operation measured 2.7 seconds.
    """
    tree.links(con, if_missing=True)   # layer 3 built it from the same tent moments ago
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
    import pyarrow as pa

    from . import ids

    t0 = time.perf_counter()
    con.execute("CREATE OR REPLACE TABLE _csize_raw (content_id VARCHAR, packed UBIGINT)")
    cids: list[str] = []
    sizes: list[int] = []
    n = 0

    def _flush() -> None:
        """One columnar insert per batch. ``executemany`` runs a prepared statement PER ROW, and
        this listing is millions of rows on a real repository - it is the whole content index."""
        nonlocal n
        if not cids:
            return
        con.register("_csize_arrow", pa.table({"content_id": pa.array(cids, pa.string()),
                                               "packed": pa.array(sizes, pa.uint64())}))
        con.execute("INSERT INTO _csize_raw SELECT content_id, packed FROM _csize_arrow")
        con.unregister("_csize_arrow")
        n += len(cids)
        cids.clear()
        sizes.clear()

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
                cids.append(fields[0])
                sizes.append(int(right))
                break
        if len(cids) >= 250_000:
            _flush()
    _flush()
    if not n:
        # An empty listing is only legitimate when the repository holds nothing. A repo with a
        # tree that lists no content is a failed or truncated listing, and a silently empty size
        # map makes every physical figure read as zero. A repo with no sources at all (a fresh
        # install) legitimately publishes the empty map.
        if con.execute("SELECT count(*) FROM tent").fetchone()[0]:
            raise RuntimeError(f"repo {repo}: 'content list -l' produced no usable rows for a "
                               "repository that holds content")
        log.warning("repo=%s 'content list -l' is empty and so is the tree - publishing empty "
                    "maps", repo)
        con.execute(f"CREATE OR REPLACE TABLE {out} AS "
                    "SELECT CAST(NULL AS INTEGER) AS cid_id, CAST(NULL AS UBIGINT) AS packed "
                    "WHERE false")
        con.execute("DROP TABLE _csize_raw")
        return 0

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


def reach_content_ids(repo: str, roots: list[str]) -> set[str]:
    """Content ids reachable from these snapshot root objects, read from the generation.

    The read-side twin of `source_reach`: ONE recursive query over the tree, no walk cache. It
    counts what `crefs` counts - a plain object, a split object's own index content, and every
    piece behind it, plus the root objects themselves. Over-counting is deliberate (a missed
    chunk reads as "safe to delete"). This is what replaces `core.walk.walk` for the callers that
    only want the set (retention simulate, snapshot analysis).
    """
    roots = [r for r in dict.fromkeys(roots or []) if r]
    if not roots:
        return set()
    cur = read_cursor(repo)
    try:
        ph = ",".join("?" * len(roots))
        rows = cur.execute(
            f"""WITH RECURSIVE r(dir_id) AS (
                    SELECT t.dir_id FROM tdir t JOIN cid c ON c.cid_id = t.cid_id
                    WHERE c.content_id IN ({ph})
                    UNION
                    SELECT e.child FROM r JOIN tent e ON e.dir_id = r.dir_id
                    WHERE e.child IS NOT NULL),
                objs AS (
                    SELECT e.cid_id, e.ind, e.ind_cid FROM r JOIN tent e USING (dir_id)
                    WHERE e.cid_id IS NOT NULL)
                SELECT DISTINCT content_id FROM (
                    SELECT c.content_id FROM cid c
                      WHERE c.cid_id IN (SELECT cid_id FROM objs WHERE NOT ind)
                    UNION ALL SELECT c.content_id FROM cid c
                      WHERE c.cid_id IN (SELECT ind_cid FROM objs WHERE ind)
                    UNION ALL SELECT pc.content_id FROM tpart p
                      JOIN cid pc ON pc.cid_id = p.part_cid
                      WHERE p.iobj_cid IN (SELECT cid_id FROM objs WHERE ind)
                    UNION ALL SELECT content_id FROM cid WHERE content_id IN ({ph}))
                WHERE content_id IS NOT NULL""", roots + roots).fetchall()
    finally:
        cur.close()
    return {r[0] for r in rows}


# The read-side reach, as CTE text rather than a table: these run on the SHARED in-memory read
# connection from request threads, so nothing here may CREATE anything (see reach_content_ids).
# `roots` is supplied by the caller and must yield (src, dir_id).
_REACH_CTE = """
    r(src, dir_id) AS (
        SELECT src, dir_id FROM roots
        UNION
        SELECT r.src, e.child FROM r JOIN tent e ON e.dir_id = r.dir_id
        WHERE e.child IS NOT NULL),
    objs AS (
        SELECT DISTINCT r.src, e.cid_id, e.ind, e.ind_cid
        FROM r JOIN tent e USING (dir_id)
        WHERE e.etype IN ({f}, {d}) AND e.cid_id IS NOT NULL),
    reach AS (
        SELECT DISTINCT src, cid_id FROM (
            SELECT src, cid_id FROM objs WHERE NOT ind
            UNION ALL SELECT src, ind_cid FROM objs WHERE ind AND ind_cid IS NOT NULL
            UNION ALL SELECT o.src, p.part_cid FROM objs o
                      JOIN tpart p ON p.iobj_cid = o.cid_id WHERE o.ind
            UNION ALL SELECT ro.src, d.cid_id FROM roots ro JOIN tdir d ON d.dir_id = ro.dir_id)
        WHERE cid_id IS NOT NULL)
"""


def _roots_cte(recent: int | None, latest_only: bool) -> str:
    """Snapshot roots per source, from the generation's own `isnaps` - no parameters, no listing.

    Driving this from the published index rather than the live snapshot listing is not a
    shortcut: a snapshot taken since the last sync has no folder in the tree either, so the walk
    it replaces could not price it. `isnaps` is simply the honest statement of what is indexed.
    """
    window = ("SELECT * FROM ranked WHERE rn <= %d" % recent) if recent else "SELECT * FROM ranked"
    if latest_only:
        window = "SELECT * FROM ranked WHERE rn = 1"
    return """
    ranked AS (
        SELECT s.src_id AS src, t.dir_id,
               row_number() OVER (PARTITION BY s.src_id ORDER BY i.start_time DESC,
                                                               i.snapshot_id DESC) AS rn
        FROM isnaps i
        JOIN src s ON s.source = i.source
        JOIN cid c ON c.content_id = i.root_obj
        JOIN tdir t ON t.cid_id = c.cid_id),
    roots AS (%s)
    """ % window


def source_totals(repo: str, recent: int | None = None) -> tuple[dict[str, dict], int]:
    """Per-source reachable bytes for the dashboard, as TWO queries over the whole tree.

    This is the plan's headline. The shape it replaces walked every source separately in Python
    and cached each source's content-id set in a pickle - 67 s and 3.3 GB on repo 1, and the sets
    themselves were hundreds of MB of process memory that the app then held.

    Returns ``{src_id: {"total", "free", "phys_latest"}}`` and the deduped union across every
    source. ``free`` is content no OTHER source reaches, which is the delete-safety number, so it
    is computed from the same reach in the same pass - never from a separately cached refcount
    that could be a generation behind.
    """
    f, d = tree.TYPES.index("f"), tree.TYPES.index("d")
    body = _REACH_CTE.format(f=f, d=d)
    cur = read_cursor(repo)
    try:
        # src stays an INTEGER through the recursion - it rides on millions of rows, and the
        # name is only needed on the ~100 grouped rows at the end.
        rows = cur.execute(f"""
            WITH RECURSIVE {_roots_cte(recent, False)}, {body},
            refs AS (SELECT cid_id, count(*) AS n FROM reach GROUP BY cid_id),
            agg AS (
                SELECT re.src,
                       sum(coalesce(cs.packed, 0))::HUGEINT AS total,
                       sum(CASE WHEN rf.n = 1 THEN coalesce(cs.packed, 0) ELSE 0 END)::HUGEINT
                           AS free
                FROM reach re JOIN refs rf USING (cid_id) LEFT JOIN csize cs USING (cid_id)
                GROUP BY re.src)
            SELECT s.source, a.total, a.free FROM agg a JOIN src s ON s.src_id = a.src""").fetchall()
        union_total = cur.execute(f"""
            WITH RECURSIVE {_roots_cte(recent, False)}, {body}
            SELECT coalesce(sum(coalesce(cs.packed, 0)), 0)::HUGEINT
            FROM (SELECT DISTINCT cid_id FROM reach) u
            LEFT JOIN csize cs USING (cid_id)""").fetchone()[0]
        latest = cur.execute(f"""
            WITH RECURSIVE {_roots_cte(recent, True)}, {body},
            agg AS (
                SELECT re.src, sum(coalesce(cs.packed, 0))::HUGEINT AS phys
                FROM (SELECT DISTINCT src, cid_id FROM reach) re
                LEFT JOIN csize cs USING (cid_id)
                GROUP BY re.src)
            SELECT s.source, a.phys FROM agg a JOIN src s ON s.src_id = a.src""").fetchall()
    finally:
        cur.close()
    out = {k: {"total": int(t or 0), "free": int(fr or 0), "phys_latest": 0}
           for k, t, fr in rows}
    for k, p in latest:
        out.setdefault(k, {"total": 0, "free": 0})["phys_latest"] = int(p or 0)
    return out, int(union_total or 0)


def source_refcounts(repo: str) -> dict[str, int]:
    """content_id -> how many sources reach it, computed from the tree in one query.

    Only for the cold start where the generation carries no ``crefs`` map (a pre- generation,
    i.e. the first run after the upgrade). Every other caller reads the published map.
    """
    f, d = tree.TYPES.index("f"), tree.TYPES.index("d")
    cur = read_cursor(repo)
    try:
        rows = cur.execute(f"""
            WITH RECURSIVE {_roots_cte(None, False)}, {_REACH_CTE.format(f=f, d=d)}
            SELECT c.content_id, count(*)::INTEGER
            FROM reach re JOIN cid c USING (cid_id) GROUP BY c.content_id""").fetchall()
    finally:
        cur.close()
    return dict(rows)


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
