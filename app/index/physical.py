"""Physical / dedup materialization + per-file footprint analysis.

The toolkit engine already computes a content->packed-bytes map (``sizemap``), a
content->reference-count map (``refcount``), and an indirect-object expansion (large/split
files). This module lands those into DuckDB so the bulk **Physical** column is a plain join,
and provides accurate per-file ``physical`` / ``freeable`` / dedup detail (which need the
indirect expansion + refcount that a naive ``content_id`` join would miss for big files).

A file's stored ``content_id`` is the object id: a single content for small files, or an
``I``-prefixed indirect object for large/split files (the ones that matter for reclaim).
"""

from __future__ import annotations

import collections
import threading

import pyarrow as pa

from ..core.cache import cache_db, decode_json
from ..core.kopia import snapshot_delete_commands
from ..core.sizemap import load_sizemap
from ..core.walk import expand_indirect
from . import resolve
from .queries import _ESC, like_literal, snapshots_for_content
from .store import read_cursor


def _chunks_for_obj(repo: str, dbsql, obj: str) -> set[str]:
    """Content chunks that physically back one file object (expands indirect objects)."""
    if not obj:
        return set()
    if obj.startswith("I"):
        return expand_indirect(repo, dbsql, {obj})
    return {obj}


def _arrow_insert(con, sql: str, columns: dict) -> None:
    n = len(next(iter(columns.values())))
    if not n:
        return
    tbl = pa.table(columns)
    con.register("_phys_ingest", tbl)
    con.execute(sql)
    con.unregister("_phys_ingest")


def materialize_physical(con, repo: str) -> None:
    """Populate ``content_sizes`` + ``indirect_phys`` from the engine artifacts and fill
    ``paths.physical`` via set-based joins. Cheap to re-run: the bulk insert is skipped when
    the sizemap is unchanged; only missing indirect objects are expanded."""
    dbsql = cache_db(repo)
    smap = load_sizemap(repo)

    # 1) content_sizes - reinsert only when the sizemap changed (cheap row-count check).
    # APPEND-ONLY (Lever 2): a content's packed size is immutable, so only INSERT new cids via an
    # anti-join - never DELETE. No index-delete bug, far less churn/bloat. Stale rows for GC'd
    # contents are harmless (never looked up; the physical/freeable math sums over current chunks).
    existing = con.execute("SELECT count(*) FROM content_sizes WHERE repo=?", [repo]).fetchone()[0]
    if existing != len(smap):
        cids = list(smap.keys())
        _arrow_insert(
            con,
            "INSERT INTO content_sizes SELECT t.repo, t.cid, t.packed FROM _phys_ingest t "
            "WHERE NOT EXISTS (SELECT 1 FROM content_sizes cs WHERE cs.repo=t.repo AND cs.cid=t.cid)",
            {"repo": [repo] * len(cids), "cid": cids, "packed": [smap[c] for c in cids]},
        )

    # 2) indirect_phys - packed footprint per indirect (large/split) file object.
    # APPEND-ONLY (Lever 2): oid->packed is immutable; expand + insert only NEW I-objects, never
    # DELETE. (Stale rows for objects no longer in paths are harmless.)
    objs = [r[0] for r in con.execute(
        "SELECT DISTINCT content_id FROM paths WHERE repo=? AND content_id LIKE 'I%'", [repo]
    ).fetchall()]
    have = {r[0] for r in con.execute("SELECT oid FROM indirect_phys WHERE repo=?", [repo]).fetchall()}
    new_objs = [o for o in objs if o not in have]
    if new_objs:
        expand_indirect(repo, dbsql, set(new_objs))  # ensure expanded + cached in sqlite
        cur = dbsql.cursor()
        oid_list: list[str] = []
        packed_list: list[int] = []
        for oid in new_objs:
            parts = {oid[1:]}  # the index content itself
            row = cur.execute("SELECT parts FROM indirect WHERE oid=?", (oid,)).fetchone()
            if row:
                # decode_json, never raw json: expand_indirect above may have just written this row
                # COMPRESSED, so a raw parse fails on the very rows this branch exists to read.
                parts |= set(decode_json(row[0]))
            oid_list.append(oid)
            packed_list.append(sum(smap.get(p, 0) for p in parts))
        _arrow_insert(
            con,
            "INSERT INTO indirect_phys SELECT repo, oid, packed FROM _phys_ingest",
            {"repo": [repo] * len(oid_list), "oid": oid_list, "packed": packed_list},
        )

    # 3) paths.physical via joins, TARGETED to freshly-(re)built rows only (the dominant DuckDB
    # churn fix). _refresh_paths inserts new/rebuilt paths with physical=0; unchanged sources keep
    # their already-correct value. content_sizes/indirect_phys are append-only with IMMUTABLE packed
    # sizes, and a path's content_id only changes via a source rebuild (which resets physical to 0),
    # so a once-correct row never needs recompute. We therefore fill ONLY the physical=0 rows instead
    # of blanket-zeroing + rewriting all ~8.5M rows every build. Genuinely-zero rows (empty files /
    # content missing from the sizemap) are harmlessly re-attempted and self-heal if the sizemap later
    # gains the content. (Removed the prior "UPDATE paths SET physical=0 WHERE repo=?" blanket rewrite.)
    con.execute(
        "UPDATE paths SET physical = cs.packed FROM content_sizes cs "
        "WHERE paths.repo=? AND cs.repo=? AND paths.physical = 0 AND paths.content_id = cs.cid "
        "AND paths.content_id NOT LIKE 'I%'",
        [repo, repo],
    )
    con.execute(
        "UPDATE paths SET physical = ip.packed FROM indirect_phys ip "
        "WHERE paths.repo=? AND ip.repo=? AND paths.physical = 0 AND paths.content_id = ip.oid",
        [repo, repo],
    )


