"""Per-snapshot analysis: churn (new physical) + reclaimable (freed if this snapshot is
deleted), persisted to DuckDB so it survives restarts and is instant to re-view.

Churn = a forward incremental walk (chunks reachable via directories new vs older snapshots).
Reclaimable = chunks that exist ONLY in this snapshot: new in BOTH the forward pass (not in any
older snapshot) AND the backward pass (not in any newer), and not present in any OTHER source
(global source refcount == 1). Deleting the snapshot frees exactly those.
"""

from __future__ import annotations

import collections
import hashlib
import threading

from ..core.cache import cache_db, get_children
from ..core.sizemap import load_sizemap
from ..core.sources import resolve_source
from ..core.walk import walk, walk_new
from .physical import _chunks_for_obj, _refcount_map
from .schema import get_db, read_cursor

# (repo, snapshot_id) -> reclaimable files. LRU-bounded: these lists can be large and were
# previously never evicted (a slow unbounded leak as snapshots were viewed over a long session).
_RECL_FILES_CACHE: collections.OrderedDict[tuple, list] = collections.OrderedDict()
_RECL_FILES_CACHE_MAX = 200
_RECL_FILES_LOCK = threading.Lock()  # guards the LRU under the sync-handler threadpool (check->get->
# move_to_end->popitem must not interleave across requests -> KeyError / unexpected eviction)


def _recl_cache_put(ck: tuple, val: list) -> None:
    with _RECL_FILES_LOCK:
        _RECL_FILES_CACHE[ck] = val
        _RECL_FILES_CACHE.move_to_end(ck)
        while len(_RECL_FILES_CACHE) > _RECL_FILES_CACHE_MAX:
            _RECL_FILES_CACHE.popitem(last=False)


def _recl_cache_get(ck: tuple) -> list | None:
    with _RECL_FILES_LOCK:
        val = _RECL_FILES_CACHE.get(ck)
        if val is not None:
            _RECL_FILES_CACHE.move_to_end(ck)
        return val


_MAX_HEAVY_PER_CYCLE = 100  # cap heavy (physical+reclaim) source recomputes per cycle (a soft guard;
# the crash it once mitigated is fixed by the drop-index rewrite, so this is set high to not delay
# population - effectively "all sources per cycle" for the current ~72-source repos)


def reclaimable_files(repo: str, source: str, snapshot_id: str, limit: int = 200) -> list[dict]:
    """ACCURATE 'what is freed if this snapshot is deleted': files whose content chunks are
    exclusive to this snapshot (in no other snapshot of any source). Uses the SAME exclusive-chunk
    set as the reclaimable byte total, then maps it to files via a full tree walk. On-demand +
    cached (expensive: walks the whole source once)."""
    ck = (repo, snapshot_id)
    cached = _recl_cache_get(ck)
    if cached is not None:
        return cached[:limit]
    key, srcs = resolve_source(repo, source=source)
    snaps = sorted(srcs[key], key=lambda s: s.get("startTime", ""))
    target = next((s for s in snaps if s["id"] == snapshot_id), None)
    troot = target and target.get("rootEntry", {}).get("obj")
    if not troot:
        return []
    db = cache_db(repo)
    smap = load_sizemap(repo)
    target_chunks = walk_new(repo, db, [troot], set())
    others: set[str] = set()
    seen: set[str] = set()
    for s in snaps:
        if s["id"] == snapshot_id:
            continue
        r = s.get("rootEntry", {}).get("obj")
        if r:
            others |= walk_new(repo, db, [r], seen)
    refs = _refcount_map(repo)
    exclusive = {c for c in (target_chunks - others) if refs.get(c, 0) <= 1}
    if not exclusive:
        _recl_cache_put(ck, [])
        return []
    root_path = key.split(":", 1)[1] if ":" in key else key
    files: list[dict] = []

    def visitor(parent: str, entry: dict) -> None:
        if entry.get("type") != "f":
            return
        chunks = _chunks_for_obj(repo, db, entry.get("obj", ""))
        excl = chunks & exclusive
        if excl:
            rel = (parent + "\\" + entry.get("name", "")).lstrip("\\")
            files.append({"full_path": root_path.rstrip("\\") + "\\" + rel,
                          "size": sum(smap.get(c, 0) for c in excl)})

    walk(repo, [troot], db, visitor)
    files.sort(key=lambda f: f["size"], reverse=True)
    _recl_cache_put(ck, files)
    return files[:limit]


