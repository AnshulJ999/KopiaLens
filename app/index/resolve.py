"""Natural-key -> integer-id resolution at the storage boundary (M2).

The rule every M2 rewrite follows: resolve INPUTS by
lookup - turn each natural key into its integer with one small query, then run the real query
on ids. Resolve OUTPUTS by joining the dictionary against the result set, never the whole
table. This module is the input side; no integer id ever leaves the storage layer through it.

Caches are scoped to one Generation (a WeakKeyDictionary on the handle a cursor reads from):
ids are only meaningful inside a generation lineage, so a cache surviving a publish would
answer with stale ids. Cursors without a generation (raw connections, test fakes) resolve
uncached every call.

Dictionary sizing, measured repo1/repo2: `src` (84/100 rows) and `snap` (6,629/3,232) load
whole per generation; `dir` (658k) and `pathx` (5.3M) lookups are LRU-bounded, not preloaded;
`cid` is millions of rows and is never cached.
"""

from __future__ import annotations

import collections
import threading
import uuid
import weakref

import pyarrow as pa

from ..core.applog import get_logger

log = get_logger("resolve")

_LOCK = threading.Lock()
_STATE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

_DIR_LRU_MAX = 8192
_PATH_LRU_MAX = 8192


def _state(cur):
    """The per-generation cache state, or None when the cursor carries no generation."""
    gen = getattr(cur, "generation", None)
    if gen is None:
        return None
    st = _STATE.get(gen)
    if st is None:
        st = {"src": None, "src_rev": None, "snap": None,
              "dir": collections.OrderedDict(), "path": collections.OrderedDict()}
        with _LOCK:
            if _STATE.get(gen) is None:
                _STATE[gen] = st
    return _STATE.get(gen)


def _lru_put(lru: collections.OrderedDict, key, val, cap: int) -> None:
    with _LOCK:
        lru[key] = val
        lru.move_to_end(key)
        while len(lru) > cap:
            lru.popitem(last=False)


# ---- source ----

def src_map(cur) -> dict[str, int]:
    """The whole source dictionary, loaded once per generation."""
    st = _state(cur)
    if st is not None and st["src"] is not None:
        return st["src"]
    m = dict(cur.execute("SELECT source, src_id FROM src").fetchall())
    if st is not None:
        with _LOCK:
            st["src"] = m
    return m


def src_id(cur, source: str) -> int | None:
    """A kopia source key's id. A miss is a warning: source keys come out of the index itself,
    so not finding one means the index and the listing disagree."""
    sid = src_map(cur).get(source)
    if sid is None:
        log.warning("resolve: source %r not in src", source)
    return sid


def source_of(cur, src_id: int) -> str | None:
    """Reverse lookup for identity rules and output naming."""
    st = _state(cur)
    if st is not None and st["src_rev"] is not None:
        return st["src_rev"].get(src_id)
    m = src_map(cur)
    rev = {v: k for k, v in m.items()}
    if st is not None:
        with _LOCK:
            st["src_rev"] = rev
    return rev.get(src_id)


# ---- snapshot ----

def snap_id(cur, snapshot_id: str) -> int | None:
    """A snapshot id's integer. Same miss semantics as src_id."""
    st = _state(cur)
    if st is not None and st["snap"] is not None:
        return st["snap"].get(snapshot_id)
    m = dict(cur.execute("SELECT snapshot_id, snap_id FROM snap").fetchall())
    if st is not None:
        with _LOCK:
            st["snap"] = m
    sid = m.get(snapshot_id)
    if sid is None:
        log.warning("resolve: snapshot %r not in snap", snapshot_id)
    return sid


# ---- path / folder ----

def path_id(cur, src_id: int, full_path: str) -> int | None:
    """A path's row id inside one source, matched on the RAW string exactly - the way every
    pre-M2 query matched it. Case-folding here made two distinct files differing only by case
    resolve to an arbitrary one of them (325 colliding keys on repo1; batch_detail feeds
    reclaim, which deletes). ONE caller at a time. At Arrow-join volumes (a set of pairs, not
    one path) use ``path_ids`` - a single lookup scans 5.3M rows, so a per-pair loop re-scans
    per pair."""
    return _path_lookup(cur, src_id, full_path)