def materialize_exclusive(con, repo: str) -> None:
    """Fill ``paths.exclusive``: how many packed bytes of each path's content are held ONLY by this
    source. NOT WIRED - the blanket UPDATE rewrote the whole paths table every dashboard build, and
    counting sources per content_id is WRONG for shared indirect/split content (two split objects
    sharing chunks each read as fully exclusive). Callers were removed 2026-09-06; the correct
    reference-count + chunk-expansion implementation is still to do. Kept as the baseline."""
    con.execute(
        """
        UPDATE paths SET exclusive = CASE WHEN srcs.n <= 1 THEN COALESCE(paths.physical, 0) ELSE 0 END
        FROM (SELECT content_id, count(DISTINCT source) AS n FROM paths
              WHERE repo=? AND content_id IS NOT NULL GROUP BY content_id) srcs
        WHERE paths.repo=? AND paths.content_id = srcs.content_id
        """,
        [repo, repo],
    )


def materialize_refcount(con, repo: str, count: dict) -> bool:
    """Persist the global content reference-count map into DuckDB (called from the pre-warm
    pass / dashboard build, which compute it anyway). Powers accurate ``freeable``. Skips the
    reinsert when the row count already matches (cheap to call every warm cycle).
    Returns True iff the table was actually rewritten, so the caller's write-throttle only
    advances on a real write (not on this early-return no-op)."""
    existing = con.execute("SELECT count(*) FROM content_refs WHERE repo=?", [repo]).fetchone()[0]
    if existing == len(count):
        return False
    # Drop the index around the rewrite: DuckDB's ART index has a fatal bug DELETING rows out of an
    # index ("Failed to delete all rows from index" -> DB invalidated). Deleting from an UN-indexed
    # table is safe; rebuilding the index after the bulk insert is also faster. (Per-repo DB, so the
    # repo-scoped delete clears the whole table.)
    con.execute("DROP INDEX IF EXISTS content_refs_cid")
    con.execute("DELETE FROM content_refs WHERE repo=?", [repo])
    cids = list(count.keys())
    _arrow_insert(
        con,
        "INSERT INTO content_refs SELECT repo, cid, refs FROM _phys_ingest",
        {"repo": [repo] * len(cids), "cid": cids, "refs": [int(count[c]) for c in cids]},
    )
    con.execute("CREATE INDEX IF NOT EXISTS content_refs_cid ON content_refs(repo, cid)")
    return True


