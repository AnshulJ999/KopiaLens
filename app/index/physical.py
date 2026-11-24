"""Per-file footprint analysis: physical, freeable, dedup, and what a delete would reclaim.

Everything comes from the generation the app is already reading ( step 4): the content
sizes and reference counts are the published ``csize`` / ``crefs`` maps, and a split file's chunks
come from the tree's ``tpart`` piece lists. Nothing here walks kopia or touches the walk cache.

A file's stored ``content_id`` is the object id: a single content for small files, or an
``I``-prefixed split object for large ones (the ones that matter for reclaim).
"""

from __future__ import annotations

import collections
import threading
import uuid
import weakref
from types import SimpleNamespace

from ..core import paths as pathrules
from ..core.applog import get_logger
from ..core.sizemap import load_sizemap
from . import resolve
from .queries import _ESC, _HOST_SQL, like_literal, scope_sql
from .store import read_cursor

log = get_logger("physical")


def _chunks_from_kopia(repo: str, obj: str) -> set[str] | None:
    """A split object's piece list straight from kopia - the click-time fallback.

    A file backed up since the last tree sync is not in the tree, and a detail panel still has to
    answer. Same shape the sync writes: the index object is chunk ordinal 0, so it belongs in the
    set. A failed read stays None - unknown is not zero bytes.
    """
    from .treesync import split_pieces
    pieces = split_pieces(repo, obj)
    return set(pieces) if pieces is not None else None


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


def _path_pairs(cur, objs) -> dict[str, int]:
    """{object: (source, path) pairs holding it in the history}, only where above one. One targeted
    count; a precomputed map measured no faster."""
    import pyarrow as pa

    if not objs:
        return {}
    cur.register("_want_objs", pa.table({"content_id": pa.array(list(objs), pa.string())}))
    try:
        return dict(cur.execute(
            "SELECT c.content_id, count(DISTINCT f.path_id) FROM _want_objs w "      # a path id is per source
            "JOIN cid c ON c.content_id = w.content_id JOIN filev f ON f.cid_id = c.cid_id "
            "GROUP BY c.content_id HAVING count(DISTINCT f.path_id) > 1").fetchall())
    finally:
        cur.unregister("_want_objs")


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


def _objrefs_for(repo: str, cids) -> dict[str, int]:
    """How many distinct (source, split object) pairs use each piece (``crefo``). A piece with no
    row is a plain object, or the generation predates the map: one object, so ``crefs`` decides."""
    cur = read_cursor(repo)
    try:
        return _values_by_content_id(cur, [c for c in dict.fromkeys(cids) if c], "crefo", "refs")
    finally:
        cur.close()


def _freed(c: str, refs: dict, objrefs: dict, sources: int, pairs: int) -> bool:
    """A piece is freed when every source reaching it and every (source, object) pair using it is
    inside the deletion. Comparing sources with path or version counts was the [C1] overstatement."""
    return refs.get(c, 0) <= sources and objrefs.get(c, 0) <= pairs


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


def _price(cur, objs: str, params: list | None = None, kept: str | None = None) -> dict:
    """Price one deletion. ``objs`` is SQL yielding DISTINCT ``(src_id, cid_id)``: every (source,
    content object) pair the deletion removes. A piece is freed when every source (crefs) and every
    (source, object) pair (crefo) using it is in ``objs`` - the rule `_freed` states - and it is not
    a piece of a ``kept`` object (SQL yielding ``cid_id``: objects a file outside the deletion still
    holds). A split object with no piece list is not in the tree yet: counted as unknown and left
    out (understated, never zeroed). ``params`` bind the placeholders of ``objs`` then ``kept``.

    Returns ``{objects, unknown, chunks, physical, freeable, kept}`` - ``kept`` is the packed bytes
    of the kept objects' pieces."""
    kept_cte = kept_join = ""
    kept_col = "false"
    if kept:
        kept_cte = (f", keptobj AS ({kept}), keptch AS (SELECT cid_id AS chunk FROM keptobj UNION "
                    "SELECT t.part_cid FROM keptobj k JOIN tpart t ON t.iobj_cid = k.cid_id)")
        kept_join = "LEFT JOIN keptch kc ON kc.chunk = m.chunk"
        kept_col = "kc.chunk IS NOT NULL"
    row = cur.execute(f"""
        WITH objs AS ({objs}),
        kinds AS (SELECT o.src_id, o.cid_id, c.content_id LIKE 'I%' AS split
                  FROM objs o JOIN cid c ON c.cid_id = o.cid_id),
        chunks AS (SELECT src_id, cid_id AS obj, cid_id AS chunk FROM kinds WHERE NOT split
                   UNION ALL
                   SELECT k.src_id, k.cid_id, t.part_cid FROM kinds k
                   JOIN tpart t ON t.iobj_cid = k.cid_id WHERE k.split),
        mine AS (SELECT chunk, count(DISTINCT src_id) AS nsrc,
                        count(DISTINCT (src_id, obj)) AS npair
                 FROM chunks GROUP BY chunk){kept_cte},
        sized AS (SELECT m.nsrc, m.npair, COALESCE(r.refs, 0) AS total, COALESCE(o.refs, 0) AS orefs,
                         COALESCE(s.packed, 0) AS packed, {kept_col} AS kept
                  FROM mine m LEFT JOIN crefs r ON r.cid_id = m.chunk
                  LEFT JOIN crefo o ON o.cid_id = m.chunk
                  LEFT JOIN csize s ON s.cid_id = m.chunk {kept_join})
        SELECT (SELECT count(DISTINCT cid_id) FROM objs),
               (SELECT count(DISTINCT k.cid_id) FROM kinds k WHERE k.split AND NOT EXISTS
                  (SELECT 1 FROM tpart t WHERE t.iobj_cid = k.cid_id)),
               count(*),
               COALESCE(sum(packed), 0),
               COALESCE(sum(packed) FILTER (WHERE total <= nsrc AND orefs <= npair AND NOT kept), 0),
               COALESCE(sum(packed) FILTER (WHERE kept), 0)
        FROM sized""", params or []).fetchone() or (0, 0, 0, 0, 0, 0)
    objects, unknown, chunks, physical, freeable, kept_b = (int(v or 0) for v in row)
    return {"objects": objects, "unknown": unknown, "chunks": chunks, "physical": physical,
            "freeable": freeable, "kept": kept_b}


