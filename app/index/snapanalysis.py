"""Per-snapshot analysis: churn (new physical) + reclaimable (freed if this snapshot is
deleted), persisted to DuckDB so it survives restarts and is instant to re-view.

Churn = a forward incremental walk (chunks reachable via directories new vs older snapshots).
Reclaimable = chunks that exist ONLY in this snapshot: new in BOTH the forward pass (not in any
older snapshot) AND the backward pass (not in any newer), and not present in any OTHER source
(global source refcount == 1). Deleting the snapshot frees exactly those.
"""

from __future__ import annotations

import collections
import contextlib
from datetime import datetime
import hashlib
import threading
import time

from ..core.applog import get_logger
from ..core.sources import resolve_source
from . import entries, layer2
from .physical import _chunks_for_objs, _refcount_map, _size_map
from .store import read_cursor

log = get_logger("analysis")

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
    smap = _size_map(repo)
    roots = [s.get("rootEntry", {}).get("obj") for s in snaps if s.get("rootEntry", {}).get("obj")]
    target_chunks = layer2.reach_content_ids(repo, [troot])
    others = layer2.reach_content_ids(repo, [r for r in roots if r != troot])
    refs = _refcount_map(repo)
    exclusive = {c for c in (target_chunks - others) if refs.get(c, 0) <= 1}
    if not exclusive:
        _recl_cache_put(ck, [])
        return []
    root_path = key.split(":", 1)[1] if ":" in key else key
    files: list[dict] = []
    unknown = 0

    # ONE cursor for the whole walk. `children_of` opens its own per call otherwise, and outside a
    # request's pin scope that also re-resolves the current generation (a manifest stat) for every
    # folder - on a source with tens of thousands of folders that is the dominant cost, not the
    # query. Threading the cursor is what keeps this a bulk read rather than a per-folder one.
    def visit(oid: str, parent: str, cur) -> None:
        nonlocal unknown
        kids = entries.children_of(repo, [oid], cursor=cur).get(oid, [])
        # One batched chunk lookup per FOLDER. Per file it was a query each, and this walk covers
        # every folder in the source.
        chunk_of = _chunks_for_objs(
            repo, [e.get("obj", "") for e in kids if e.get("type") == "f"], cursor=cur)
        for entry in kids:
            name = entry.get("name", "")
            rel = (parent + "\\" + name).lstrip("\\")
            if entry.get("type") == "d" and entry.get("obj"):
                visit(entry["obj"], rel, cur)
            elif entry.get("type") == "f":
                chunks = chunk_of.get(entry.get("obj", ""))
                if chunks is None:
                    # Not in the tree: unknown, NOT "holds nothing". Coercing it to an empty set
                    # would quietly drop a large split file out of the reclaimable list.
                    unknown += 1
                    continue
                excl = chunks & exclusive
                if excl:
                    files.append({"full_path": root_path.rstrip("\\") + "\\" + rel,
                                  "size": sum(smap.get(c, 0) for c in excl)})

    cur = read_cursor(repo)
    try:
        visit(troot, "", cur)
    finally:
        cur.close()
    if unknown:
        log.warning("repo=%s snapshot %s: %d file object(s) are not in the tree yet and are absent "
                    "from the reclaimable list until the next index", repo, snapshot_id, unknown)
    files.sort(key=lambda f: f["size"], reverse=True)
    _recl_cache_put(ck, files)
    return files[:limit]


def _obj_physical(repo: str, obj: str, smap, icache: dict, chunk_of: dict | None = None) -> int:
    """Packed bytes backing one file object (indirect/split objects expanded + cached).

    ``chunk_of`` is the caller's batched lookup for the folder being walked; without it this would
    be one query per file. An object the tree has not reached contributes 0 and is NOT cached, so
    the next pass - which runs after the tree sync - prices it properly. Caching the 0 would make
    one bad moment stick for the life of the process.
    """
    if not obj:
        return 0
    if obj.startswith("I"):
        p = icache.get(obj)
        if p is None:
            chunks = (chunk_of or {}).get(obj)
            if chunks is None:
                log.warning("repo=%s split object %s is not in the tree yet - counted as 0 bytes "
                            "for this pass, priced properly once the index catches up", repo, obj)
                return 0
            p = sum(smap.get(c, 0) for c in chunks)
            icache[obj] = p
        return p
    return smap.get(obj, 0)


def _subtree_physical(repo: str, oid: str, smap, memo: dict, icache: dict, cursor=None) -> int:
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
    # The cursor is threaded down the recursion: without it every folder opens its own, and outside
    # a request's pin scope each of those also re-resolves the current generation.
    kids = entries.children_of(repo, [oid], cursor=cursor).get(oid, [])
    # One batched chunk lookup per folder, for the split objects this folder's files point at.
    chunk_of = _chunks_for_objs(
        repo, [e.get("obj", "") for e in kids
               if e.get("type") == "f" and str(e.get("obj", "")).startswith("I")
               and e.get("obj") not in icache],
        cursor=cursor)
    for e in kids:
        t = e.get("type")
        obj = e.get("obj", "")
        if t == "d":
            total += _subtree_physical(repo, obj, smap, memo, icache, cursor)
        elif t == "f":
            total += _obj_physical(repo, obj, smap, icache, chunk_of)
    memo[oid] = total
    return total