def _refcount_for(repo: str, cids) -> dict[str, int] | None:
    """Targeted refcount for just these content chunks (one file's chunks), via batched IN
    queries - far faster + lighter than loading the whole 4M-row crefs table into a dict
    on every file-detail open. Returns None when crefs isn't built yet (cold start), so
    the caller can fall back to the full map."""
    cids = list(cids)
    cur = read_cursor(repo)
    out: dict[str, int] = {}
    try:
        if not cur.execute("SELECT count(*) FROM crefs").fetchone()[0]:
            return None
        id_of = resolve.cid_ids(cur, cids)
        rev = {v: k for k, v in id_of.items()}
        ids = sorted(set(id_of.values()))
        refs: dict[int, int] = {}
        for i in range(0, len(ids), 900):
            grp = ids[i:i + 900]
            if not grp:
                continue
            q = "SELECT cid_id, refs FROM crefs WHERE cid_id IN (%s)" % ",".join("?" * len(grp))
            for c, r in cur.execute(q, grp).fetchall():
                refs[c] = r
        out = {rev[c]: r for c, r in refs.items() if c in rev}
    finally:
        cur.close()
    _record_refcount_misses(repo, len(cids), len(cids) - len(out))
    return out


# MEASUREMENT, not a behaviour change. A chunk absent from content_refs reads as 0 references in
# every caller, which means "nothing else needs this, deleting frees it". That is CORRECT for new
# unique content (the common case) and an OVERSTATEMENT for new content that is also duplicated
# elsewhere. Excluding missing chunks instead would be wrong the other way, and understating what a
# delete frees is not "safer", it is just a different wrong number. So: count how often it actually
# happens before changing anything. content_refs is rewritten with the nightly pass and at most
# every `refcount_write_interval`, so misses should be confined to content backed up since then.
_REFCOUNT_MISS: dict[str, dict] = {}
_REFCOUNT_MISS_LOCK = threading.Lock()


def _record_refcount_misses(repo: str, asked: int, missing: int) -> None:
    with _REFCOUNT_MISS_LOCK:
        st = _REFCOUNT_MISS.setdefault(repo, {"lookups": 0, "asked": 0, "missing": 0,
                                              "lookups_with_misses": 0, "worst": 0})
        st["lookups"] += 1
        st["asked"] += asked
        st["missing"] += missing
        if missing:
            st["lookups_with_misses"] += 1
            st["worst"] = max(st["worst"], missing)


def refcount_miss_stats() -> dict:
    """{repo: {...}} - surfaced by /debug/memory so the question can be answered from real use."""
    with _REFCOUNT_MISS_LOCK:
        return {r: dict(v) for r, v in _REFCOUNT_MISS.items()}


def _refcount_map(repo: str) -> dict[str, int]:
    """Global content refcount: prefer the materialized crefs artifact; fall back to the engine
    pkl (rebuilds once if absent). The cid join is the OUTPUT side - the whole map is being
    named, which is inherently the full dictionary."""
    cur = read_cursor(repo)
    try:
        rows = cur.execute(
            "SELECT c.content_id, m.refs FROM crefs m JOIN cid c ON c.cid_id = m.cid_id"
        ).fetchall()
    finally:
        cur.close()
    if rows:
        return {cid: refs for cid, refs in rows}
    from ..core.metrics import cached_refcount  # lazy: avoids a heavy import on hot paths
    count, _ = cached_refcount(repo, cache_db(repo), load_sizemap(repo), mode="all")
    return count