def _kept_sql(sel: str) -> str:
    """Objects a deletion of the paths in ``sel`` (SQL yielding ``path_id``) leaves stored: held
    by more paths anywhere in the repo than inside the set, i.e. by a copy outside it. Every
    version counts. The same rule as the file panel."""
    vers = f"SELECT DISTINCT x.path_id, f.cid_id FROM ({sel}) x JOIN filev f ON f.path_id = x.path_id"
    return (f"SELECT i.cid_id FROM (SELECT cid_id, count(DISTINCT path_id) AS n FROM ({vers}) "
            f"GROUP BY cid_id) i JOIN (SELECT f.cid_id, count(DISTINCT f.path_id) AS n FROM filev f "
            f"WHERE f.cid_id IN (SELECT cid_id FROM ({vers})) GROUP BY f.cid_id) h "
            f"ON h.cid_id = i.cid_id WHERE h.n > i.n")


def _objs_sql(sel: str, all_versions: bool) -> str:
    """The (source, object) pairs a deletion of the paths in ``sel`` (SQL yielding ``src_id,
    path_id``) removes: every version's object, or each path's newest one."""
    if all_versions:
        return (f"SELECT DISTINCT x.src_id, f.cid_id FROM ({sel}) x "
                f"JOIN filev f ON f.path_id = x.path_id WHERE f.cid_id IS NOT NULL")
    return (f"SELECT DISTINCT x.src_id, p.cid_id FROM ({sel}) x "
            f"JOIN pathx p ON p.path_id = x.path_id WHERE p.cid_id IS NOT NULL")


def _newest_per_file_sql(sel: str) -> str:
    """Each FILE's newest object (the holder that saw it last), for Physical of the newest
    versions. Nested sources snapshotted at different times each hold a "newest" object; only
    the latest one is the file's newest version."""
    return (f"SELECT DISTINCT src_id, cid_id FROM (SELECT x.src_id, p.cid_id, row_number() OVER ("
            f"PARTITION BY {_HOST_SQL}, p.full_path ORDER BY p.last_seen DESC, p.path_id DESC) AS rn "
            f"FROM ({sel}) x JOIN pathx p ON p.path_id = x.path_id JOIN src s ON s.src_id = p.src_id) "
            f"WHERE rn = 1 AND cid_id IS NOT NULL")


def _register_files(cur, files) -> str | None:
    """Register (source, full_path) pairs; returns SQL yielding their ``src_id, path_id`` (RAW-EXACT,
    one hash join at any size - trap 29). Unregister with ``_unregister``."""
    import pyarrow as pa

    sids = resolve.src_map(cur)
    rows = list(dict.fromkeys((sids[s], fp) for s, fp in files if s in sids and fp))
    if not rows:
        return None
    name = f"_sel_{uuid.uuid4().hex[:10]}"
    cur.register(name, pa.table({"src_id": pa.array([r[0] for r in rows], pa.int64()),
                                 "full_path": pa.array([r[1] for r in rows], pa.string())}))
    return name


def _unregister(cur, name: str | None) -> None:
    if name:
        try:
            cur.unregister(name)
        except Exception:  # noqa: BLE001 - already gone
            pass


def _host_table() -> str:
    """SQL: every source's host, the computer half of a file's identity."""
    return f"(SELECT s.src_id, {_HOST_SQL} AS host FROM src s)"


