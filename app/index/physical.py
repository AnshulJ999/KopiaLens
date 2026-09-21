"""Per-file footprint analysis: physical, freeable, dedup, and what a delete would reclaim.

Everything comes from the generation the app is already reading ( step 4): the content
sizes and reference counts are the published ``csize`` / ``crefs`` maps, and a split file's chunks
come from the tree's ``tpart`` piece lists. Nothing here walks kopia or touches the walk cache.

A file's stored ``content_id`` is the object id: a single content for small files, or an
``I``-prefixed split object for large ones (the ones that matter for reclaim).
"""

from __future__ import annotations

import collections
import json
import threading
import weakref

from ..core.applog import get_logger
from ..core.sizemap import load_sizemap
from . import resolve
from .queries import _ESC, like_literal, scope_sql
from .store import read_cursor

log = get_logger("physical")


def _chunks_from_kopia(repo: str, obj: str) -> set[str] | None:
    """A split object's piece list straight from kopia - the click-time fallback.

    A file backed up since the last tree sync is not in the tree, and a detail panel still has to
    answer. Same shape the sync writes: the index object is chunk ordinal 0, so it belongs in the
    set. A failed read stays None - unknown is not zero bytes.
    """
    from ..core.kopia import kopia
    res = kopia(repo, ["content", "show", obj[1:]])
    if res.returncode != 0:
        log.warning("chunks: kopia content show failed for %s (rc=%d)", obj, res.returncode)
        return None
    try:
        entries = json.loads(res.stdout).get("entries") or []
    except ValueError:
        log.warning("chunks: unparseable content show output for %s", obj)
        return None
    return {e["o"] for e in entries if e.get("o")} | {obj[1:]}


def _chunks_for_obj(repo: str, obj: str, *, fetch: bool = False) -> set[str] | None:
    """Content chunks that physically back one file object, from the generation's tree.

    A split object (``I`` prefixed) returns its pieces from ``tpart``, which includes its own index
    content as ordinal 0 - so nothing is missed. A plain object is its own single chunk. ``None``
    means neither the tree nor kopia could say; the caller treats that as unknown rather than as
    zero bytes, which is what kept a missing chunk list from reading as "this file takes no space".

    ``fetch`` asks kopia for a split object the tree has not seen yet - the behaviour the old
    ``expand_indirect(fetch=True)`` had, and the same trade ``entries.children_of`` makes for
    folder listings. Click paths pass it; bulk analysis does not, because one kopia call per file
    across a whole source is exactly the shape this store exists to remove.
    """
    if not obj:
        return set()
    if not obj.startswith("I"):
        return {obj}
    cur = read_cursor(repo)
    try:
        cid = resolve.cid_id(cur, obj)
        parts = (cur.execute("SELECT part_cid FROM tpart WHERE iobj_cid = ?", [cid]).fetchall()
                 if cid is not None else [])
        if parts:
            return set(resolve.content_ids(cur, [p[0] for p in parts]).values())
    finally:
        cur.close()
    return _chunks_from_kopia(repo, obj) if fetch else None


def _chunks_for_objs(repo: str, objs, *, cursor=None) -> dict[str, set[str] | None]:
    """Chunk sets for MANY objects at once - the shape every bulk caller wants.

    One `_chunks_for_obj` measures 4.2 ms; two hundred of them batched measure 0.04 s in total. A
    folder holding ten thousand split objects therefore spent about forty seconds where a tenth
    of one would do, which is the per-item shape `tree.py`'s own docstring forbids.

    A plain object is its own chunk and never touches the store. An object the tree holds no piece
    list for maps to None - unknown, never an empty set.
    """
    wanted = [o for o in dict.fromkeys(objs) if o]
    out: dict[str, set[str] | None] = {o: {o} for o in wanted if not o.startswith("I")}
    split = [o for o in wanted if o.startswith("I")]
    if not split:
        return out
    own = cursor is None
    cur = read_cursor(repo) if own else cursor
    try:
        id_of = resolve.cid_ids(cur, split)
        ids = sorted(set(id_of.values()))
        parts: dict[int, list[int]] = {}
        for i in range(0, len(ids), 900):
            grp = ids[i:i + 900]
            q = ("SELECT iobj_cid, part_cid FROM tpart WHERE iobj_cid IN (%s)"
                 % ",".join("?" * len(grp)))
            for iobj, part in cur.execute(q, grp).fetchall():
                parts.setdefault(iobj, []).append(part)
        names = resolve.content_ids(cur, sorted({p for lst in parts.values() for p in lst}))
    finally:
        if own:
            cur.close()
    for obj in split:
        cid = id_of.get(obj)
        lst = parts.get(cid) if cid is not None else None
        out[obj] = {names[p] for p in lst if p in names} if lst else None
    return out