def path_ids(cur, pairs) -> dict[tuple, int]:
    """Batched ``path_id``: one scan per source group for the whole set, not one scan per pair.

    ``physical.batch_detail`` passes hundreds of (source, path) pairs; per-pair lookups there
    re-scanned pathx hundreds of times (the KL47 regression shape - correct answers, minutes
    of wall time, invisible to a value-based gate). Matching is RAW-EXACT within each source
    group - one registered-join per group rather than a single IN list. Unresolvable pairs are
    absent from the result, never errors. ``dir_id`` carries the same caution: batch, never
    loop, if a volume caller ever appears."""
    out: dict[tuple, int] = {}
    groups: dict[int, list[str]] = {}
    for sid, fp in pairs or []:
        if sid is not None and fp:
            groups.setdefault(sid, []).append(fp)
    st = _state(cur)
    for sid, fps in groups.items():
        if source_of(cur, sid) is None:
            log.warning("resolve: src_id %r not in src", sid)
            continue
        wanted: list[str] = []
        for fp in fps:
            key = (sid, fp)
            hit = st["path"].get(key) if st is not None else None
            if hit is not None:
                out[(sid, fp)] = hit
            else:
                wanted.append(fp)
        uniq = list(dict.fromkeys(wanted))
        if not uniq:
            continue
        name = f"_resolve_paths_{uuid.uuid4().hex[:12]}"
        cur.register(name, pa.table({"p": uniq}))
        try:
            rows = cur.execute(
                f"SELECT r.p, px.path_id FROM pathx px JOIN {name} r "
                f"ON px.full_path = r.p WHERE px.src_id = ?",
                [sid]).fetchall()
        finally:
            cur.unregister(name)
        found = dict(rows)
        for fp in uniq:
            pid = found.get(fp)
            if pid is None:
                log.debug("resolve: path %r not in src_id %s", fp, sid)
                continue
            out[(sid, fp)] = pid
            if st is not None:
                _lru_put(st["path"], (sid, fp), pid, _PATH_LRU_MAX)
    return out


def _path_lookup(cur, src_id: int, full_path: str) -> int | None:
    key = (src_id, full_path)
    st = _state(cur)
    if st is not None:
        hit = st["path"].get(key)
        if hit is not None:
            with _LOCK:
                st["path"].move_to_end(key)
            return hit
    row = cur.execute(
        "SELECT path_id FROM pathx WHERE src_id = ? AND full_path = ?",
        [src_id, full_path]).fetchone()
    pid = row[0] if row else None
    if pid is None:
        log.debug("resolve: path %r not in src_id %s", full_path, src_id)
    elif st is not None:
        _lru_put(st["path"], key, pid, _PATH_LRU_MAX)
    return pid


def dir_id(cur, src_id: int, dir_path: str) -> int | None:
    """A folder's row id inside one source, matched on the RAW string exactly - same rule as
    ``path_id``; folding case-variants would merge distinct directories."""
    key = (src_id, dir_path)
    st = _state(cur)
    if st is not None:
        hit = st["dir"].get(key)
        if hit is not None:
            with _LOCK:
                st["dir"].move_to_end(key)
            return hit
    row = cur.execute(
        "SELECT dir_id FROM dir WHERE src_id = ? AND dir_path = ?",
        [src_id, dir_path]).fetchone()
    did = row[0] if row else None
    if did is None:
        log.debug("resolve: dir %r not in src_id %s", dir_path, src_id)
    elif st is not None:
        _lru_put(st["dir"], key, did, _DIR_LRU_MAX)
    return did


# ---- content ----

def cid_id(cur, content_id: str) -> int | None:
    """One content id's integer. Never cached - the dictionary is millions of rows and the
    callers that need volumes use ``cid_ids``."""
    row = cur.execute("SELECT cid_id FROM cid WHERE content_id = ?", [content_id]).fetchone()
    cid = row[0] if row else None
    if cid is None:
        log.debug("resolve: content %r not in cid", content_id)
    return cid


def cid_ids(cur, content_ids: list[str]) -> dict[str, int]:
    """Batched content-id resolution for IN-lists (chunked at 900 like every other IN batch).
    Unresolvable ids are simply absent - callers treat that as an empty result, never an error."""
    out: dict[str, int] = {}
    ids = [c for c in dict.fromkeys(content_ids or []) if c]
    for i in range(0, len(ids), 900):
        grp = ids[i:i + 900]
        q = "SELECT content_id, cid_id FROM cid WHERE content_id IN (%s)" % ",".join("?" * len(grp))
        for c, cid in cur.execute(q, grp).fetchall():
            out[c] = cid
    return out


def content_ids(cur, cid_ids: list[int]) -> dict[int, str]:
    """Reverse map for result sets already carrying cid_id values - the output side of the
    rule, for callers whose public shape is keyed by content-id string."""
    out: dict[int, str] = {}
    ids = [c for c in dict.fromkeys(cid_ids or []) if c is not None]
    for i in range(0, len(ids), 900):
        grp = ids[i:i + 900]
        q = "SELECT cid_id, content_id FROM cid WHERE cid_id IN (%s)" % ",".join("?" * len(grp))
        for cid, c in cur.execute(q, grp).fetchall():
            out[cid] = c
    return out