def frees_sql(targets: str) -> str:
    """SQL yielding ``(t, value)``: what removing each target FILE frees (). ``targets`` yields
    ``(t, src_id, path_id)`` - every holder path of each file (nested sources on one computer hold
    one file). Every version goes, from every holder; an object another path still holds keeps its
    pieces; a piece is freed when every source and (source, object) pair using it is the file's.
    The file panel's Reclaimable card, for many files at once. A file with a split object not yet
    in the tree gets no row (unknown, never zero)."""
    return f"""
        WITH vers AS (SELECT DISTINCT g.t, g.src_id, g.path_id, f.cid_id FROM ({targets}) g
                      JOIN filev f ON f.path_id = g.path_id WHERE f.cid_id IS NOT NULL),
        inside AS (SELECT t, cid_id, count(DISTINCT path_id) AS n FROM vers GROUP BY t, cid_id),
        held AS (SELECT f.cid_id, count(DISTINCT f.path_id) AS n FROM filev f
                 WHERE f.cid_id IN (SELECT cid_id FROM inside) GROUP BY f.cid_id),
        keptobj AS (SELECT i.t, i.cid_id FROM inside i JOIN held h ON h.cid_id = i.cid_id
                    WHERE h.n > i.n),
        keptch AS (SELECT t, cid_id AS chunk FROM keptobj
                   UNION SELECT k.t, tp.part_cid FROM keptobj k JOIN tpart tp ON tp.iobj_cid = k.cid_id),
        ch AS (SELECT DISTINCT v.t, v.src_id, v.cid_id AS obj, coalesce(tp.part_cid, v.cid_id) AS chunk,
                      (c.content_id LIKE 'I%' AND tp.part_cid IS NULL) AS unknown
               FROM vers v JOIN cid c ON c.cid_id = v.cid_id
               LEFT JOIN tpart tp ON tp.iobj_cid = v.cid_id),
        mine AS (SELECT t, chunk, count(DISTINCT src_id) AS nsrc,
                        count(DISTINCT (src_id, obj)) AS npair
                 FROM ch WHERE t NOT IN (SELECT t FROM ch WHERE unknown) GROUP BY t, chunk)
        SELECT m.t, COALESCE(sum(s.packed) FILTER (
                   WHERE coalesce(r.refs, 0) <= m.nsrc AND coalesce(o.refs, 0) <= m.npair
                     AND k.chunk IS NULL), 0)::UBIGINT AS value
        FROM mine m LEFT JOIN crefs r ON r.cid_id = m.chunk
        LEFT JOIN crefo o ON o.cid_id = m.chunk
        LEFT JOIN csize s ON s.cid_id = m.chunk
        LEFT JOIN keptch k ON k.t = m.t AND k.chunk = m.chunk
        GROUP BY m.t"""


def frees_for_files(repo: str, handles: list[tuple[str, str]]) -> dict[tuple[str, str], int | None]:
    """{(source, full_path): bytes freed by removing that file} for a page of Search rows - the
    file panel's Reclaimable, one query for the page. None where a piece list is unknown."""
    import pyarrow as pa

    uniq = list(dict.fromkeys(h for h in handles if h[0] and h[1]))
    if not uniq:
        return {}
    cur = read_cursor(repo)
    name = f"_ff_{uuid.uuid4().hex[:10]}"
    try:
        cur.register(name, pa.table({
            "t": pa.array(range(len(uniq)), pa.int64()),
            "host": pa.array([pathrules.host(s) for s, _ in uniq], pa.string()),
            "full_path": pa.array([fp for _, fp in uniq], pa.string())}))
        targets = (f"SELECT w.t, p.src_id, p.path_id FROM {name} w JOIN pathx p ON p.full_path = w.full_path "
                   f"JOIN {_host_table()} h ON h.src_id = p.src_id AND h.host = w.host")
        got = dict(cur.execute(frees_sql(targets)).fetchall())
    finally:
        _unregister(cur, name)
        cur.close()
    return {h: (int(got[i]) if i in got else None) for i, h in enumerate(uniq)}