_SEP = chr(92)
# (repo, content_id) -> reclaim-everywhere bytes. LRU-bounded (values are small ints, but the key
# set was unbounded - grew with every distinct content viewed via the ∑Reclaim button).
_RECLAIM_CACHE: collections.OrderedDict[tuple, int] = collections.OrderedDict()
_RECLAIM_CACHE_MAX = 100_000
_RECLAIM_LOCK = threading.Lock()  # guards the LRU: sync FastAPI handlers run in a threadpool, so the
# check->get->move_to_end->popitem sequence can interleave across requests (KeyError / lost entries)


def _reclaim_cache_put(ck: tuple, val: int) -> None:
    with _RECLAIM_LOCK:
        _RECLAIM_CACHE[ck] = val
        _RECLAIM_CACHE.move_to_end(ck)
        while len(_RECLAIM_CACHE) > _RECLAIM_CACHE_MAX:
            _RECLAIM_CACHE.popitem(last=False)


def _reclaim_cache_get(ck: tuple) -> int | None:
    with _RECLAIM_LOCK:
        val = _RECLAIM_CACHE.get(ck)
        if val is not None:
            _RECLAIM_CACHE.move_to_end(ck)
        return val


def reclaim_for(repo: str, content_ids: list[str], cap: int = 500) -> dict[str, int]:
    """For each content id (a file's latest object), compute the TRUE 'reclaim if purged
    everywhere': the deduped union of chunks across ALL versions of ALL paths sharing that
    content, whose global refcount is fully attributable to those paths. Cached per content id.
    This is the honest all-versions, content-aware freeable for the search 'Reclaim' column.

    The two index lookups are batched across the whole request: nothing indexes
    `files.content_id`, so the per-id loop scanned the full history once per id - up to 500 scans
    for one Reclaim click. One scan for "which paths hold these ids" plus one for "which objects
    each of those paths has held" returns byte-identical results, and the chunk-expansion memo is
    shared across the page so a repeated indirect object is expanded once, not per id."""
    dbsql = cache_db(repo)
    smap = load_sizemap(repo)
    uniq: list[str] = []
    seen: set[str] = set()
    for cid in content_ids:
        if cid and cid not in seen:
            seen.add(cid)
            uniq.append(cid)
        if len(uniq) >= cap:
            break
    out: dict[str, int] = {}
    misses: list[str] = []
    for cid in uniq:
        cached = _reclaim_cache_get((repo, cid))
        if cached is not None:
            out[cid] = cached
        else:
            misses.append(cid)
    if not misses:
        return out
    cur = read_cursor(repo)
    try:
        # cid -> distinct (source, full_path) pairs holding it. Input ids resolve batched, then
        # one chunked scan; the cid name comes back from the result set (output side).
        id_of = resolve.cid_ids(cur, misses)
        rev = {v: k for k, v in id_of.items()}
        shares: dict[str, list[tuple[str, str]]] = {c: [] for c in misses}
        ids = sorted(set(id_of.values()))
        for i in range(0, len(ids), 900):
            grp = ids[i:i + 900]
            if not grp:
                continue
            q = ("SELECT DISTINCT f.cid_id, s.source, p.full_path FROM filev f "
                 "JOIN pathx p ON p.path_id = f.path_id "
                 "JOIN src s ON s.src_id = p.src_id "
                 "WHERE f.cid_id IN (%s)" % ",".join("?" * len(grp)))
            for c, src, fp in cur.execute(q, grp).fetchall():
                name = rev.get(c)
                if name:
                    shares[name].append((src, fp))

        # (source, full_path) -> every distinct object the path has held across its versions.
        # Batched path resolution (KL47's shape - never a per-path lookup), then one chunked
        # filev scan; objects come back named via the result-set join.
        handles = {(src, fp) for hs in shares.values() for src, fp in hs}
        objs_of: dict[tuple[str, str], list[str]] = {h: [] for h in handles}
        if handles:
            sid_of = resolve.src_map(cur)
            rev_sid = {v: k for k, v in sid_of.items()}
            pid_of = resolve.path_ids(cur, [(sid_of[s], fp) for s, fp in handles if s in sid_of])
            key_of = {pid: (rev_sid[sid], fp) for (sid, fp), pid in pid_of.items()}
            rows_by_pid: dict[int, list[int]] = {}
            all_c: set[int] = set()
            pids = sorted(set(pid_of.values()))
            for i in range(0, len(pids), 900):
                grp = pids[i:i + 900]
                if not grp:
                    continue
                q = ("SELECT path_id, cid_id FROM filev WHERE path_id IN (%s)"
                     % ",".join("?" * len(grp)))
                for pid, c in cur.execute(q, grp).fetchall():
                    if c is not None and pid in key_of:
                        rows_by_pid.setdefault(pid, []).append(c)
                        all_c.add(c)
            cstr = resolve.content_ids(cur, sorted(all_c))
            for pid, cs in rows_by_pid.items():
                objs = objs_of[key_of[pid]]
                for c in cs:
                    obj = cstr.get(c)
                    if obj and obj not in objs:  # DISTINCT: one object per path, however many versions
                        objs.append(obj)

        chunk_memo: dict[str, set[str]] = {}
        for cid in misses:
            this_refs: dict[str, int] = {}
            for src, fp in shares.get(cid, []):
                for obj in objs_of.get((src, fp), []):
                    chunks = chunk_memo.get(obj)
                    if chunks is None:
                        chunks = _chunks_for_obj(repo, dbsql, obj)
                        chunk_memo[obj] = chunks
                    for c in chunks:
                        this_refs[c] = this_refs.get(c, 0) + 1
            refs = _refcount_for(repo, this_refs.keys()) or {}
            reclaim = sum(smap.get(c, 0) for c in this_refs if refs.get(c, 0) <= this_refs[c])
            _reclaim_cache_put((repo, cid), reclaim)
            out[cid] = reclaim
    finally:
        cur.close()
    return out


