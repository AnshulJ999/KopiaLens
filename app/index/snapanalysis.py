"""Per-snapshot analysis: churn (new physical) + reclaimable (freed if this snapshot is
deleted), persisted to DuckDB so it survives restarts and is instant to re-view.

Churn = the chunks a snapshot introduced, i.e. those whose oldest snapshot is this one.
Reclaimable = chunks that exist ONLY in this snapshot (their oldest and newest snapshot are the
same one) and in no OTHER source (global source refcount == 1). Deleting it frees exactly those.
Physical = the snapshot's own footprint: the packed bytes of every file it reaches.

All three come from ONE recursive pass per source recording which snapshots reach which folders
(). They were three separate whole-tree passes PER snapshot, which is why a 514-snapshot
source took 662 s.
"""

from __future__ import annotations

import collections
import contextlib
from datetime import datetime
import uuid
import hashlib
import threading
import time

from ..core import paths as pathrules
from ..core.applog import get_logger
from ..core.sources import resolve_source
from . import entries, layer2, tree
from .physical import _chunks_for_objs, _refcount_map, _size_map
from .store import read_cursor

log = get_logger("analysis")

REACH_BATCH_ROWS = 2_000_000   # (snapshot, folder) pairs per recursive pass in _analyse

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
    sep = pathrules.sep(key)
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
            rel = parent + sep + name if parent else name
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
                    files.append({"full_path": root_path.rstrip(sep) + sep + rel,
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


def _ranked(snaps: list[dict]) -> list[tuple[int, str, str | None]]:
    """(rank, snapshot_id, root object) newest-first, ranked the way layer 3 ranks.

    Two stable sorts rather than one key, because the order is start time DESCENDING with the
    snapshot id ASCENDING inside a tie. Kopia checkpoints share a start time exactly, so the
    tiebreak decides which snapshot a chunk's first appearance is credited to; matching layer 3
    means the whole store answers that question the same way.
    """
    ordered = sorted(snaps, key=lambda s: s.get("id") or "")
    ordered.sort(key=lambda s: s.get("startTime") or "", reverse=True)
    return [(r, s["id"], (s.get("rootEntry") or {}).get("obj"))
            for r, s in enumerate(ordered, 1)]


@contextlib.contextmanager
def prepared(repo: str):
    """One read cursor for a whole analysis stage, holding what every source's analysis re-derived
    from the 20M-row ``tent``: the folder->subfolder links (scanned once per tree LEVEL per source
    otherwise) and each folder's own file bytes. Both are small next to ``tent``."""
    f = tree.TYPES.index("f")
    cur = read_cursor(repo)
    try:
        cur.execute("CREATE OR REPLACE TEMP TABLE _sa_link AS "
                    "SELECT dir_id, child FROM tent WHERE child IS NOT NULL")
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE _sa_own AS
            SELECT e.dir_id,
                   sum(coalesce(CASE WHEN e.ind THEN ip.packed ELSE cs.packed END, 0))::HUGEINT AS own
            FROM tent e
            LEFT JOIN csize cs ON cs.cid_id = e.cid_id
            LEFT JOIN iphys ip ON ip.cid_id = e.cid_id
            WHERE e.etype = {f} AND e.cid_id IS NOT NULL
            GROUP BY e.dir_id""")
        yield {"cur": cur, "link": "_sa_link", "own": "_sa_own"}
    finally:
        for t in ("_sa_link", "_sa_own"):
            try:
                cur.execute(f"DROP TABLE IF EXISTS {t}")
            except Exception:  # noqa: BLE001 - cleanup must never mask the stage result
                pass
        cur.close()


def _reach_bound(snap: dict) -> int | None:
    """The most (snapshot, folder) pairs one snapshot can add to the reach: its folders + the root."""
    dirs = ((snap.get("rootEntry") or {}).get("summ") or {}).get("dirs")
    return int(dirs) + 1 if isinstance(dirs, int) and dirs >= 0 else None


def _analyse(repo: str, snaps: list[dict], heavy: bool,
             prep: dict | None = None) -> tuple[dict, dict, dict]:
    """Churn, physical and reclaimable for EVERY snapshot of one source, as set operations.

    One recursive pass records which snapshots reach which folders; the rest are group-bys over
    it. What this replaces ran two whole-tree reaches plus a Python folder walk PER snapshot,
    which cost 722 s on a 514-snapshot source whose snapshots hold a few hundred chunks each -
    the cost was per call, not per byte.

    The two halves read that pass differently, and the difference is the whole performance story:

      * churn and reclaimable only ever ask "oldest and newest snapshot holding this chunk", so
        the folders collapse to one row each FIRST and the entry join stays tree-sized.
      * physical is a total, so it needs real per-snapshot membership - but it joins folders to a
        number already aggregated per folder, so the entries are never multiplied either.
    """
    import pyarrow as pa

    ranked = _ranked(snaps)
    churn = {sid: 0 for _r, sid, _o in ranked}
    physical = {sid: 0 for _r, sid, _o in ranked}
    reclaim: dict[str, int] = {sid: 0 for _r, sid, _o in ranked}
    of_rank = {r: sid for r, sid, _o in ranked}
    live = [(r, obj) for r, _sid, obj in ranked if obj]
    if not live:
        return churn, physical, reclaim

    f = tree.TYPES.index("f")
    # Working tables carry a per-RUN suffix: two Analyze passes share the read connection,
    # and fixed names let one run's `_sa_dir` overwrite the other's mid-query - silently wrong
    # numbers rather than an error.
    ns = f"_sa{uuid.uuid4().hex[:8]}_"
    cur = prep["cur"] if prep else read_cursor(repo)
    link = prep["link"] if prep else "(SELECT dir_id, child FROM tent WHERE child IS NOT NULL)"
    try:
        cur.register(ns + "roots", pa.table({
            "rank": pa.array([r for r, _o in live], pa.int32()),
            "obj": pa.array([o for _r, o in live], pa.string())}))
        # A root the tree could not read still contributes its own content id, which is what
        # `reach_content_ids` does - it just reaches nothing below it.
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE {ns}rcid AS
            SELECT a.rank, c.cid_id FROM {ns}roots a JOIN cid c ON c.content_id = a.obj
            WHERE c.cid_id IS NOT NULL""")
        # Built a batch of snapshots at a time: the recursive UNION keeps every (rank, dir_id) pair
        # in one in-memory hash table that cannot spill. A snapshot adds at most its folder count
        # (kopia's root summary) plus the root, so batches fill up to REACH_BATCH_ROWS by that
        # bound; a snapshot without one is sized from the most an earlier snapshot added.
        ranks = sorted(r for r, _o in live)
        by_id = {s.get("id"): s for s in snaps}
        bound = {r: _reach_bound(by_id.get(of_rank[r]) or {}) for r in ranks}
        i, total, seen = 0, 0, 0
        while i < len(ranks):
            n, used = 0, 0
            while i + n < len(ranks):
                b = bound[ranks[i + n]] or seen
                if n and (not b or used + b > REACH_BATCH_ROWS):
                    break
                used += b
                n += 1
            lo, hi = ranks[i], ranks[i + n - 1]
            head = f"CREATE OR REPLACE TEMP TABLE {ns}reach AS" if i == 0 else f"INSERT INTO {ns}reach"
            cur.execute(f"""{head}
                WITH RECURSIVE r(rank, dir_id) AS (
                    SELECT a.rank, t.dir_id FROM {ns}roots a
                      JOIN cid c ON c.content_id = a.obj
                      JOIN tdir t ON t.cid_id = c.cid_id
                    WHERE a.rank BETWEEN {lo} AND {hi}
                    UNION
                    SELECT r.rank, l.child FROM r JOIN {link} l ON l.dir_id = r.dir_id)
                SELECT rank, dir_id FROM r""")
            now = cur.execute(f"SELECT count(*) FROM {ns}reach").fetchone()[0] or 0
            seen = max(seen, -(-(now - total) // n))
            total = now
            i += n
        cur.unregister(ns + "roots")
        # One row per folder: the newest rank reaching it and the oldest. A folder's snapshot
        # coverage can have gaps, and these two numbers deliberately ignore them - they are only
        # ever used for "first appearance" and "lives in exactly one snapshot", and both of those
        # read off the extremes.
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE {ns}dir AS
            SELECT dir_id, min(rank) AS rnew, max(rank) AS rold FROM {ns}reach GROUP BY dir_id""")
        # Same reach definition as `reach_content_ids`: every entry carrying a content id, a split
        # object's index content plus every piece behind it, and the snapshot roots themselves.
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE {ns}span AS
            SELECT cid_id, min(rnew) AS rnew, max(rold) AS rold FROM (
                SELECT l.rnew, l.rold, e.cid_id FROM {ns}dir l JOIN tent e USING (dir_id)
                    WHERE e.cid_id IS NOT NULL AND NOT e.ind
                UNION ALL
                SELECT l.rnew, l.rold, e.ind_cid AS cid_id FROM {ns}dir l JOIN tent e USING (dir_id)
                    WHERE e.cid_id IS NOT NULL AND e.ind AND e.ind_cid IS NOT NULL
                UNION ALL
                SELECT l.rnew, l.rold, p.part_cid AS cid_id FROM {ns}dir l JOIN tent e USING (dir_id)
                    JOIN tpart p ON p.iobj_cid = e.cid_id WHERE e.cid_id IS NOT NULL AND e.ind
                UNION ALL
                SELECT rank, rank, cid_id FROM {ns}rcid)
            WHERE cid_id IS NOT NULL
            GROUP BY cid_id""")
        # Churn = chunks whose OLDEST snapshot is this one, i.e. the content it introduced. A
        # chunk with no size contributes 0, as the size-map lookup did.
        for rank, total in cur.execute(f"""
                SELECT s.rold, sum(coalesce(cs.packed, 0))::HUGEINT
                FROM {ns}span s LEFT JOIN csize cs ON cs.cid_id = s.cid_id
                GROUP BY s.rold""").fetchall():
            churn[of_rank[rank]] = int(total or 0)
        if not heavy:
            return churn, physical, reclaim
        # Reclaimable = chunks in no older and no newer snapshot, and in no other source. A chunk
        # absent from `crefs` counts as zero references, same as the map lookup it replaces.
        for rank, total in cur.execute(f"""
                SELECT s.rold, sum(coalesce(cs.packed, 0))::HUGEINT
                FROM {ns}span s
                LEFT JOIN csize cs ON cs.cid_id = s.cid_id
                LEFT JOIN crefs cr ON cr.cid_id = s.cid_id
                WHERE s.rnew = s.rold AND coalesce(cr.refs, 0) <= 1
                GROUP BY s.rold""").fetchall():
            reclaim[of_rank[rank]] = int(total or 0)
        # Physical: each folder's own file bytes once, then summed over the folders each snapshot
        # reaches. A folder sitting at two paths in one snapshot is counted ONCE - kopia stores it
        # once, and the walk this replaces counted it per path.
        own_sql = (f"SELECT dir_id, own FROM {prep['own']} WHERE dir_id IN (SELECT dir_id FROM {ns}dir)"
                   if prep else None)
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE {ns}own AS {own_sql}""" if own_sql else f"""
            CREATE OR REPLACE TEMP TABLE {ns}own AS
            SELECT e.dir_id,
                   sum(coalesce(CASE WHEN e.ind THEN ip.packed ELSE cs.packed END, 0))::HUGEINT
                       AS own
            FROM tent e
            LEFT JOIN csize cs ON cs.cid_id = e.cid_id
            LEFT JOIN iphys ip ON ip.cid_id = e.cid_id
            WHERE e.etype = {f} AND e.cid_id IS NOT NULL
              AND e.dir_id IN (SELECT dir_id FROM {ns}dir)
            GROUP BY e.dir_id""")
        for rank, total in cur.execute(f"""
                SELECT r.rank, sum(coalesce(o.own, 0))::HUGEINT
                FROM {ns}reach r LEFT JOIN {ns}own o ON o.dir_id = r.dir_id
                GROUP BY r.rank""").fetchall():
            physical[of_rank[rank]] = int(total or 0)
    finally:
        # The per-run working tables must not outlive the call: the read connection is shared
        # and long-lived in the app, so without this a repo-wide Analyze piles up one set per source
        # until its single publish retires the connection.
        try:
            cur.unregister(ns + "roots")
        except Exception:  # noqa: BLE001 - normally unregistered already
            pass
        for name in (ns + "rcid", ns + "reach", ns + "dir", ns + "span", ns + "own"):
            try:
                cur.execute(f"DROP TABLE IF EXISTS {name}")
            except Exception:  # noqa: BLE001 - cleanup must never mask the analysis result
                pass
        if not prep:
            cur.close()
    return churn, physical, reclaim


def compute_source(repo: str, source: str, with_reclaim: bool = True,
                   prep: dict | None = None) -> dict:
    """Per-snapshot churn for a source. ``with_reclaim`` ALSO computes the heavy fields - absolute
    per-snapshot physical + reclaimable; the light (churn-only) pass PRESERVES the
    previously-computed physical/reclaimable rather than wiping them. Persisted to DuckDB."""
    key, srcs = resolve_source(repo, source=source)
    snaps = sorted(srcs[key], key=lambda s: s.get("startTime", ""))  # oldest -> newest
    churn, phys, recl = _analyse(repo, snaps, heavy=with_reclaim, prep=prep)

    physical: dict[str, int] = {sid: 0 for sid in churn}      # 0 = not yet computed
    reclaim: dict[str, int | None] = {sid: None for sid in churn}  # None = not computed (shows "—")

    if with_reclaim:
        physical.update(phys)
        reclaim.update(recl)
    else:
        # Light pass: keep the existing heavy fields; brand-new snapshots get 0/None so
        # phys_complete/recl_complete flip false -> the slow pass refills just those.
        existing = _existing_analysis(repo, key)
        for sid in churn:
            kept, rec = existing.get(sid, (0, None))
            physical[sid] = int(kept or 0)
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
    failed: list[str] = []
    first_error = ""
    recomputed = 0
    compute_secs = 0.0
    t_stage = time.perf_counter()
    with batched(repo), prepared(repo) as prep:  # ONE published generation per stage
        srcs = sources_index(repo)
        for key in sorted(srcs):
            try:
                info = read_analysis(repo, key)
                need = (not info["fresh"]) or (with_reclaim and not (info["recl_complete"] and info["phys_complete"]))
                if not need:
                    continue
                # Per-source timing: only these lines show which source ate the stage.
                t_src = time.perf_counter()
                compute_source(repo, key, with_reclaim=with_reclaim, prep=prep)
                dt = time.perf_counter() - t_src
                recomputed += 1
                compute_secs += dt
                log.info("repo=%s analysis recomputed a source (%d snapshots, %s) in %.1fs",
                         repo, len(srcs[key]), "heavy" if with_reclaim else "churn-only", dt)
                log.debug("repo=%s analysis source %s", repo, key)
            except Exception as exc:  # noqa: BLE001 - one source failing must not abort the stage
                failed.append(key)
                first_error = first_error or str(exc).splitlines()[0][:300]
                log.debug("repo=%s analysis failed for %s: %s", repo, key, exc)
                continue
    log.info("repo=%s analysis stage: %d of %d source(s) recomputed in %.1fs of %.1fs total",
             repo, recomputed, len(srcs), compute_secs, time.perf_counter() - t_stage)
    if failed:
        log.warning("repo=%s analysis stage: %d source(s) failed (partial results published): %s; first error: %s",
                    repo, len(failed), ", ".join(failed[:10]), first_error)
        return {"failed_sources": failed}
    return None