def folder_detail(repo: str, source: str | None, prefix: str, all_versions: bool = True,
                  filters: dict | None = None, loose: bool = False) -> dict:
    """What deleting a folder frees, over every version (or each path's newest, matching a
    latest-only delete plan), plus the folder's Physical for its newest versions
    (``physical_newest``: each stored piece once -).

    ``source=None`` spans EVERY source covering the folder: a piece held by two sources only counts
    as freeable when both are inside the folder. ``filters`` (``queries.folder_file_filter``
    keywords) narrow it to the files matching a Search filter; ``loose`` to the files lying
    directly in it. Files and Logical count each file once - a path held by nested sources is one
    file ()."""
    from .queries import _folder_dirs, folder_file_filter
    cur = read_cursor(repo)
    try:
        if loose:
            ids = _folder_dirs(cur, prefix)[0]
            where, params = (f"p.dir_id IN ({','.join(str(i) for i in ids)})" if ids else "1 = 0"), []
        else:
            where, params = scope_sql(cur, prefix, include_path=True)
        where = where or "true"
        if filters:
            conds, fparams = folder_file_filter(cur, **filters)
            if conds:
                where = f"({where}) AND " + " AND ".join(conds)
                params = [*params, *fparams]
        empty = False
        if source:
            sid = resolve.src_id(cur, source)
            if sid is None:  # an unknown source matched no rows before; keep that
                empty = True
            else:
                where += " AND p.src_id = ?"
                params.append(sid)
        if empty:
            files = logical = 0
            priced = newest = {"objects": 0, "unknown": 0, "physical": 0, "freeable": 0}
        else:
            vjoin, size = (("JOIN filev f ON f.path_id = p.path_id", "f.size") if all_versions
                           else ("", "p.size"))
            files, logical = cur.execute(
                f"SELECT count(*), COALESCE(sum(sz), 0) FROM (SELECT max({size}) AS sz FROM pathx p "
                f"JOIN src s ON s.src_id = p.src_id {vjoin} WHERE {where} "
                f"GROUP BY {_HOST_SQL}, p.full_path)",
                params).fetchone() or (0, 0)
            sel = f"SELECT p.src_id, p.path_id FROM pathx p WHERE {where}"
            zero = {"objects": 0, "unknown": 0, "physical": 0, "freeable": 0}
            newest = _price(cur, _newest_per_file_sql(sel), params) if files else zero
            # A latest-only delete removes every holder's newest object, so it is priced per holder.
            priced = (_price(cur, _objs_sql(sel, all_versions), params) if files else zero)
    finally:
        cur.close()
    if priced["unknown"]:
        log.warning("repo=%s folder detail: %d object(s) are not in the tree yet - their bytes are "
                    "not counted until the next index", repo, priced["unknown"])
    return {
        "logical": int(logical or 0), "physical": priced["physical"], "freeable": priced["freeable"],
        "files": int(files or 0), "shared": priced["physical"] - priced["freeable"],
        "objects": priced["objects"], "all_versions": all_versions,
        "physical_newest": newest["physical"],
    }


def batch_detail(repo: str, files: list[dict], all_versions: bool = True,
                 keep_copies: bool = False) -> dict:
    """What deleting a SET of files frees - the multi-file equivalent of the file panel.
    physical = packed bytes of every piece the set's versions use; freeable = the pieces nothing
    outside the set uses; logical = the newest sizes, identical content counted once.

    ``all_versions=False`` prices each path's newest version only, matching a latest-only delete
    plan. ``keep_copies`` is for deletes that remove exactly these paths (by file name): an object
    another path still holds keeps its pieces stored (``kept`` = their bytes). Without it the
    set is priced as a content delete, where copies in the same sources go too."""
    pairs = [(f.get("source"), f.get("full_path")) for f in files if f.get("source") and f.get("full_path")]
    if not pairs:
        return {"found": False}
    cur = read_cursor(repo)
    name = None
    try:
        name = _register_files(cur, pairs)
        if name is None:
            return {"found": False}
        sel = (f"SELECT p.src_id, p.path_id FROM {name} w JOIN pathx p "
               f"ON p.src_id = w.src_id AND p.full_path = w.full_path")
        nfiles, ncontent, srcs = cur.execute(
            f"SELECT count(DISTINCT ({_HOST_SQL}, p.full_path)), count(DISTINCT p.cid_id), "
            f"list_sort(list(DISTINCT s.source)) FROM {name} w JOIN pathx p "
            f"ON p.src_id = w.src_id AND p.full_path = w.full_path JOIN src s ON s.src_id = p.src_id"
        ).fetchone()
        if not nfiles:
            return {"found": False}
        logical = cur.execute(
            f"SELECT COALESCE(sum(sz), 0) FROM (SELECT max(p.size) AS sz FROM ({sel}) x "
            f"JOIN pathx p ON p.path_id = x.path_id GROUP BY coalesce(p.cid_id, -p.path_id))").fetchone()[0]
        versions = cur.execute(
            f"SELECT count(*) FROM ({sel}) x JOIN filev f ON f.path_id = x.path_id" if all_versions
            else f"SELECT count(*) FROM ({sel})").fetchone()[0]
        priced = _price(cur, _objs_sql(sel, all_versions),
                        kept=_kept_sql(f"SELECT path_id FROM ({sel})") if keep_copies else None)
    finally:
        _unregister(cur, name)
        cur.close()
    if priced["unknown"]:
        log.warning("repo=%s batch detail: %d object(s) are not in the tree yet - their bytes are "
                    "not counted until the next index", repo, priced["unknown"])
    sources = list(srcs or [])
    out = {
        "found": True,
        "files": int(nfiles),
        "distinct_content": int(ncontent or 0),
        "sources": sources,
        "source_count": len(sources),
        "versions": int(versions or 0),
        "chunk_count": priced["chunks"],
        "logical": int(logical or 0),
        "physical": priced["physical"],
        "freeable": priced["freeable"],
        "shared": priced["physical"] - priced["freeable"],
        "all_versions": all_versions,
    }
    if keep_copies:
        out["kept"] = priced["kept"]
    return out