def folder_detail(repo: str, source: str | None, prefix: str, all_versions: bool = True) -> dict:
    """Subtree reclaim over EVERY version, not just the latest.

    This used to read the ``paths`` table, which holds one row per path carrying only its LATEST
    content object. That made the headline "frees if deleted" number wrong in the direction that
    matters: measured on this repo, the Everything app-data folder has 28 files but 6,786 stored
    versions, so the latest-only figure counted ~0.4% of what a delete actually reclaims. Since the
    delete planner runs with all-versions on by default, the panel was understating the payoff of
    exactly the folders worth cleaning.

    Now it unions the chunks of every version of every path under the prefix - the same counting
    ``reclaim_for`` already does for a single content - so the number lines up with what the delete
    will really free.

    ``source=None`` spans EVERY source covering the folder. Chunk occurrences are counted per
    (source, path, object), so a chunk held by two sources only counts as freeable when both are
    inside this folder; deleting from one source would not free it, and the number reflects that.

    Matching is case-insensitive: Windows paths are, and ``folder_summary`` already matched that
    way, so a case-sensitive comparison here could make the two halves of the panel disagree.
    """
    base = prefix.rstrip(_SEP)
    # Escaped like every other prefix match: `_` is a single-character wildcard, so an unescaped
    # `Backup_2024` also selects `BackupX2024`. This one computes the freeable/shared figures the
    # delete decision is made on, so a wrong subtree here is worse than a wrong listing.
    exact_lc, like_lc = base.lower(), like_literal(base.lower()) + _SEP + "%"
    dbsql = cache_db(repo)
    smap = load_sizemap(repo)
    cur = read_cursor(repo)
    try:
        where = "(lower(p.full_path) = ? OR lower(p.full_path) LIKE ?" + _ESC + ")"
        params: list = [exact_lc, like_lc]
        run = True
        if source:
            sid = resolve.src_id(cur, source)
            if sid is None:  # an unknown source matched no rows before; keep that
                run = False
            else:
                where += " AND p.src_id = ?"
                params.append(sid)
        objs: list = []
        agg = (0, 0)
        cstr: dict[int, str] = {}
        if run:
            # `filev` carries every version; `pathx` carries one row per path at its latest object. The
            # delete panel can be set to latest-only, and then the reclaim figure must match THAT, not the
            # all-versions number - a preview that describes a different operation is worse than none.
            if all_versions:
                sel = ("SELECT DISTINCT p.src_id, p.full_path, f.cid_id, f.size "
                       "FROM filev f JOIN pathx p ON p.path_id = f.path_id WHERE " + where)
            else:
                sel = ("SELECT DISTINCT p.src_id, p.full_path, p.cid_id, p.size "
                       "FROM pathx p WHERE " + where)
            # Aggregate in DuckDB, not Python: one row per DISTINCT object with how many (source, path)
            # pairs reference it. Pulling every version row into Python would be tens of millions of
            # rows on a big folder; this is the same information in a fraction of the memory.
            objs = cur.execute(
                f"SELECT cid_id, count(*) AS refs FROM ({sel}) GROUP BY cid_id", params
            ).fetchall()
            agg = cur.execute(
                f"SELECT count(*), COALESCE(sum(size), 0) FROM ("
                f"  SELECT DISTINCT src_id, full_path, max(size) AS size FROM ({sel})"
                f"  GROUP BY src_id, full_path"
                f")",
                params,
            ).fetchone() or (0, 0)
            # Object names come back from the result set (output side), never a whole-table join.
            cstr = resolve.content_ids(cur, sorted({c for c, _ in objs if c is not None}))
    finally:
        cur.close()

    this_refs: dict[str, int] = {}
    obj_chunks: dict[str, set] = {}
    for cid_id, n in objs:
        if not cid_id:
            continue
        cid = cstr[cid_id]
        chunks = obj_chunks.get(cid)
        if chunks is None:
            chunks = _chunks_for_obj(repo, dbsql, cid)
            obj_chunks[cid] = chunks
        for c in chunks:
            this_refs[c] = this_refs.get(c, 0) + int(n)
    refs = _refcount_for(repo, this_refs.keys()) or {}
    physical = sum(smap.get(c, 0) for c in this_refs)
    freeable = sum(smap.get(c, 0) for c in this_refs if refs.get(c, 0) <= this_refs[c])
    return {
        "logical": int(agg[1] or 0), "physical": physical, "freeable": freeable,
        "files": int(agg[0] or 0), "shared": physical - freeable,
        "objects": len(objs), "all_versions": all_versions,
    }