def _chunks_memo(repo: str, obj: str, memo: dict, *, fetch: bool = False) -> set[str] | None:
    """Chunk set for one object, memoized per call site.

    A None answer is never memoized: `memo.get(obj)` cannot tell "not looked up yet" from "looked
    up and unknown", so storing it both defeats the memo and leaves a None where callers used to
    assume a set.
    """
    if obj in memo:
        return memo[obj]
    chunks = _chunks_for_obj(repo, obj, fetch=fetch)
    if chunks is not None:
        memo[obj] = chunks
    return chunks


def fill_paths_physical(con, repo: str) -> None:
    """Fill ``paths.physical`` from the generation's own maps ( layer 2).

    ``csize``/``iphys`` are keyed on ``cid_id``; ``paths`` still carries the string content id,
    so the join goes through ``cid_rev``. Same target as ``materialize_physical``: only rows at
    ``physical = 0`` (a rebuilt source resets them), because a content id only changes when its
    source is rebuilt and the packed size of a content is immutable. Rows whose content is absent
    stay 0 and are re-attempted next build - an empty file has no content size either.
    """
    con.execute(
        "UPDATE paths SET physical = cs.packed FROM csize_new cs "
        "JOIN cid_rev cr ON cr.cid_id = cs.cid_id "
        "WHERE paths.repo=? AND paths.physical = 0 AND paths.content_id = cr.content_id "
        "AND paths.content_id NOT LIKE 'I%'",
        [repo],
    )
    con.execute(
        "UPDATE paths SET physical = ip.packed FROM iphys_new ip "
        "JOIN cid_rev cr ON cr.cid_id = ip.cid_id "
        "WHERE paths.repo=? AND paths.physical = 0 AND paths.content_id = cr.content_id",
        [repo],
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


def _values_by_content_id(cur, cids: list[str], table: str, col: str) -> dict[str, int]:
    """``{content_id: value}`` from a per-chunk map, as ONE join ().

    What this replaces resolved the ids through `cid` in batches of 900 and then queried the map
    the same way - two scans of a multi-million row view per batch. Fine for a file panel holding
    a few hundred chunks, and quadratic-feeling for a folder holding six figures of them: pricing
    one 151k-object folder measured 21 s, 83% of it here. Binding the whole set once and letting
    DuckDB hash-join it is the same answer in one pass.
    """
    import pyarrow as pa

    if not cids:
        return {}
    cur.register("_want_cids", pa.table({"content_id": pa.array(cids, pa.string())}))
    try:
        rows = cur.execute(
            f"SELECT c.content_id, m.{col} FROM _want_cids w "
            f"JOIN cid c ON c.content_id = w.content_id "
            f"JOIN {table} m ON m.cid_id = c.cid_id").fetchall()
    finally:
        cur.unregister("_want_cids")
    return dict(rows)


def _refcount_for(repo: str, cids) -> dict[str, int] | None:
    """Targeted refcount for just these content chunks - far lighter than loading the whole 4M-row
    crefs table into a dict on every file-detail open. Returns None when crefs isn't built yet
    (cold start), so the caller can fall back to the full map."""
    cids = list(cids)
    cur = read_cursor(repo)
    out: dict[str, int] = {}
    try:
        if not cur.execute("SELECT count(*) FROM crefs").fetchone()[0]:
            return None
        out = _values_by_content_id(cur, [c for c in dict.fromkeys(cids) if c], "crefs", "refs")
    finally:
        cur.close()
    _record_refcount_misses(repo, len(cids), len(cids) - len(out))
    return out


def _sizes_for(repo: str, cids) -> dict[str, int]:
    """Targeted packed sizes for just these chunks, from the generation's `csize` map.

    One join, exactly like `_refcount_for`, instead of building the whole multi-million row map:
    a page of file panels would otherwise pay seconds and hundreds of MB per open (the KL47
    lesson - never full-map on a per-item path). Missing chunks are simply absent, and every
    caller reads them with `.get(c, 0)` - the same default the sizemap had.
    """
    wanted = [c for c in dict.fromkeys(cids) if c]
    if not wanted:
        return {}
    cur = read_cursor(repo)
    try:
        return _values_by_content_id(cur, wanted, "csize", "packed")
    finally:
        cur.close()


def _size_map(repo: str, refresh: bool = False) -> dict[str, int]:
    """content_id -> packed bytes for the WHOLE repo, from the generation's `csize` map.

    For the few callers that price a large set difference (retention simulate: millions of ids),
    where a targeted lookup per chunk is the wrong shape. `_sizes_for` is the default everywhere
    else. Deliberately NOT memoized in the app: this is a multi-million-entry dict, and pinning one
    between publishes is exactly the RAM the walk cache carried. It is rebuilt per call, as the
    sizemap was.
    """
    cur = read_cursor(repo)
    try:
        rows = cur.execute(
            "SELECT c.content_id, m.packed FROM csize m JOIN cid c ON c.cid_id = m.cid_id"
        ).fetchall()
    finally:
        cur.close()
    if rows:
        return dict(rows)
    # A generation published before the unified store has no csize map yet - fall back to the
    # engine's sizemap so an old generation still prices. An unreadable map must not break a
    # simulate: it degrades to zeros, which the caller reports as-is.
    try:
        return load_sizemap(repo, refresh=refresh)
    except Exception:  # noqa: BLE001
        return {}


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
    named, which is inherently the full dictionary.

    REFCOUNT_MEMO_ENABLED (the nightly worker turns it on in worker.main) memoizes the built
    dict per generation: the 3.5M-row join + dict cost ~1.8s and was paid by every analysis
    source. The WeakKeyDictionary key is the Generation handle, so an entry lives exactly as
    long as the crefs artifact it describes and dies with the next publish. Scoped to the
    worker on purpose - the long-lived app must not pin the map in RAM."""
    cur = read_cursor(repo)
    gen = getattr(cur, "generation", None)
    try:
        if REFCOUNT_MEMO_ENABLED and gen is not None:
            hit = _REFCOUNT_MEMO.get(gen)
            if hit is not None:
                return hit
        rows = cur.execute(
            "SELECT c.content_id, m.refs FROM crefs m JOIN cid c ON c.cid_id = m.cid_id"
        ).fetchall()
    finally:
        cur.close()
    if rows:
        count: dict[str, int] = {cid: refs for cid, refs in rows}
        if REFCOUNT_MEMO_ENABLED and gen is not None:
            _REFCOUNT_MEMO[gen] = count
        return count
    # Cold start only: a generation from before the unified store carries no crefs map. Counted
    # from the tree in one query rather than by walking every source.
    from ..core.metrics import cached_refcount  # lazy: avoids a heavy import on hot paths
    return cached_refcount(repo)


_REFCOUNT_MEMO: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
REFCOUNT_MEMO_ENABLED = False


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


def _objects_by_path(cur, handles: set[tuple[str, str]]) -> dict[tuple[str, str], list[str]]:
    """(source, full_path) -> every distinct object the path has held across its versions.
    Batched path resolution, then one chunked filev scan."""
    objs_of: dict[tuple[str, str], list[str]] = {h: [] for h in handles}
    if not handles:
        return objs_of
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
    return objs_of


def stored_all_versions(repo: str, handles: list[tuple[str, str]]) -> dict[tuple[str, str], int | None]:
    """Packed bytes of the union of chunks across every version a path has held (chunks shared
    between its own versions count once). None when a version's chunk list is not in the walk
    cache; never calls kopia."""
    uniq = set(handles)
    if not uniq:
        return {}
    from .store import read_manifest
    gen = (read_manifest(repo) or {}).get("generation")
    out: dict[tuple[str, str], int | None] = {}
    todo = set()
    for h in uniq:
        hit = _reclaim_cache_get(("stored", repo, gen) + h)
        if hit is not None:
            out[h] = hit
        else:
            todo.add(h)
    if not todo:
        return out
    cur = read_cursor(repo)
    try:
        objs_of = _objects_by_path(cur, todo)
    finally:
        cur.close()
    # One batched lookup for every object across every handle, instead of one query each.
    memo = _chunks_for_objs(repo, [o for h in todo for o in objs_of.get(h, [])])
    chunk_sets: dict[tuple, set[str]] = {}
    for h in todo:
        chunks: set[str] | None = set()
        for obj in objs_of.get(h, []):
            if memo.get(obj) is None:
                chunks = None
                break
            chunks |= memo[obj]
        if chunks is not None:
            chunk_sets[h] = chunks
    sizes = _sizes_for(repo, {c for cs in chunk_sets.values() for c in cs})
    for h in todo:
        chunks = chunk_sets.get(h)
        out[h] = (sum(sizes.get(c, 0) for c in chunks)
                  if chunks is not None and objs_of.get(h) else None)
        if out[h] is not None:
            _reclaim_cache_put(("stored", repo, gen) + h, out[h])
    return out


def reclaim_for(repo: str, content_ids: list[str], cap: int = 500, fetch: bool = True) -> dict[str, int]:
    """For each content id (a file's latest object), compute the TRUE 'reclaim if purged
    everywhere': the deduped union of chunks across ALL versions of ALL paths sharing that
    content, whose global refcount is fully attributable to those paths. Cached per content id.
    This is the honest all-versions, content-aware freeable for the search 'Reclaim' column.

    The two index lookups are batched across the whole request: nothing indexes
    `files.content_id`, so the per-id loop scanned the full history once per id - up to 500 scans
    for one Reclaim click. One scan for "which paths hold these ids" plus one for "which objects
    each of those paths has held" returns byte-identical results, and the chunk-expansion memo is
    shared across the page so a repeated indirect object is expanded once, not per id.

    ``fetch=False`` never calls kopia: an id whose chunk lists are not all in the walk cache is left
    out of the answer and not cached."""
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

        handles = {(src, fp) for hs in shares.values() for src, fp in hs}
        objs_of = _objects_by_path(cur, handles)

        chunk_memo = _chunks_for_objs(
            repo, [o for hs in objs_of.values() for o in hs], cursor=cur)
        for cid in misses:
            this_refs: dict[str, int] = {}
            unknown = False
            for src, fp in shares.get(cid, []):
                for obj in objs_of.get((src, fp), []):
                    chunks = chunk_memo.get(obj)
                    if chunks is None:
                        unknown = True
                        break
                    for c in chunks:
                        this_refs[c] = this_refs.get(c, 0) + 1
                if unknown:
                    break
            if unknown:
                continue
            refs = _refcount_for(repo, this_refs.keys()) or {}
            sizes = _sizes_for(repo, this_refs)
            reclaim = sum(sizes.get(c, 0) for c in this_refs if refs.get(c, 0) <= this_refs[c])
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
    cur = read_cursor(repo)
    try:
        where, params = scope_sql(cur, prefix, include_path=True)
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
    unknown = 0
    # One batched lookup for every object in the folder. No kopia fetch: an aggregate can hold
    # thousands of objects, and one call each is the per-item shape this store exists to remove.
    # An object the tree has not reached is left out and counted - understated, loudly, rather
    # than crashing or silently zeroed.
    obj_chunks = _chunks_for_objs(repo, [cstr[c] for c, _n in objs if c])
    for cid_id, n in objs:
        if not cid_id:
            continue
        cid = cstr[cid_id]
        chunks = obj_chunks.get(cid)
        if chunks is None:
            unknown += 1
            continue
        for c in chunks:
            this_refs[c] = this_refs.get(c, 0) + int(n)
    if unknown:
        log.warning("repo=%s folder detail: %d object(s) are not in the tree yet - their bytes are "
                    "not counted until the next index", repo, unknown)
    refs = _refcount_for(repo, this_refs.keys()) or {}
    sizes = _sizes_for(repo, this_refs)
    physical = sum(sizes.get(c, 0) for c in this_refs)
    freeable = sum(sizes.get(c, 0) for c in this_refs if refs.get(c, 0) <= this_refs[c])
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
    obj_chunks: dict[str, set[str]] = {}
    this_refs: dict[str, int] = {}     # references the selection contributes per chunk (all versions)
    latest: dict[tuple, tuple] = {}    # (source,fp) -> (latest content_id, latest size)
    versions = 0
    unknown = 0                        # objects the tree has not reached; counted, never zeroed
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
        obj_chunks = _chunks_for_objs(
            repo, [o for rows in by_path.values()
                   for o, _s in (rows if all_versions else rows[:1])], cursor=cur)
        for key, rows in by_path.items():
            if not rows:
                continue
            latest[key] = (rows[0][0], int(rows[0][1] or 0))
            for obj, _sz in (rows if all_versions else rows[:1]):
                versions += 1
                chunks = obj_chunks.get(obj)
                if chunks is None:
                    unknown += 1
                    continue
                for c in chunks:
                    this_refs[c] = this_refs.get(c, 0) + 1
    finally:
        cur.close()
    if not latest:
        return {"found": False}
    if unknown:
        log.warning("repo=%s batch detail: %d version object(s) are not in the tree yet - their "
                    "bytes are not counted until the next index", repo, unknown)

    union_chunks = set(this_refs)
    refs = _refcount_for(repo, union_chunks)
    if refs is None:
        refs = _refcount_map(repo)
    sizes = _sizes_for(repo, union_chunks)
    physical = sum(sizes.get(c, 0) for c in union_chunks)
    freeable = sum(sizes.get(c, 0) for c in union_chunks if refs.get(c, 0) <= this_refs.get(c, 0))

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

    this_refs: dict[str, int] = {}  # references this file contributes per chunk (across versions)
    unknown = 0
    # One batched lookup for the file's versions, then - and only here - kopia for anything the
    # tree has not reached. A file backed up since the last index is exactly what a user opens
    # this panel to look at, and "-" where a size belongs is the least useful honest answer.
    obj_chunks = _chunks_for_objs(repo, [o for _s, _t, _z, o in versions])
    for obj, got in list(obj_chunks.items()):
        if got is None and obj.startswith("I"):
            obj_chunks[obj] = _chunks_from_kopia(repo, obj)
    for _sid, _start, _size, obj in versions:
        chunks = obj_chunks.get(obj)
        if chunks is None:
            unknown += 1
            continue
        for c in chunks:
            this_refs[c] = this_refs.get(c, 0) + 1
    if unknown:
        log.warning("repo=%s file detail %s: %d version(s) could not be measured", repo,
                    full_path, unknown)

    latest_obj = versions[0][3]
    latest_chunks = obj_chunks.get(latest_obj, set())
    union_chunks = set(this_refs)

    refs = _refcount_for(repo, union_chunks)  # targeted (fast); None => content_refs not built yet
    if refs is None:
        refs = _refcount_map(repo)            # cold-start fallback (full map / rebuild)

    sizes = _sizes_for(repo, union_chunks)
    physical_latest = sum(sizes.get(c, 0) for c in latest_chunks)
    physical_total = sum(sizes.get(c, 0) for c in union_chunks)
    freeable = sum(sizes.get(c, 0) for c in union_chunks if refs.get(c, 0) <= this_refs.get(c, 0))
    shared = physical_total - freeable

    chunk_rows = []
    for c in sorted(latest_chunks, key=lambda x: sizes.get(x, 0), reverse=True)[:200]:
        g = refs.get(c, 0)
        chunk_rows.append({
            "cid": c, "packed": sizes.get(c, 0), "refs": g,
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
        # the SOURCES holding this exact PATH (raw-exact identity) - what the file panel's
        # "Backed up by" list renders. Distinct from `others` (other PATHS with the same bytes)
        # and from `instances` (distinct paths): one concept per field, never conflated.
        machine = source.split(":", 1)[0]   # another machine's identical path is a different file
        holders = [source] + [r[0] for r in cur.execute(
            "SELECT DISTINCT s.source FROM pathx p JOIN src s ON s.src_id = p.src_id "
            "WHERE p.full_path = ? AND s.source <> ? ORDER BY s.source",
            [full_path, source],
        ).fetchall() if r[0].split(":", 1)[0] == machine]
    finally:
        cur.close()

    # Content-centric reclaim: what is freed if this content is removed from every location. Per-path
    # `freeable` reads ~0 for a multi-copy file (the copies keep it alive). A chunk is freed iff its
    # global source-refcount is fully attributable to the duplicate-holding sources. The snapshots
    # holding it come from /api/delete/plan (the delta index cannot list them).
    dup_sources = {source} | {s for s, _ in others}
    n_dup = len(dup_sources)
    reclaim_everywhere = sum(sizes.get(c, 0) for c in latest_chunks if refs.get(c, 0) <= n_dup)

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
        "holders": holders,                       # every source backing up this exact path
        # content-centric / purge-everywhere
        "locations": n_dup,                       # distinct sources holding this exact content
        "instances": len(others) + 1,             # distinct paths holding it (this one + others)
        "reclaim_everywhere": reclaim_everywhere,  # bytes freed if purged from every location
    }