def copies_outside(repo: str, files: list[tuple[str, str]], sources: list[str] | None = None,
                   cap: int = 500, all_versions: bool = True) -> dict:
    """Files OUTSIDE ``files`` that hold an object of them (any version's, or only each path's
    newest) - identical copies at other paths. ``sources`` limits where to look (None = every
    source). One row per file:
    ``{"rows": [{source, sources, full_path, size, physical, in_latest}], "total", "logical",
    "physical"}``; totals cover every copy."""
    from .queries import _per_file, _rows
    empty = {"rows": [], "total": 0, "logical": 0, "physical": 0}
    cur = read_cursor(repo)
    name = None
    try:
        name = _register_files(cur, files)
        if name is None:
            return empty
        sel = (f"SELECT p.path_id FROM {name} w JOIN pathx p "
               f"ON p.src_id = w.src_id AND p.full_path = w.full_path")
        mine = (f"SELECT f.cid_id FROM ({sel}) x JOIN filev f ON f.path_id = x.path_id" if all_versions
                else f"SELECT q.cid_id FROM ({sel}) x JOIN pathx q ON q.path_id = x.path_id")
        where = (f"p.path_id IN (SELECT f2.path_id FROM filev f2 WHERE f2.cid_id IN ({mine})) "
                 f"AND p.path_id NOT IN ({sel})")
        params: list = []
        if sources is not None:
            sids = [i for i in (resolve.src_id(cur, s) for s in sources) if i is not None]
            if not sids:
                return empty
            where += " AND p.src_id IN (%s)" % ",".join("?" * len(sids))
            params = sids
        # A file whose other holder is inside the set is the same file, not a copy.
        base = _per_file(where)
        own = f"SELECT DISTINCT {_HOST_SQL} AS host, p.full_path FROM ({sel}) x JOIN pathx p ON p.path_id = x.path_id JOIN src s ON s.src_id = p.src_id"
        base = f"SELECT b.* FROM ({base}) b ANTI JOIN ({own}) o ON o.host = b.host AND o.full_path = b.full_path"
        total, logical, phys = cur.execute(
            f"SELECT count(*), COALESCE(sum(size), 0), COALESCE(sum(physical), 0) FROM ({base})",
            params).fetchone()
        rows = _rows(cur, f"SELECT source, sources, full_path, size, physical, in_latest FROM ({base}) "
                          f"ORDER BY physical DESC NULLS LAST, size DESC, full_path LIMIT {int(cap)}", params)
    finally:
        _unregister(cur, name)
        cur.close()
    return {"rows": rows, "total": int(total or 0), "logical": int(logical or 0),
            "physical": int(phys or 0)}


def price_objects(repo: str, object_ids: list[str], sources: list[str] | None = None) -> dict:
    """What removing these content objects frees inside ``sources`` (None = every source) - a
    content delete: every path holding them in those sources loses them, and nothing else goes.
    ``{objects, unknown, physical, freeable}``."""
    import pyarrow as pa

    oids = list(dict.fromkeys(o for o in object_ids if o))
    zero = {"objects": 0, "unknown": 0, "chunks": 0, "physical": 0, "freeable": 0, "kept": 0}
    if not oids:
        return zero
    cur = read_cursor(repo)
    name = f"_po_{uuid.uuid4().hex[:10]}"
    try:
        cur.register(name, pa.table({"content_id": pa.array(oids, pa.string())}))
        where = ""
        params: list = []
        if sources is not None:
            sids = [i for i in (resolve.src_id(cur, s) for s in sources) if i is not None]
            if not sids:
                return zero
            where = " AND p.src_id IN (%s)" % ",".join("?" * len(sids))
            params = sids
        objs = (f"SELECT DISTINCT p.src_id, f.cid_id FROM {name} w JOIN cid c ON c.content_id = w.content_id "
                f"JOIN filev f ON f.cid_id = c.cid_id JOIN pathx p ON p.path_id = f.path_id WHERE true{where}")
        return _price(cur, objs, params)
    finally:
        _unregister(cur, name)
        cur.close()


_PRICED: dict[tuple, object] = {}   # the newest few files priced, per generation
_PRICED_MAX = 4
_PRICED_LOCK = threading.Lock()


def _price_file(repo: str, source: str, full_path: str):
    """One file's pricing state, shared by the file panel and its storage breakdown: every version
    in every holder, the pieces they use, and which of those pieces deleting the file frees.
    None when the path is unknown. The panel's card and its breakdown price the same file, so the
    last few answers are kept for their generation."""
    cur = read_cursor(repo)
    try:
        gen = getattr(cur, "generation", None)
        ident = (getattr(gen, "id", None), getattr(gen, "_mtime_ns", None))
    finally:
        cur.close()
    if None in ident:
        return _price_file_now(repo, source, full_path)
    key = (repo, *ident, source, full_path)
    with _PRICED_LOCK:
        if key in _PRICED:
            return _PRICED[key]
    st = _price_file_now(repo, source, full_path)
    with _PRICED_LOCK:
        _PRICED[key] = st
        while len(_PRICED) > _PRICED_MAX:
            _PRICED.pop(next(iter(_PRICED)))
    return st