def _obj_physical(repo: str, db, obj: str, smap, icache: dict) -> int:
    """Packed bytes backing one file object (indirect/split objects expanded + cached)."""
    if not obj:
        return 0
    if obj.startswith("I"):
        p = icache.get(obj)
        if p is None:
            p = sum(smap.get(c, 0) for c in _chunks_for_obj(repo, db, obj))
            icache[obj] = p
        return p
    return smap.get(obj, 0)


def _subtree_physical(repo: str, db, oid: str, smap, memo: dict, icache: dict) -> int:
    """Absolute physical of a directory subtree = sum of its files' packed bytes, MEMOIZED per
    directory object so subtrees shared across snapshots (the dedup norm) are computed once.
    Additive: intra-snapshot duplicate content is counted per file (a minor over-count) - this is
    the snapshot's standalone deduped footprint, the basis for its accumulation factor."""
    if not oid:
        return 0
    cached = memo.get(oid)
    if cached is not None:
        return cached
    total = 0
    for e in get_children(repo, db, [oid]).get(oid, []):
        t = e.get("type")
        obj = e.get("obj", "")
        if t == "d":
            total += _subtree_physical(repo, db, obj, smap, memo, icache)
        elif t == "f":
            total += _obj_physical(repo, db, obj, smap, icache)
    memo[oid] = total
    return total


def _existing_analysis(repo: str, source: str) -> dict:
    """Current persisted (physical, reclaimable) per snapshot - lets the light churn pass refresh
    churn WITHOUT wiping the heavy fields (physical/reclaimable) the slow pass computed."""
    cur = read_cursor(repo)
    try:
        rows = cur.execute(
            "SELECT snapshot_id, physical, reclaimable FROM snapshot_analysis WHERE repo=? AND source=?",
            [repo, source],
        ).fetchall()
    finally:
        cur.close()
    return {sid: (phys, rec) for sid, phys, rec in rows}


def cached_reclaimable_files(repo: str, snapshot_id: str) -> list | None:
    """Already-computed reclaimable file list for a snapshot, or None (never triggers a walk).
    Used by the all-sources Tree 'reclaimable' view (shows only what's cached, per the cheap option)."""
    return _recl_cache_get((repo, snapshot_id))


def _snaphash(snaps: list[dict]) -> str:
    digest = hashlib.md5()
    for snap in snaps:
        digest.update(snap.get("id", "").encode())
    return digest.hexdigest()


def _persist(repo: str, source: str, rows: list[dict], snaphash: str) -> None:
    con = get_db(repo).cursor()
    try:
        con.execute("DELETE FROM snapshot_analysis WHERE repo=? AND source=?", [repo, source])
        for r in rows:
            rec = r["reclaimable"]
            con.execute(
                "INSERT INTO snapshot_analysis(repo, source, snapshot_id, churn, physical, reclaimable, snaphash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",  # plain INSERT: _persist DELETEs the source's rows first; no PK now
                [repo, source, r["id"], int(r["churn"]), int(r.get("physical") or 0),
                 None if rec is None else int(rec), snaphash],
            )
        con.commit()
    finally:
        con.close()