def _existing_analysis(repo: str, source: str) -> dict:
    """Current persisted (physical, reclaimable) per snapshot - lets the light churn pass refresh
    churn WITHOUT wiping the heavy fields (physical/reclaimable) the slow pass computed."""
    cur = read_cursor(repo)
    try:
        # `sanalysis` is the raw artifact (the old view was a pure passthrough of it), and the
        # repo predicate is gone: a generation only ever holds its own repo's rows.
        rows = cur.execute(
            "SELECT snapshot_id, physical, reclaimable FROM sanalysis WHERE source=?",
            [source],
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


_BATCH: dict[str, dict] = {}      # repo -> {source: (rows, snaphash)} while a batch is open
_BATCH_LOCK = threading.Lock()


@contextlib.contextmanager
def batched(repo: str):
    """Collect a whole stage's analysis and publish it as ONE generation.

    Each `_persist` publishes a generation, and the analysis stage runs per source - so a 100-source
    repo published 100 generations in one pass, rewriting the manifest 100 times and forcing every
    reader to rebind its views just as often. The work is identical; only the number of publishes
    changes.

    Yields a dict whose ``saved`` is set on exit. Batching means ONE publish attempt for the whole
    pass, so a declined publish (another writer holds the repo) discards everything the block
    computed instead of just one source's worth - the caller has to be able to see that.
    """
    state: dict = {"saved": None}
    with _BATCH_LOCK:
        _BATCH.setdefault(repo, {})
    try:
        yield state
    finally:
        with _BATCH_LOCK:
            pending = _BATCH.pop(repo, {})
        state["saved"] = True if not pending else _publish_analysis(repo, pending) is not None


def _persist(repo: str, source: str, rows: list[dict], snaphash: str) -> None:
    """Republish the analysis artifact with this source's rows replaced.

    Artifacts are immutable, so there is no row-level DELETE any more: the surviving rows and the
    new ones are unioned into a fresh `sanalysis` file and published as a new generation with every
    other artifact carried forward by reference. Column types come from the current view rather
    than being restated here, so the artifact schema cannot drift between generations.

    Analysis is a cache. If another writer holds the repo, `replace_artifacts` returns None and we
    keep the computed value in memory - the next pass recomputes it.

    Inside a `batched(repo)` block this only stages the rows; the block publishes once at the end.
    """
    with _BATCH_LOCK:
        if repo in _BATCH:
            _BATCH[repo][source] = (rows, snaphash)
            return
    _publish_analysis(repo, {source: (rows, snaphash)})


def _publish_analysis(repo: str, per_source: dict) -> int | None:
    """Union the surviving rows with every staged source's rows and publish one generation.
    Returns the new generation, or None when another writer holds the repo and nothing was saved."""
    import pyarrow as pa

    from . import publish

    now = datetime.now()
    new = [(repo, source, r["id"], int(r["churn"]), int(r.get("physical") or 0),
            None if r["reclaimable"] is None else int(r["reclaimable"]), snaphash, now)
           for source, (rows, snaphash) in per_source.items()
           for r in rows]
    cols = list(zip(*new)) if new else [[]] * 8
    # Explicit uint64: plain signed ints unioned with the artifact's UBIGINT columns widen the
    # whole union to DECIMAL, and a Decimal then reaches the API as a STRING
    # ("reclaimable":"284114983") - breaking every numeric sort/add on these fields.
    u64 = pa.uint64()
    incoming = pa.table({
        "repo": list(cols[0]), "source": list(cols[1]), "snapshot_id": list(cols[2]),
        "churn": pa.array(cols[3], type=u64), "physical": pa.array(cols[4], type=u64),
        "reclaimable": pa.array(cols[5], type=u64),
        "snaphash": list(cols[6]), "computed_at": list(cols[7]),
    })
    replaced = list(per_source)
    cur = read_cursor(repo)
    try:
        cur.register("_sa_incoming", incoming)
        placeholders = ",".join("?" * len(replaced))
        # to_arrow_table(), never .arrow(): the latter returns a LAZY RecordBatchReader, and the
        # unregister below then removes the very table it reads from, so the publisher writes an
        # empty artifact with no error. Every analysis generation was 0 rows until 2026-09-07.
        merged = cur.execute(
            f"SELECT * FROM sanalysis "
            f"WHERE NOT (source IN ({placeholders})) "
            f"UNION ALL BY NAME SELECT * FROM _sa_incoming", [*replaced]).to_arrow_table()
        cur.unregister("_sa_incoming")
    finally:
        cur.close()
    # Heal generations published before the uint64 fix: the surviving side still carries the old
    # DECIMAL type, so recasting the merged result here is what makes the published artifact UBIGINT.
    for name in ("churn", "physical", "reclaimable"):
        idx = merged.schema.get_field_index(name)
        if idx >= 0 and merged.schema.field(idx).type != u64:
            merged = merged.set_column(idx, name, merged.column(idx).cast(u64))
    return publish.replace_artifacts(repo, {"sanalysis": merged})


def compute_source(repo: str, source: str, with_reclaim: bool = True) -> dict:
    """Walk a source's snapshots and compute per-snapshot churn. ``with_reclaim`` ALSO computes the
    heavy fields - absolute per-snapshot physical (memoized subtree walk) + reclaimable; the light
    (churn-only) pass PRESERVES the previously-computed physical/reclaimable rather than wiping
    them. Persisted to DuckDB."""
    key, srcs = resolve_source(repo, source=source)
    snaps = sorted(srcs[key], key=lambda s: s.get("startTime", ""))  # oldest -> newest
    smap = _size_map(repo)
    roots = {s["id"]: (s.get("rootEntry") or {}).get("obj") for s in snaps}

    # Forward pass: churn (new physical) + remember each snapshot's forward-new chunk set. The
    # tree reach replaces the incremental walk; subtracting the accumulated set gives the same
    # "content this snapshot introduced" answer the old seen_dirs/seen_content pair did.
    seen_content: set[str] = set()
    fwd_new: dict[str, set[str]] = {}
    churn: dict[str, int] = {}
    for snap in snaps:
        found = layer2.reach_content_ids(repo, [roots[snap["id"]]]) if roots[snap["id"]] else set()
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
        pcur = read_cursor(repo)
        try:
            for snap in snaps:
                root = roots[snap["id"]]
                physical[snap["id"]] = (
                    _subtree_physical(repo, root, smap, phys_memo, icache, pcur) if root else 0)
        finally:
            pcur.close()
        # ... + reclaimable (backward pass: chunks ONLY in this snapshot, source-exclusive).
        refs = _refcount_map(repo)  # global per-source refcount: ==1 means only this source has it
        seen_content_b: set[str] = set()
        for snap in reversed(snaps):  # newest -> oldest
            root = roots[snap["id"]]
            found = layer2.reach_content_ids(repo, [root]) if root else set()
            exclusive = fwd_new[snap["id"]] & (found - seen_content_b)  # no older, no newer
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
            "SELECT snapshot_id, churn, physical, reclaimable, snaphash FROM sanalysis WHERE source=?",
            [key],
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


def prewarm(repo: str, with_reclaim: bool = False) -> dict | None:
    """Background: keep per-snapshot analysis warm. churn-only (``with_reclaim=False``) recomputes
    only when the snapshot set changed; the heavy pass (``with_reclaim=True``) ALSO recomputes when
    physical/reclaimable are missing for any snapshot - so the slow pass actually refills the heavy
    fields instead of being skipped because churn looks fresh (the old reclaimable-blank bug).

    A source that fails is skipped and counted, never fatal: partial results are published and the
    stage succeeds. Returns ``{"failed_sources": [...]}`` when anything failed, else None - the
    caller surfaces the list so a nightly cannot report a clean pass that lost sources."""
    from ..core.sources import sources_index
    heavy = 0
    failed: list[str] = []
    recomputed = 0
    compute_secs = 0.0
    t_stage = time.perf_counter()
    with batched(repo):  # ONE published generation per stage, not one per source
        srcs = sources_index(repo)
        for key in sorted(srcs):
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
                # Per-source timing: the three whole-history passes in compute_source are the
                # analysis stage's cost ( - the dominant one), and only these lines show
                # which source ate the time.
                t_src = time.perf_counter()
                compute_source(repo, key, with_reclaim=with_reclaim)
                dt = time.perf_counter() - t_src
                recomputed += 1
                compute_secs += dt
                log.info("repo=%s analysis recomputed %s (%d snapshots, %s) in %.1fs",
                         repo, key, len(srcs[key]), "heavy" if with_reclaim else "churn-only", dt)
            except Exception as exc:  # noqa: BLE001 - one source failing must not abort the stage
                failed.append(key)
                log.warning("repo=%s analysis failed for %s: %s", repo, key, exc)
                continue
    log.info("repo=%s analysis stage: %d of %d source(s) recomputed in %.1fs of %.1fs total",
             repo, recomputed, len(srcs), compute_secs, time.perf_counter() - t_stage)
    if failed:
        log.warning("repo=%s analysis stage: %d source(s) failed (partial results published): %s",
                    repo, len(failed), ", ".join(failed[:10]))
        return {"failed_sources": failed}
    return None