def _price_file_now(repo: str, source: str, full_path: str):
    cur = read_cursor(repo)
    try:
        sid = resolve.src_id(cur, source)
        pid = resolve.path_id(cur, sid, full_path) if sid is not None else None
        if pid is None:  # unknown source or path: nothing was found before, none now
            return None
        versions = cur.execute(
            "SELECT sn.snapshot_id, sn.start_time, f.size, c.content_id FROM filev f "
            "JOIN snap sn ON sn.snap_id = f.snap_id "
            "LEFT JOIN cid c ON c.cid_id = f.cid_id "
            "WHERE f.path_id = ? ORDER BY sn.start_time DESC, sn.snapshot_id DESC",
            [pid],
        ).fetchall()
        # the SOURCES holding this exact PATH (raw-exact identity) on this computer; another
        # computer's identical path is a different file.
        host = pathrules.host(source)
        holders = [source] + [r[0] for r in cur.execute(
            "SELECT DISTINCT s.source FROM pathx p JOIN src s ON s.src_id = p.src_id "
            "WHERE p.full_path = ? AND s.source <> ? ORDER BY s.source",
            [full_path, source],
        ).fetchall() if pathrules.host(r[0]) == host]
        sids = resolve.src_map(cur)
        objs_by_holder: dict[str, set[str]] = {source: {r[3] for r in versions if r[3]}}
        if len(holders) > 1:
            rev = {v: k for k, v in sids.items()}
            pids = resolve.path_ids(cur, [(sids[h], full_path) for h in holders[1:] if h in sids])
            for (hsid, _fp), hpid in pids.items():
                objs_by_holder[rev[hsid]] = {r[0] for r in cur.execute(
                    "SELECT DISTINCT c.content_id FROM filev f JOIN cid c ON c.cid_id = f.cid_id "
                    "WHERE f.path_id = ?", [hpid]).fetchall() if r[0]}
        all_objs = sorted({o for objs in objs_by_holder.values() for o in objs})
        # Objects this file shares with any OTHER path (or another machine) stay stored.
        oid_of = resolve.cid_ids(cur, all_objs)
        # An object held by more (source, path) pairs than this file's own holders lives elsewhere too.
        pairs = _path_pairs(cur, all_objs)
        own = {o: sum(o in objs for objs in objs_by_holder.values()) for o in all_objs}
        kept_objs = {o for o in all_objs if o in oid_of and pairs.get(o, 1) > own[o]}
    finally:
        cur.close()
    if not versions:
        return None

    unknown = 0
    # One batched lookup for every holder's versions, then - and only here - kopia for anything the
    # tree has not reached. A file backed up since the last index is exactly what a user opens
    # this panel to look at, and "-" where a size belongs is the least useful honest answer.
    obj_chunks = _chunks_for_objs(repo, all_objs)
    for obj, got in list(obj_chunks.items()):
        if got is None and obj.startswith("I"):
            obj_chunks[obj] = _chunks_from_kopia(repo, obj)
    holder_refs: dict[str, set[str]] = {}   # chunk -> holders whose versions of this path use it
    pair_refs: dict[str, set[tuple]] = {}    # chunk -> (holder, object) pairs using it
    for h, objs in objs_by_holder.items():
        for obj in objs:
            chunks = obj_chunks.get(obj)
            if chunks is None:
                unknown += 1
                continue
            for c in chunks:
                holder_refs.setdefault(c, set()).add(h)
                pair_refs.setdefault(c, set()).add((h, obj))
    if unknown:
        log.warning("repo=%s file detail: %d version(s) could not be measured", repo, unknown)
        log.debug("repo=%s file detail path %s", repo, full_path)
    kept_chunks = {c for o in kept_objs for c in (obj_chunks.get(o) or ())}
    union_chunks = set(holder_refs)

    refs = _refcount_for(repo, union_chunks)  # targeted (fast); None => content_refs not built yet
    if refs is None:
        refs = _refcount_map(repo)            # cold-start fallback (full map / rebuild)

    objrefs = _objrefs_for(repo, union_chunks)

    def freed(c: str) -> bool:
        return c not in kept_chunks and _freed(c, refs, objrefs, len(holder_refs.get(c, ())),
                                               len(pair_refs.get(c, ())))

    return SimpleNamespace(
        versions=versions, holders=holders, objs_by_holder=objs_by_holder, all_objs=all_objs,
        oid_of=oid_of, kept_objs=kept_objs, obj_chunks=obj_chunks, kept_chunks=kept_chunks,
        holder_refs=holder_refs, pair_refs=pair_refs, union_chunks=union_chunks, refs=refs,
        objrefs=objrefs, freed=freed, sizes=_sizes_for(repo, union_chunks))