def batch_detail(repo: str, files: list[dict], all_versions: bool = True) -> dict:
    """Union (content-aware / deduped) footprint for a SET of selected files - the multi-file
    equivalent of file_detail. Unions every selected file's content chunks across all its
    versions, then: physical = packed bytes of the union; freeable = packed bytes referenced
    ONLY by the selection (global refcount fully attributable to it -> removing the whole
    selection frees them); logical = sum of distinct latest-content sizes (deduped).

    ``all_versions=False`` unions only each path's LATEST version, matching a latest-only delete
    plan. It must track whatever the caller is about to delete: pricing an all-versions union for a
    latest-only deletion overstates the freeable bytes by the churn multiplier, which on a
    rewrite-heavy path is two orders of magnitude."""
    pairs = [(f.get("source"), f.get("full_path")) for f in files if f.get("source") and f.get("full_path")]
    if not pairs:
        return {"found": False}
    dbsql = cache_db(repo)
    smap = load_sizemap(repo)
    obj_chunks: dict[str, set[str]] = {}
    this_refs: dict[str, int] = {}     # references the selection contributes per chunk (all versions)
    latest: dict[tuple, tuple] = {}    # (source,fp) -> (latest content_id, latest size)
    versions = 0
    cur = read_cursor(repo)
    try:
        # One batched path resolution for the whole selection, then one filev scan per 900
        # paths. SQL orders newest-version-first within a path, so grouping in fetch order gives
        # each path its versions newest-first exactly as the per-path query did.
        # Deduped: a repeated selection entry must not join twice (and the per-path loop it
        # replaces double-counted its chunk references, understating freeable).
        sid_of = resolve.src_map(cur)
        rev_sid = {v: k for k, v in sid_of.items()}
        pid_of = resolve.path_ids(cur, [(sid_of[s], fp) for s, fp in pairs if s in sid_of])
        key_of = {pid: (rev_sid[sid], fp) for (sid, fp), pid in pid_of.items()}
        by_path: dict[tuple, list[tuple]] = {p: [] for p in pairs}
        pids = sorted(set(pid_of.values()))
        for i in range(0, len(pids), 900):
            grp = pids[i:i + 900]
            if not grp:
                continue
            q = ("SELECT f.path_id, c.content_id, f.size FROM filev f "
                 "JOIN snap sn ON sn.snap_id = f.snap_id "
                 "LEFT JOIN cid c ON c.cid_id = f.cid_id "
                 "WHERE f.path_id IN (%s) "
                 "ORDER BY f.path_id, sn.start_time DESC, sn.snapshot_id DESC"
                 % ",".join("?" * len(grp)))
            for pid, obj, sz in cur.execute(q, grp).fetchall():
                by_path[key_of[pid]].append((obj, sz))
        for key, rows in by_path.items():
            if not rows:
                continue
            latest[key] = (rows[0][0], int(rows[0][1] or 0))
            for obj, _sz in (rows if all_versions else rows[:1]):
                versions += 1
                chunks = obj_chunks.get(obj)
                if chunks is None:
                    chunks = _chunks_for_obj(repo, dbsql, obj)
                    obj_chunks[obj] = chunks
                for c in chunks:
                    this_refs[c] = this_refs.get(c, 0) + 1
    finally:
        cur.close()
    if not latest:
        return {"found": False}

    union_chunks = set(this_refs)
    refs = _refcount_for(repo, union_chunks)
    if refs is None:
        refs = _refcount_map(repo)
    physical = sum(smap.get(c, 0) for c in union_chunks)
    freeable = sum(smap.get(c, 0) for c in union_chunks if refs.get(c, 0) <= this_refs.get(c, 0))

    seen_cid: set[str] = set()
    logical = 0
    for cid, sz in latest.values():
        if cid in seen_cid:
            continue
        seen_cid.add(cid)
        logical += sz
    sources = sorted({s for s, _ in latest})
    return {
        "found": True,
        "files": len(latest),
        "distinct_content": len(seen_cid),
        "sources": sources,
        "source_count": len(sources),
        "versions": versions,
        "chunk_count": len(union_chunks),
        "logical": logical,
        "physical": physical,
        "freeable": freeable,
        "shared": physical - freeable,
        "all_versions": all_versions,
    }