def compute_source(repo: str, source: str, with_reclaim: bool = True) -> dict:
    """Walk a source's snapshots and compute per-snapshot churn. ``with_reclaim`` ALSO computes the
    heavy fields - absolute per-snapshot physical (memoized subtree walk) + reclaimable; the light
    (churn-only) pass PRESERVES the previously-computed physical/reclaimable rather than wiping
    them. Persisted to DuckDB."""
    key, srcs = resolve_source(repo, source=source)
    snaps = sorted(srcs[key], key=lambda s: s.get("startTime", ""))  # oldest -> newest
    db = cache_db(repo)
    smap = load_sizemap(repo)

    # Forward pass: churn (new physical) + remember each snapshot's forward-new chunk set.
    seen_dirs: set[str] = set()
    seen_content: set[str] = set()
    fwd_new: dict[str, set[str]] = {}
    churn: dict[str, int] = {}
    for snap in snaps:
        root = snap.get("rootEntry", {}).get("obj")
        found = walk_new(repo, db, [root], seen_dirs) if root else set()
        new_here = found - seen_content
        fwd_new[snap["id"]] = new_here
        churn[snap["id"]] = sum(smap.get(c, 0) for c in new_here)
        seen_content |= found

    physical: dict[str, int] = {sid: 0 for sid in churn}      # 0 = not yet computed
    reclaim: dict[str, int | None] = {sid: None for sid in churn}  # None = not computed (shows "—")

    if with_reclaim:
        # Heavy pass: absolute per-snapshot physical (memoized across shared subtrees) ...
        phys_memo: dict[str, int] = {}
        icache: dict[str, int] = {}
        for snap in snaps:
            root = snap.get("rootEntry", {}).get("obj")
            physical[snap["id"]] = _subtree_physical(repo, db, root, smap, phys_memo, icache) if root else 0
        # ... + reclaimable (backward pass: chunks ONLY in this snapshot, source-exclusive).
        refs = _refcount_map(repo)  # global per-source refcount: ==1 means only this source has it
        seen_dirs_b: set[str] = set()
        seen_content_b: set[str] = set()
        for snap in reversed(snaps):  # newest -> oldest
            root = snap.get("rootEntry", {}).get("obj")
            found = walk_new(repo, db, [root], seen_dirs_b) if root else set()
            new_bwd = found - seen_content_b
            exclusive = fwd_new[snap["id"]] & new_bwd  # only in this snapshot (no older, no newer)
            reclaim[snap["id"]] = sum(smap.get(c, 0) for c in exclusive if refs.get(c, 0) <= 1)
            seen_content_b |= found
    else:
        # Light pass: keep the existing heavy fields; brand-new snapshots get 0/None so
        # phys_complete/recl_complete flip false -> the slow pass refills just those.
        existing = _existing_analysis(repo, key)
        for sid in churn:
            phys, rec = existing.get(sid, (0, None))
            physical[sid] = int(phys or 0)
            reclaim[sid] = None if rec is None else int(rec)

    rows = [{"id": sid, "churn": churn[sid], "physical": physical[sid], "reclaimable": reclaim[sid]}
            for sid in churn]
    snaphash = _snaphash(snaps)
    _persist(repo, key, rows, snaphash)
    return {"source": key, "rows": rows, "snaphash": snaphash, "with_reclaim": with_reclaim}


def read_analysis(repo: str, source: str, compute_if_stale: bool = False, with_reclaim: bool = True) -> dict:
    """Return persisted per-snapshot churn/physical/reclaimable; (re)compute if missing/stale.
    ``recl_complete`` / ``phys_complete`` tell the background pre-warm whether the heavy fields are
    populated for EVERY snapshot (separate from churn-freshness, so a churn-only refresh that
    leaves a new snapshot's reclaimable empty still triggers the heavy recompute)."""
    key, srcs = resolve_source(repo, source=source)
    want = _snaphash(srcs[key])
    cur = read_cursor(repo)
    try:
        rows = cur.execute(
            "SELECT snapshot_id, churn, physical, reclaimable, snaphash FROM snapshot_analysis WHERE repo=? AND source=?",
            [repo, key],
        ).fetchall()
    finally:
        cur.close()
    fresh = bool(rows) and all(r[4] == want for r in rows)
    if compute_if_stale and not fresh:
        rows = [(r["id"], r["churn"], r["physical"], r["reclaimable"], want)
                for r in compute_source(repo, key, with_reclaim=with_reclaim)["rows"]]
        fresh = True
    recl_complete = bool(rows) and all(r[3] is not None for r in rows)
    phys_complete = bool(rows) and all(r[2] for r in rows)
    return {
        "source": key, "fresh": fresh, "recl_complete": recl_complete, "phys_complete": phys_complete,
        # None preserved -> "—"
        "by_id": {r[0]: {"churn": r[1], "physical": r[2], "reclaimable": r[3]} for r in rows},
    }


def prewarm(repo: str, with_reclaim: bool = False) -> None:
    """Background: keep per-snapshot analysis warm. churn-only (``with_reclaim=False``) recomputes
    only when the snapshot set changed; the heavy pass (``with_reclaim=True``) ALSO recomputes when
    physical/reclaimable are missing for any snapshot - so the slow pass actually refills the heavy
    fields instead of being skipped because churn looks fresh (the old reclaimable-blank bug)."""
    from ..core.sources import sources_index
    heavy = 0
    for key in sorted(sources_index(repo)):
        try:
            info = read_analysis(repo, key)
            need = (not info["fresh"]) or (with_reclaim and not (info["recl_complete"] and info["phys_complete"]))
            if not need:
                continue
            # Throttle the HEAVY (with_reclaim) recompute to a few sources per cycle so the
            # first-time populate doesn't burst-DELETE the indexed maps and corrupt DuckDB (the
            # repo1 fatal-index incident). The rest catch up on subsequent cycles.
            if with_reclaim:
                if heavy >= _MAX_HEAVY_PER_CYCLE:
                    continue
                heavy += 1
            compute_source(repo, key, with_reclaim=with_reclaim)
        except Exception:
            continue