def file_detail(repo: str, source: str, full_path: str, others_cap: int = 500) -> dict:
    """Accurate per-file footprint: logical/physical/freeable + dedup chunk breakdown +
    other paths sharing the same content object.

    The same path held by several sources on one machine (nested backups such as a drive and a
    folder on it) is ONE file: ``freeable`` prices removing it, every version, from every one of
    those holders - the delete panel's default scope. Identical content at OTHER paths keeps its
    bytes. A piece is freed when every source and every (source, object) pair using it belongs to
    this file's versions in its holders, and no other path in the repo holds that object."""
    st = _price_file(repo, source, full_path)
    if st is None:
        return {"found": False}
    versions, holders, objs_by_holder = st.versions, st.holders, st.objs_by_holder
    oid_of, kept_objs, obj_chunks, refs = st.oid_of, st.kept_objs, st.obj_chunks, st.refs
    union_chunks, freed, sizes = st.union_chunks, st.freed, st.sizes
    latest_obj = versions[0][3]
    latest_chunks = obj_chunks.get(latest_obj) or set()
    physical_latest = sum(sizes.get(c, 0) for c in latest_chunks)
    physical_total = sum(sizes.get(c, 0) for c in union_chunks)
    freeable = sum(sizes.get(c, 0) for c in union_chunks if freed(c))
    shared = physical_total - freeable

    chunk_rows = []
    # Largest first, ties by id: storage_breakdown numbers the same 200 pieces in this order.
    for c in sorted(latest_chunks, key=lambda x: (-sizes.get(x, 0), x))[:200]:
        chunk_rows.append({
            "cid": c, "packed": sizes.get(c, 0), "refs": refs.get(c, 0), "exclusive": freed(c),
        })

    # Every location of the latest content; the same path in a holder is this file, not a copy.
    rows: list = []
    n_files = n_src = None
    latest_cid = oid_of.get(latest_obj) if latest_obj else None
    if latest_cid is not None and latest_obj not in kept_objs:
        rows = [(h, full_path) for h, objs in objs_by_holder.items() if latest_obj in objs]
    elif latest_cid is not None:
        cur = read_cursor(repo)
        try:
            rows = cur.execute(
                "SELECT DISTINCT s.source, p.full_path FROM filev f "
                "JOIN pathx p ON p.path_id = f.path_id "
                "JOIN src s ON s.src_id = p.src_id "
                "WHERE f.cid_id=? LIMIT ?",
                [latest_cid, others_cap],
            ).fetchall()
            # The list is capped; the counts are not (every empty file shares one content).
            n_files, n_src = cur.execute(
                f"SELECT count(DISTINCT ({_HOST_SQL}, p.full_path)), count(DISTINCT p.src_id) FROM filev f "
                "JOIN pathx p ON p.path_id = f.path_id JOIN src s ON s.src_id = p.src_id WHERE f.cid_id=?",
                [latest_cid]).fetchone()
        finally:
            cur.close()
    holder_set = set(holders)
    others = [(s, p) for s, p in rows if not (p == full_path and s in holder_set)]
    if n_files is None:
        n_files = len({_file_key(s, p) for s, p in others} | {_file_key(source, full_path)})
        n_src = len({source} | {s for s, _ in rows})

    # What an exact-content delete of this file frees when it reaches every copy: every version's
    # object removed from every source holding it. Only asked when copies exist.
    n_dup = n_src
    reclaim_everywhere = (price_objects(repo, st.all_objs)["freeable"] if others else freeable)

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
        # distinct files holding it: one per (computer, path), this one included
        "instances": int(n_files),
        "reclaim_everywhere": reclaim_everywhere,  # bytes freed if purged from every location
    }


def _register_ids(cur, ids) -> str:
    """Register a set of integer ids as a one-column view for a hash join (trap 29)."""
    import uuid

    import pyarrow as pa
    name = f"_ids_{uuid.uuid4().hex[:8]}"
    cur.register(name, pa.table({"c": pa.array(sorted(set(ids)), pa.int64())}))
    return name


def _price_pairs(repo: str, cur, pairs: set[tuple[str, str]]) -> tuple[int, int]:
    """(physical, freeable) of deleting every version of every (source, path) in ``pairs``. Same rule
    as the file panel: an object also held outside the set keeps its pieces, and a piece is freed
    only when every source and (source, object) pair using it is inside the set."""
    name = _register_files(cur, pairs)
    if name is None:
        return 0, 0
    try:
        sel = (f"SELECT p.src_id, p.path_id FROM {name} w JOIN pathx p "
               f"ON p.src_id = w.src_id AND p.full_path = w.full_path")
        priced = _price(cur, _objs_sql(sel, True), kept=_kept_sql(f"SELECT path_id FROM ({sel})"))
    finally:
        _unregister(cur, name)
    return priced["physical"], priced["freeable"]


def _file_key(source: str, full_path: str) -> tuple[str, str]:
    """One file = one (computer, path); a path under nested sources is still one file."""
    return pathrules.host(source), full_path