def file_detail(repo: str, source: str, full_path: str) -> dict:
    """Accurate per-file footprint: logical/physical/freeable + dedup chunk breakdown +
    other paths sharing the same content object."""
    cur = read_cursor(repo)
    try:
        sid = resolve.src_id(cur, source)
        pid = resolve.path_id(cur, sid, full_path) if sid is not None else None
        if pid is None:  # unknown source or path: nothing was found before, none now
            return {"found": False}
        versions = cur.execute(
            "SELECT sn.snapshot_id, sn.start_time, f.size, c.content_id FROM filev f "
            "JOIN snap sn ON sn.snap_id = f.snap_id "
            "LEFT JOIN cid c ON c.cid_id = f.cid_id "
            "WHERE f.path_id = ? ORDER BY sn.start_time DESC, sn.snapshot_id DESC",
            [pid],
        ).fetchall()
    finally:
        cur.close()
    if not versions:
        return {"found": False}

    dbsql = cache_db(repo)
    smap = load_sizemap(repo)

    obj_chunks: dict[str, set[str]] = {}
    this_refs: dict[str, int] = {}  # references this file contributes per chunk (across versions)
    for _sid, _start, _size, obj in versions:
        chunks = obj_chunks.get(obj)
        if chunks is None:
            chunks = _chunks_for_obj(repo, dbsql, obj)
            obj_chunks[obj] = chunks
        for c in chunks:
            this_refs[c] = this_refs.get(c, 0) + 1

    latest_obj = versions[0][3]
    latest_chunks = obj_chunks.get(latest_obj, set())
    union_chunks = set(this_refs)

    refs = _refcount_for(repo, union_chunks)  # targeted (fast); None => content_refs not built yet
    if refs is None:
        refs = _refcount_map(repo)            # cold-start fallback (full map / rebuild)

    physical_latest = sum(smap.get(c, 0) for c in latest_chunks)
    physical_total = sum(smap.get(c, 0) for c in union_chunks)
    freeable = sum(smap.get(c, 0) for c in union_chunks if refs.get(c, 0) <= this_refs.get(c, 0))
    shared = physical_total - freeable

    chunk_rows = []
    for c in sorted(latest_chunks, key=lambda x: smap.get(x, 0), reverse=True)[:200]:
        g = refs.get(c, 0)
        chunk_rows.append({
            "cid": c, "packed": smap.get(c, 0), "refs": g,
            "exclusive": g <= this_refs.get(c, 0),
        })

    # Other paths sharing this exact content object (the dominant dedup case = identical files).
    cur = read_cursor(repo)
    try:
        latest_cid = resolve.cid_id(cur, latest_obj) if latest_obj else None
        others: list = []
        if latest_cid is not None:
            others = cur.execute(
                "SELECT DISTINCT s.source, p.full_path FROM filev f "
                "JOIN pathx p ON p.path_id = f.path_id "
                "JOIN src s ON s.src_id = p.src_id "
                "WHERE f.cid_id=? AND NOT (p.src_id=? AND p.full_path=?) LIMIT 500",
                [latest_cid, sid, full_path],
            ).fetchall()
    finally:
        cur.close()

    # CONTENT-CENTRIC reclaim: what is TRULY freed if this exact content object is purged from
    # EVERY location (all paths/sources), and the full set of snapshots that must be deleted to do
    # it. Per-path `freeable` reads ~0 for a multi-copy file (the copies keep it alive); this is the
    # honest repo-wide number. A chunk is freed iff its global source-refcount is fully attributable
    # to the duplicate-holding sources (refs[c] <= number of distinct sources holding this object).
    dup_sources = {source} | {s for s, _ in others}
    n_dup = len(dup_sources)
    reclaim_everywhere = sum(smap.get(c, 0) for c in latest_chunks if refs.get(c, 0) <= n_dup)
    snaps_everywhere = snapshots_for_content(repo, latest_obj) if latest_obj else []
    purge_all_cmds = snapshot_delete_commands(repo, snaps_everywhere) if snaps_everywhere else []

    return {
        "found": True,
        "source": source,
        "full_path": full_path,
        "content_id": latest_obj,
        "versions": [
            {"snapshot_id": s, "start_time": str(t) if t else "", "size": int(z or 0), "content_id": o}
            for s, t, z, o in versions
        ],
        "logical": int(versions[0][2] or 0),
        "physical": physical_latest,
        "physical_all_versions": physical_total,
        "freeable": freeable,
        "shared": shared,
        "chunks": chunk_rows,
        "chunk_count": len(latest_chunks),
        "other_paths": [{"source": s, "full_path": p} for s, p in others],
        # content-centric / purge-everywhere
        "locations": n_dup,                       # distinct sources holding this exact content
        "instances": len(others) + 1,             # distinct paths holding it (this one + others)
        "reclaim_everywhere": reclaim_everywhere,  # bytes freed if purged from every location
        "snapshots_everywhere": snaps_everywhere,  # full delete-set to free it everywhere
        "purge_everywhere_count": len(snaps_everywhere),
        "purge_everywhere_cmds": "\n".join(" ".join(c) for c in purge_all_cmds),
    }