def storage_breakdown(repo: str, source: str, full_path: str, limit: int = 10,
                      together_cap: int = 50) -> dict:
    """Where a file's all-versions storage goes if it is deleted: what comes back, and for the rest
    the identical copies and other files that keep it stored. ``together`` prices deleting the file
    together with those files (every version), when there are at most ``together_cap`` of them."""
    st = _price_file(repo, source, full_path)
    if st is None:
        return {"found": False}
    sizes = st.sizes
    kept = [c for c in st.union_chunks if not st.freed(c)]
    shared = [c for c in kept if c not in st.kept_chunks]
    own_pairs = {(h, full_path) for h in st.holders}
    mine = set(st.oid_of.values())
    copy_paths: set[tuple] = set()
    by_path: dict[tuple, set[str]] = {}
    cur = read_cursor(repo)
    try:
        kept_ids = [st.oid_of[o] for o in st.kept_objs if o in st.oid_of]
        if kept_ids:
            name = _register_ids(cur, kept_ids)
            try:
                copy_paths = {tuple(r) for r in cur.execute(
                    f"SELECT DISTINCT s.source, p.full_path FROM filev f JOIN {name} w ON w.c = f.cid_id "
                    "JOIN pathx p ON p.path_id = f.path_id JOIN src s ON s.src_id = p.src_id").fetchall()}
            finally:
                cur.unregister(name)
            copy_paths -= own_pairs
        piece_id = resolve.cid_ids(cur, shared) if shared else {}
        if piece_id:
            piece_of = {v: k for k, v in piece_id.items()}
            pieces = _register_ids(cur, piece_id.values())
            try:
                # Other files using these pieces: split objects listing them as parts, and plain
                # objects that are the piece itself.
                users: dict[int, set[str]] = {}
                for obj, part in cur.execute(
                        f"SELECT t.iobj_cid, t.part_cid FROM tpart t JOIN {pieces} w ON w.c = t.part_cid").fetchall():
                    if obj not in mine:
                        users.setdefault(obj, set()).add(piece_of[part])
                for (obj,) in cur.execute(
                        f"SELECT DISTINCT f.cid_id FROM filev f JOIN {pieces} w ON w.c = f.cid_id").fetchall():
                    if obj not in mine:
                        users.setdefault(obj, set()).add(piece_of[obj])
            finally:
                cur.unregister(pieces)
            if users:
                objs = _register_ids(cur, users)
                try:
                    for obj, src, fp in cur.execute(
                            f"SELECT DISTINCT f.cid_id, s.source, p.full_path FROM filev f "
                            f"JOIN {objs} w ON w.c = f.cid_id JOIN pathx p ON p.path_id = f.path_id "
                            "JOIN src s ON s.src_id = p.src_id").fetchall():
                        if (src, fp) not in own_pairs:
                            by_path.setdefault((src, fp), set()).update(users[obj])
                finally:
                    cur.unregister(objs)
        together = None
        others = set(by_path) | copy_paths
        if others and len(others) <= together_cap:
            phys, frees = _price_pairs(repo, cur, own_pairs | others)
            together = {"files": len({_file_key(s_, fp) for s_, fp in others}), "physical": phys, "frees": frees}
    finally:
        cur.close()

    def nbytes(chunks) -> int:
        return sum(sizes.get(c, 0) for c in chunks)

    matched = set().union(*by_path.values()) if by_path else set()
    # Who else uses each shared piece in the panel's chunk list (the latest version's 200 largest).
    latest = st.obj_chunks.get(st.versions[0][3]) or set()
    order = sorted(latest, key=lambda c: (-sizes.get(c, 0), c))[:200]   # file_detail's chunk order
    listed, rank = set(order), {c: i + 1 for i, c in enumerate(order)}
    pieces: dict[str, dict] = {}
    for (_s, fp), cs in sorted(by_path.items(), key=lambda kv: kv[0][1]):
        for c in cs & listed:
            names = pieces.setdefault(c, {"files": [], "n": rank[c]})["files"]
            if fp not in names:
                names.append(fp)
    for c in listed & st.kept_chunks:
        pieces.setdefault(c, {"files": [], "n": rank[c]})["copy"] = True
    # One entry per file: nested sources hold the same (machine, path) once each.
    merged: dict[tuple, dict] = {}
    for (src, fp), cs in sorted(by_path.items()):
        row = merged.setdefault(_file_key(src, fp), {"source": src, "full_path": fp, "chunks": set()})
        row["chunks"] |= cs
    files = sorted(({"source": r["source"], "full_path": r["full_path"], "bytes": nbytes(r["chunks"])}
                    for r in merged.values()), key=lambda r: (-r["bytes"], r["full_path"], r["source"]))
    copies = {}
    for src, fp in sorted(copy_paths):
        copies.setdefault(_file_key(src, fp), {"source": src, "full_path": fp})
    copy_list = sorted(copies.values(), key=lambda r: (r["full_path"], r["source"]))
    return {
        "found": True,
        "all_versions": nbytes(st.union_chunks),
        "frees": nbytes(c for c in st.union_chunks if st.freed(c)),
        "copies": {"bytes": nbytes(c for c in kept if c in st.kept_chunks), "count": len(copy_list),
                   "paths": copy_list[:limit]},
        "shared": {"bytes": nbytes(matched & set(shared)), "count": len(files), "files": files[:limit]},
        "unmatched": nbytes(set(shared) - matched),   # pieces whose other user the index has not mapped
        "together": together,
        "pieces": pieces,
    }
