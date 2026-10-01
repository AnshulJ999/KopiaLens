"""M1 writer: produce a repo's Parquet generation (the single entry point is ``build_repo``).

With no usable manifest under ``root`` (fresh install, schema bump, counters missing, an
artifact file lost on disk) it does a FULL rebuild: the existing DuckDB builder
(``builder._build_source`` + ``backfill_first_seen``) runs against a SCRATCH database, the
result is transformed into the skinny integer-id schema (ported from
``experiments/build_skinny.py``), verified, and written as zstd Parquet.

With a usable manifest it UPDATES per source (plan 2.10): unchanged sources are carried into
the new manifest by reference - no walk, no rewrite, ids untouched - and only sources whose
snapshot set changed are re-walked. Existing path ids and sticky ``first_seen`` dates carry
through the previous artifacts; new ids are allocated above the manifest's counters
(``ids.Allocator``). The maps are rewritten whole (accepted, ~40 MB); ``sanalysis`` is
carried by reference unchanged.

Nothing here touches the live generation - ``publish.py`` moves the staged files and swaps
the manifest atomically.
"""

from __future__ import annotations

import json
import os
import re
import time

import duckdb
import pyarrow as pa

from ..core import paths as pathrules
from ..core.applog import get_logger
from ..core.sources import sources_index
from . import crypto, ids, layer2, layer3, perf, store, tree, treeconvert, treesync
from .builder import _drop_indexes
from .physical import fill_paths_physical
from .schema import init_schema
from .upgrade import MANIFEST_VERSION as _MANIFEST_VERSION, IndexVersionMismatch

log = get_logger("store")

_ZSTD = "FORMAT parquet, COMPRESSION zstd"
# 3 = the unified store: layer 1 (the tree) is published inside the generation. A generation at
# version 2 has no tree to carry, so it rebuilds once and carries one from then on.
# The generation format lives in index/upgrade.py (one authority: it is also imported
# by index/store.py, which refuses to read a generation written at another version).
_TREE_TABLES = ("tdir", "tent", "tpart")
_MAX_UNREADABLE = 200      # how many unreadable object ids the manifest carries


def _copy_opts() -> str:
    """The COPY option list for this build's state.

    No explicit flag on purpose: ``build_repo`` wraps the whole build in ``crypto.writing_state``
    so every COPY in it follows the generation's state (which can differ from the setting while a
    switch is pending) instead of each call site remembering to pass it.
    """
    return crypto.copy_options(_ZSTD)


def _load_tree(con, root: str, prev: dict | None) -> bool:
    """Load the carried tree into the scratch, or create it empty. True when one was carried.

    A generation published before the unified store has no tree artifacts. That is not an error:
    the sync simply finds nothing known and reads what it needs from kopia, exactly as a first
    index does.
    """
    tree.create_tables(con)
    art = (prev or {}).get("artifacts", {})
    if not all(name in art for name in _TREE_TABLES):
        return False
    for name in _TREE_TABLES:
        paths = [_fwd(os.path.join(root, r)) for r in art[name]]
        with treesync.timed(f"load/{name}"):
            con.execute(f"DROP TABLE IF EXISTS {name}")
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM "
                        f"read_parquet({crypto.parquet_read_list(paths)}, "
                        f"hive_partitioning=false)")
    return True


def _sync_tree(con, repo: str, srcs: dict, alloc, job: dict) -> dict:
    """Bring the tree up to date with the current snapshots, then merge and GC.

    The GC is driven by the listing the CALLER passed in, which it has already established came
    from a command that succeeded. Pruning the tree against a failed listing would drop folders
    the store still needs - the same class of mistake that once made an empty listing look like
    a repository with no sources.
    """
    job["current_source"] = "(tree sync)"
    roots = sorted({(s.get("rootEntry") or {}).get("obj")
                    for snaps in srcs.values() for s in snaps
                    if (s.get("rootEntry") or {}).get("obj")})

    # No tree yet, but a warm walk cache on disk: seed from it instead of re-reading every
    # folder object from kopia. This is the upgrade path for an install that predates the
    # unified store - without it the first run after the schema bump would re-fetch 1.4M
    # folders on repo 1. A fresh install has no cache and simply reads from kopia.
    seeded = {}
    if not con.execute("SELECT count(*) FROM tdir").fetchone()[0]:
        try:
            if os.path.exists(treeconvert.cache_path(repo)):
                job["current_source"] = "(seeding the tree from the walk cache)"
                log.info("repo=%s no tree yet - seeding layer 1 from the walk cache", repo)
                # A real denominator (the cache's folder count), so the bar is honest here.
                seeded = treeconvert.convert(
                    con, repo, alloc=alloc,
                    progress=lambda **kw: job.update({"phase": "seeding the tree", "seed": kw}))
        except Exception as exc:  # noqa: BLE001 - a bad cache must not block the build
            log.warning("repo=%s walk-cache seed failed (%s) - reading the tree from kopia "
                        "instead, which is slower but correct", repo, exc)

    known_sql = "SELECT c.content_id AS oid FROM tdir d JOIN cid c USING (cid_id)"
    if seeded:
        # The seed's folders are in _tdir_new, not tdir (the merge happens later), so without
        # this the sync would re-read from kopia everything the walk cache just gave us.
        known_sql += " UNION SELECT c.content_id FROM _tdir_new n JOIN cid c USING (cid_id)"
    con.execute(f"CREATE OR REPLACE TABLE _known AS {known_sql}")
    job["phase"] = "tree sync"
    with perf.phase("tree/read from kopia"):
        res = treesync.sync(con, repo, roots, known_table="_known", alloc=alloc,
                            progress=lambda **kw: job.update({"sync": kw}))
    job.pop("seed", None)
    with perf.phase("tree/merge"):
        mat = treesync.materialize(con, alloc, base_known="tdir")
        merged = _reach_and_merge(con, roots)
    perf.count(tree_folders=merged.get("folders"), tree_entries=merged.get("entries"),
               tree_parts=merged.get("parts"), tree_levels=res.levels,
               folders_read=res.fetched, unreadable=len(res.unreadable))
    return {"sync": res.as_dict(), "materialize": mat, "merge": merged,
            "incomplete": sorted(set(res.unreadable))}


def _reach_and_merge(con, roots: list[str]) -> dict:
    """GC the tree to what the current snapshot roots reach, then merge it in walk order."""
    import pyarrow as pa
    con.register("_roots_arrow", pa.table({"content_id": pa.array(roots, pa.string())}))
    con.execute("CREATE OR REPLACE TABLE _root_cids AS SELECT c.cid_id FROM cid c "
                "JOIN _roots_arrow r USING (content_id)")
    con.unregister("_roots_arrow")
    treesync.reach_from_roots(con, "_root_cids", "_tree_reach")
    merged = treesync.merge(con, reachable="_tree_reach")
    for name in _TREE_TABLES:
        con.execute(f"DROP TABLE {name}")
        con.execute(f"ALTER TABLE {name}_final RENAME TO {name}")
    return merged


def _empty_previous(con) -> None:
    """The dictionaries and carried tables a FIRST build starts from, empty but correctly typed.

    A full rebuild is just an incremental build whose previous generation happens to be empty.
    Keeping one code path means the id allocation, the per-source verification and the manifest
    assembly are exercised on every run rather than only on the path that happens to be taken.
    """
    con.execute("CREATE OR REPLACE TABLE src (src_id SMALLINT, source VARCHAR)")
    con.execute("""CREATE OR REPLACE TABLE dir (dir_id INTEGER, src_id SMALLINT,
        dir_path VARCHAR, depth SMALLINT, name VARCHAR, parent_id INTEGER)""")
    con.execute("CREATE OR REPLACE TABLE anc (dir_id INTEGER, depth SMALLINT, anc_id INTEGER)")
    con.execute("CREATE OR REPLACE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    con.execute("""CREATE OR REPLACE TABLE snap (snap_id INTEGER, snapshot_id VARCHAR,
        start_time TIMESTAMP, src_id SMALLINT)""")
    con.execute("""CREATE OR REPLACE TABLE prev_isnaps (repo VARCHAR, snapshot_id VARCHAR,
        source VARCHAR, start_time TIMESTAMP, root_obj VARCHAR, indexed_at TIMESTAMP,
        file_count BIGINT)""")
    con.execute("""CREATE OR REPLACE TABLE prev_pathx (path_id INTEGER, src_id SMALLINT,
        dir_id INTEGER, name VARCHAR, ext VARCHAR, size UBIGINT, versions INTEGER,
        first_seen TIMESTAMP, last_seen TIMESTAMP, in_latest BOOLEAN, cid_id INTEGER,
        physical UBIGINT, exclusive UBIGINT, full_path VARCHAR)""")
    con.execute("""CREATE OR REPLACE TABLE prev_hist (src_id SMALLINT, path_id INTEGER,
        snap_id INTEGER, cid_id INTEGER, size UBIGINT, mtime TIMESTAMP, ordinal BIGINT)""")


def _write_tree(con, staging: str, gen: int) -> tuple[dict, list]:
    """Stage the three tree artifacts. Written in walk order, which is both how a folder's
    entries become a contiguous row range and what makes them compress."""
    artifacts, moves = {}, []
    for name, order in (("tdir", "dir_id"), ("tent", "dir_id, pos"), ("tpart", "iobj_cid, ord")):
        out = os.path.join(staging, f"{name}.parquet")
        with treesync.timed(f"write/{name}"):
            con.execute(f"COPY (SELECT * FROM {name} ORDER BY {order}) "
                        f"TO '{_fwd(out)}' ({_copy_opts()})")
        rel = f"pool/tree/{name}/g{gen}.parquet"
        artifacts[name] = [rel]
        moves.append((out, rel))
    return artifacts, moves


def _fwd(p: str) -> str:
    return p.replace("\\", "/").replace("'", "''")


def _scratch(staging: str) -> tuple[str, duckdb.DuckDBPyConnection]:
    """A fresh scratch DuckDB in ``_staging/`` - spilled to disk, closed and deleted after.

    Goes through ``crypto.open_scratch`` so the build gets the machine's RAM tier, the registered
    Parquet key, and - when encryption is on - an encrypted scratch file and encrypted spills. A
    hand-rolled ``duckdb.connect()`` here silently missed all four.
    """
    scratch = os.path.join(staging, "build.duckdb")
    for suffix in ("", ".wal"):
        if os.path.exists(scratch + suffix):
            os.remove(scratch + suffix)
    limit, threads = store.resolve_limits()
    con = crypto.open_scratch(scratch, os.path.join(staging, "tmp"), limit, threads)
    return scratch, con


def _close_scratch(scratch: str, con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.close()
    except Exception:  # noqa: BLE001
        pass
    for suffix in ("", ".wal"):
        try:
            if os.path.exists(scratch + suffix):
                os.remove(scratch + suffix)
        except Exception:  # noqa: BLE001
            pass


def build_repo(repo: str, root: str, gen: int = 1, job: dict | None = None, full: bool = False,
               *, force: frozenset | set | None = None) -> dict:
    """Produce the manifest body for the repo's CURRENT generation under ``root`` (the caller
    publishes it via ``publish.publish``; ``root/_staging`` holds the scratch DB and staged
    files). One entry point, two internal paths:

    - ``full=True``, no usable manifest, a schema-version change, or missing counters/artifact
      files -> rebuild from scratch, through the SAME code path.
    - otherwise -> per-source update (``_build_incremental``): unchanged sources carried by
      reference, only changed sources re-walked.

    ``gen`` is bumped past the published generation either way, so artifact file names are
    never reused while a reader can hold them. ``force`` names sources to rebuild even when
    their snapshot set is unchanged (manual rebuild).

    A schema-version change is migrated IN PLACE by the app's startup routine (``index.upgrade``),
    never by a rebuild: a rebuild is 10-40 min per repo and loses the sticky first-seen dates. This
    path only sees the leftover case (no migration registered, or the routine could not run), and
    says so loudly before falling back.

    A build never changes the generation's encryption state: an existing manifest dictates it,
    and only a first build follows the setting. ``crypto.writing_state`` pins that for every
    COPY and every connection inside the build."""
    job = {} if job is None else job
    prev = _read_prev_manifest(root)
    if prev is not None:
        gen = max(int(gen or 1), int(prev.get("generation") or 0) + 1)
        found = int(prev.get("schema_version") or 0)
        if found > _MANIFEST_VERSION:
            raise IndexVersionMismatch(repo, found, _MANIFEST_VERSION)
        if full:
            prev = None
        elif found != _MANIFEST_VERSION:
            log.warning("repo=%s index format %s -> %s has no in-place migration applied - "
                        "rebuilding from kopia (10-40 min; sticky first-seen dates are lost)",
                        repo, found, _MANIFEST_VERSION)
            prev = None
        elif not _manifest_usable(root, prev):
            prev = None          # rebuild from scratch, through the SAME code path
    state = bool(prev.get("encrypted")) if prev is not None else crypto.writes_encrypted()
    with crypto.writing_state(state):
        body = _build_incremental(repo, root, prev, gen, job, frozenset(force or ()))
    body["encrypted"] = state
    return body


def _read_prev_manifest(root: str) -> dict | None:
    try:
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 - absent or corrupt manifest -> rebuild from scratch
        return None


def _manifest_usable(root: str, prev: dict) -> bool:
    """Every artifact the manifest names exists and counters are present; a file lost to a
    crashed GC would make a carried reference unreadable, so any miss forces a full rebuild."""
    try:
        if not prev.get("counters"):
            return False
        for rels in prev.get("artifacts", {}).values():
            for rel in rels:
                if not os.path.exists(os.path.join(root, rel)):
                    return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _file_src_map(con, root: str, rels: list[str]) -> dict[int, list[str]]:
    """src_id -> pool-relative files for a per-source artifact list (pathx/history), read from
    the files themselves. Files with no rows carry no src_id; the writer's ``src_id=N`` naming
    attributes those, and anything still unattributable fails loudly rather than silently
    dropping a source from the next manifest."""
    out: dict[int, list[str]] = {}
    fl = [_fwd(os.path.join(root, r)) for r in rels]
    for sid, fn in con.execute(
            f"SELECT DISTINCT src_id, filename FROM read_parquet({crypto.parquet_read_list(fl)}, "
            f"filename=true, hive_partitioning=false)").fetchall():
        out.setdefault(int(sid), []).append(os.path.relpath(fn, root).replace(os.sep, "/"))
    mapped = {r for lst in out.values() for r in lst}
    for rel in rels:
        if rel in mapped:
            continue
        m = re.search(r"src_id=(\d+)", rel)
        if not m:
            raise RuntimeError(f"cannot attribute artifact file {rel!r} to a source")
        out.setdefault(int(m.group(1)), []).append(rel)
    return out


def _build_incremental(repo: str, root: str, prev: dict | None, gen: int, job: dict,
                       force: frozenset) -> dict:
    """Per-source update of the published generation under ``root`` (plan 2.10). Unchanged
    sources are carried into the new manifest by reference - no walk, no rewrite, ids
    untouched. A changed source is re-walked (append fast path when the change is strictly new
    snapshots on top, else a full source rebuild), its new paths anti-joined against its
    previous ``pathx`` artifact so every surviving row keeps its ``path_id``, and sticky
    ``first_seen`` merged least() from the previous artifact exactly as
    ``builder._capture_old_seen`` does - captured BEFORE the rewrite. New ids are allocated
    above the manifest's counters in bulk (``ids.Allocator``). A vanished source is dropped
    from the manifest and purged from the dictionaries (its ``cid`` rows are kept - content
    ids can be shared across sources); GC reclaims its files. The maps are rewritten whole
    (accepted, ~40 MB); ``sanalysis`` is carried by reference unchanged.

    ``prev=None`` means a FULL rebuild: the dictionaries start empty, every source is a target,
    and nothing is carried. folded the two build paths together - a full rebuild used to
    be a separate function with its own id assignment, which meant the incremental path's
    verification only ever ran on incremental builds.

    Sources are rebuilt from the TREE, not by walking kopia one folder at a time. A changed
    source costs about a second, so there is no append fast path and no special case for
    retention expiry: whatever changed, the source is rebuilt whole."""
    staging = os.path.join(root, "_staging")
    os.makedirs(staging, exist_ok=True)
    t0 = time.perf_counter()
    perf.lap("listing")
    srcs = sources_index(repo)
    perf.set_inputs(srcs, first=prev is None)
    prev_sources = set((prev or {}).get("sources") or ())
    if not srcs and prev_sources:
        raise RuntimeError(
            f"repo {repo}: the snapshot listing came back empty while the published "
            f"generation holds {len(prev_sources)} source(s) - refusing to treat that as a "
            "removal. The repository is most likely unreachable; the current generation is "
            "untouched.")
    # A source indexed from a tree the sync could not read whole is still recorded as
    # indexed, and its snapshot set then matches the listing forever - so it is carried by
    # reference every later night and the short rows never come back. Archival sources never gain
    # a new snapshot, so the gap is permanent.
    #
    # So the previous generation's unreadable set triggers a rebuild - but ONCE per distinct set.
    # Real repositories do contain permanently unreadable content, and forcing on every run would
    # then rebuild every source every night forever (minutes per night on repo 2) while the tree
    # still cannot hold the folder. If the set changes - something became readable, or something
    # new broke - there is fresh information and it forces again.
    unread_prev = list((prev or {}).get("tree_incomplete") or [])
    forced_for = list((prev or {}).get("tree_forced_for") or [])
    if unread_prev and unread_prev != forced_for:
        log.warning("repo=%s the previous tree sync left %d folder object(s) unread - rebuilding "
                    "every source once from the repaired tree", repo, len(unread_prev))
        force = frozenset(srcs)
        forced_for = unread_prev
    elif unread_prev:
        log.warning("repo=%s %d folder object(s) are still unreadable and were already retried - "
                    "not forcing another full rebuild. e.g. %s",
                    repo, len(unread_prev), unread_prev[:3])
    perf.lap("load previous")
    scratch, con = _scratch(staging)
    prev_art = (prev or {}).get("artifacts", {})
    moves: list[tuple[str, str]] = []
    try:
        init_schema(con)
        _drop_indexes(con)

        # The previous generation's dictionaries, loaded as scratch tables - new rows are
        # appended and the whole tables are rewritten as this generation's dict artifacts.
        # hive_partitioning=false: the pool paths look like `src_id=N/`, and DuckDB's
        # auto-detection would let the DIRECTORY name override the file's own src_id column.
        def _read_prev_art(name: str, out: str) -> None:
            fl = [_fwd(os.path.join(root, r)) for r in prev_art[name]]
            con.execute(f"CREATE OR REPLACE TABLE {out} AS SELECT * FROM "
                        f"read_parquet({crypto.parquet_read_list(fl)}, "
                        f"hive_partitioning=false)")

        _empty_previous(con)
        if prev is not None:
            for name in ("src", "dir", "anc", "cid", "snap"):
                _read_prev_art(name, name)
            _read_prev_art("isnaps", "prev_isnaps")
        _load_tree(con, root, prev)
        con.execute("CREATE TABLE src_rev AS SELECT source, src_id FROM src")
        con.execute("CREATE TABLE cid_rev AS SELECT content_id, cid_id FROM cid")
        con.execute("CREATE TABLE snap_rev AS SELECT snapshot_id, snap_id FROM snap")

        # Classification - the _run_build rules, against the published generation.
        prev_sets: dict[str, set] = {}
        for source, sid in con.execute("SELECT source, snapshot_id FROM prev_isnaps").fetchall():
            prev_sets.setdefault(source, set()).add(sid)
        stale = sorted((prev_sources | set(prev_sets)) - set(srcs))
        # One classification rule now: the snapshot set changed, or the caller forced it. There
        # is no append mode any more - rebuilding a source from the tree costs about a second,
        # and the append path was the source of both known index bugs (a late first_seen and
        # duplicate rows after a folder reverted to an earlier state).
        targets: list[tuple[str, str, list | None]] = []
        for key in sorted(srcs):
            cur_ids = {s["id"] for s in srcs[key]}
            if key in prev_sources and key not in force and cur_ids == prev_sets.get(key, set()):
                continue  # carried forward by reference
            targets.append((key, "rebuild", None))
        # `total` and `indexed` must carry the SAME unit - index_status renders the pair straight
        # into the status strip. `indexed` ticks once per rebuilt SOURCE (a source rebuilds whole
        # from the tree in about a second, so counting snapshots no longer means anything), so the
        # denominator is the target count. Leaving it as a snapshot total read "3 / 6,629".
        job.update({"total": len(targets), "indexed": 0,
                    "targets": len(targets), "current_source": None, "error": None})
        log.info("repo=%s index classified %d source(s) in %.1fs: %d carried, %d to rebuild",
                 repo, len(srcs), time.perf_counter() - t0,
                 len(srcs) - len(targets), len(targets))

        # Snapshot ids that still exist in the CURRENT listing, and the sources being purged -
        # both as tables so the carry filters below never build huge IN-lists in SQL text.
        tbl = pa.table({"snapshot_id": pa.array(
            sorted({s["id"] for src in srcs.values() for s in src}), pa.string())})
        con.register("_cur_snaps_arrow", tbl)
        con.execute("CREATE OR REPLACE TABLE _cur_snaps AS "
                    "SELECT snapshot_id FROM _cur_snaps_arrow")
        con.unregister("_cur_snaps_arrow")
        tbl = pa.table({"source": pa.array(list(stale), pa.string())})
        con.register("_stale_arrow", tbl)
        con.execute("CREATE OR REPLACE TABLE _stale_srcs AS SELECT source FROM _stale_arrow")
        con.unregister("_stale_arrow")

        # Stale sources: purge their dictionary rows. Their artifacts simply are not carried.
        stale_ids = []
        if stale:
            stale_ids = [r[0] for r in con.execute(
                f"SELECT src_id FROM src WHERE source IN ({','.join('?' * len(stale))})",
                stale).fetchall()]
        for sid in stale_ids:
            con.execute("DELETE FROM anc WHERE dir_id IN (SELECT dir_id FROM dir WHERE src_id=?)",
                        [sid])
            con.execute("DELETE FROM dir WHERE src_id=?", [sid])
            con.execute("DELETE FROM snap WHERE src_id=?", [sid])
            con.execute("DELETE FROM src WHERE src_id=?", [sid])

        # The previous pathx rows of the sources being rebuilt - the source of their carried
        # path ids and sticky first_seen dates. src_id=-1 matches nothing when the list is
        # empty, which also yields an empty clone with the published schema.
        src_of = dict(con.execute("SELECT source, src_id FROM src_rev").fetchall())
        tgt_ids = sorted({int(src_of[k]) for k, _m, _a in targets if src_of.get(k) is not None})
        if prev is not None:
            fl = [_fwd(os.path.join(root, r)) for r in prev_art["pathx"]]
            con.execute(f"CREATE OR REPLACE TABLE prev_pathx AS SELECT * FROM "
                        f"read_parquet({crypto.parquet_read_list(fl)}, hive_partitioning=false) "
                        f"WHERE src_id IN ({','.join('?' * len(tgt_ids or [-1]))})",
                        tgt_ids or [-1])

        # The allocator is the single id authority for this build, so it has to exist before the
        # tree sync: folder objects, file contents and chunk ids all come out of one dictionary.
        alloc = ids.Allocator((prev or {}).get("counters") or {})
        hw0 = alloc.counters()

        # Layer 1: bring the tree up to date, then merge and GC it.
        perf.lap("tree")
        tree_stats = _sync_tree(con, repo, srcs, alloc, job)
        perf.lap("rebuild sources")

        # Rebuild what changed, from the TREE. One level pass over whole sets replaces the
        # per-folder walk AND the oldest-first first_seen backfill - the two stages that used
        # to be eleven of the nightly's minutes on repo 1.
        t_walk = time.perf_counter()
        if targets:
            job["phase"] = "rebuilding sources"
            job["current_source"] = "(rebuilding from the tree)"
            layer3.build(con, repo, {k: srcs[k] for k, _m, _a in targets})
            # first_seen merges what the published index knew with what the tree says, and
            # takes the older - stickiness and correctness at once.
            layer3.refresh_paths(con, repo, [k for k, _m, _a in targets])
            log.info("repo=%s index rebuilt %d source(s) from the tree in %.1fs",
                     repo, len(targets), time.perf_counter() - t_walk)

        # Sources new to the dictionary, then the physical fill and the global refcount.
        tbl = pa.table({"source": sorted(srcs)})
        con.register("_cur_keys", tbl)
        ids.anti_join(con, "_src_new", "SELECT source FROM _cur_keys",
                      "SELECT source FROM src", ["source"])
        con.unregister("_cur_keys")
        alloc.assign(con, "_src_alloc", "SELECT source FROM _src_new", "source", "src_id",
                     id_col="src_id", id_type="SMALLINT")
        con.execute("INSERT INTO src SELECT src_id, source FROM _src_alloc")
        con.execute("INSERT INTO src_rev SELECT source, src_id FROM _src_alloc")
        src_of = dict(con.execute("SELECT source, src_id FROM src_rev").fetchall())

        # Layer 2: the maps, from the tree. `csize` is the whole `content list -l` stream, `crefs`
        # is one reach query covering every source at once (the walk cache answered this by
        # walking each source in Python, 67 s / 3.3 GB on repo 1), `iphys` is the sum of a split
        # object's pieces from `tpart`. `_rsnap` must cover EVERY source, not just the rebuilt
        # ones: the refcount is global, and a source missing from the reach reads as "no
        # references", i.e. freeable.
        job["current_source"] = "(maps from the tree)"
        job["phase"] = "maps"
        perf.lap("maps")
        t_phys = time.perf_counter()
        layer3.snapshots(con, repo, srcs)
        l2 = layer2.build(con, repo, alloc, srcs=srcs)
        fill_paths_physical(con, repo)
        log.info("repo=%s index maps (layer 2) in %.1fs: %s", repo,
                 time.perf_counter() - t_phys, l2)

        # A content id the index references that neither the tree nor the content listing knows.
        # Should be empty - this is an assertion, not an expected state. Allocating it here keeps
        # such a path priced at 0 rather than dropping it out of the dictionary entirely.
        ids.anti_join(con, "_cid_new",
                      "SELECT DISTINCT content_id FROM ("
                      "SELECT content_id FROM files WHERE content_id IS NOT NULL "
                      "UNION SELECT content_id FROM paths WHERE content_id IS NOT NULL)",
                      "SELECT content_id FROM cid", ["content_id"])
        alloc.assign(con, "_cid_alloc", "SELECT content_id FROM _cid_new", "content_id",
                     "cid_id", id_col="cid_id")
        con.execute("INSERT INTO cid SELECT cid_id, content_id FROM _cid_alloc")
        con.execute("INSERT INTO cid_rev SELECT content_id, cid_id FROM _cid_alloc")

        ids.anti_join(con, "_snap_new",
                      "SELECT DISTINCT i.snapshot_id, i.start_time, sr.src_id "
                      "FROM indexed_snapshots i JOIN src_rev sr ON sr.source = i.source",
                      "SELECT snapshot_id FROM snap", ["snapshot_id"])
        alloc.assign(con, "_snap_alloc",
                     "SELECT snapshot_id, start_time, src_id FROM _snap_new",
                     "start_time, snapshot_id", "snap_id", id_col="snap_id")
        con.execute("INSERT INTO snap SELECT snap_id, snapshot_id, start_time, src_id "
                    "FROM _snap_alloc")
        con.execute("INSERT INTO snap_rev SELECT snapshot_id, snap_id FROM _snap_alloc")
        # Drop the dictionary rows of snapshots that no longer exist (retention expiry, manual
        # delete). Surviving rows keep their ids - allocation never renumbers - and no file row
        # can reference them: a source whose snapshots vanished was rebuilt above, and a carried
        # source's set was verified equal to the listing.
        con.execute("DELETE FROM snap WHERE snapshot_id NOT IN (SELECT snapshot_id FROM _cur_snaps)")
        con.execute("DELETE FROM snap_rev WHERE snapshot_id NOT IN (SELECT snapshot_id FROM _cur_snaps)")

        # Per-source mapping: carry path ids by raw full_path, sticky first_seen from the
        # immutable prev artifact, allocate the residue, rewrite the source's two artifacts.
        # Raw full_path rather than (identity, name): a case-only FOLDER rename leaves two
        # display rows sharing one identity AND one name, and grouping by identity hands both
        # rows the same id - the dup check then aborts the whole build. The old spelling keeps
        # its id, the new spelling allocates a fresh one; the sticky first_seen still carries
        # across the rename via _old_seen, which stays keyed on the folded identity.
        new_entries: dict[int, tuple[str, str]] = {}
        per_source: dict[str, dict] = {}
        t_map = time.perf_counter()
        # Every per-source statement below reads only its own source. Copies ordered by the
        # integer src_id let row-group statistics skip the other sources; filtering the unordered
        # tables by source string rescanned all of them once per source (~2 billion rows on repo 2).
        con.execute("SET preserve_insertion_order=true")
        try:
            for t in ("paths", "files"):
                con.execute(f"CREATE OR REPLACE TABLE _{t}_s AS SELECT sr.src_id, x.* FROM {t} x "
                            f"JOIN src_rev sr ON sr.source = x.source WHERE x.repo = ? "
                            f"ORDER BY sr.src_id", [repo])
        finally:
            con.execute("SET preserve_insertion_order=false")
        job["phase"] = "sources"
        perf.lap("per-source mapping")
        for key, mode, ap in targets:
            S = int(src_of[key])
            sep = pathrules.sep(key)
            ident_raw = pathrules.ident_sql(key)
            job["current_source"] = key
            job["indexed"] = int(job.get("indexed") or 0) + 1

            con.execute(
                f"CREATE OR REPLACE TEMPORARY TABLE _old_seen AS "
                f"SELECT {ident_raw} AS ident, min(first_seen) AS fs FROM prev_pathx "
                f"WHERE src_id = ? AND first_seen IS NOT NULL GROUP BY 1", [S])
            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _px AS
                    SELECT p.full_path, p.name, p.ext, p.size, p.versions,
                           least(coalesce(o.fs, p.first_seen), p.first_seen) AS first_seen,
                           p.last_seen, p.in_latest, p.content_id, p.physical
                    FROM _paths_s p
                    LEFT JOIN _old_seen o ON o.ident = {pathrules.ident_sql(key, 'p.full_path')}
                    WHERE p.src_id = ?""", [S])

            # A path's parent is its full path minus "<sep><name>"; the per-source check below
            # (d.dir_path || sep || name = full_path) fails the build if that ever disagrees.
            # Every folder is a prefix of some parent, so prefixes come from DISTINCT parents only.
            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _pxp AS
                    SELECT DISTINCT full_path,
                           left(full_path, length(full_path) - length(name) - {len(sep)}) AS parent
                    FROM _px""")
            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _dseg AS
                    SELECT DISTINCT CAST(? AS SMALLINT) AS src_id,
                           array_to_string(t.parts[1:g.k], '{sep}') AS dir_path,
                           g.k::SMALLINT AS depth
                    FROM (SELECT string_split(parent, '{sep}') AS parts
                          FROM (SELECT DISTINCT parent FROM _pxp)) t,
                         unnest(generate_series(1, len(t.parts))) AS g(k)""", [S])
            ids.anti_join(con, "_dirs_new", "SELECT src_id, dir_path, depth FROM _dseg",
                          "SELECT src_id, dir_path FROM dir", ["src_id", "dir_path"])
            alloc.assign(con, "_dirs_alloc", "SELECT src_id, dir_path, depth FROM _dirs_new",
                         "src_id, dir_path", "dir_id", id_col="dir_id")
            con.execute(
                f"""INSERT INTO dir
                    SELECT a.dir_id, a.src_id, a.dir_path, a.depth,
                           array_to_string(string_split(a.dir_path, '{sep}')
                               [len(string_split(a.dir_path, '{sep}')):], '{sep}'),
                           p.dir_id
                    FROM _dirs_alloc a LEFT JOIN (
                             SELECT dir_id, src_id, dir_path FROM dir
                             UNION ALL SELECT dir_id, src_id, dir_path FROM _dirs_alloc) p
                        ON p.src_id = a.src_id AND p.dir_path =
                           CASE WHEN a.depth <= 1 THEN NULL ELSE
                                array_to_string(string_split(a.dir_path, '{sep}')
                                    [1:a.depth - 1], '{sep}') END""")
            con.execute(
                """CREATE OR REPLACE TABLE _anc_new AS
                   WITH RECURSIVE up(dir_id, anc_id) AS (
                       SELECT dir_id, dir_id FROM _dirs_alloc
                       UNION ALL
                       SELECT u.dir_id, d.parent_id FROM up u JOIN dir d ON d.dir_id = u.anc_id
                       WHERE d.parent_id IS NOT NULL)
                   SELECT u.dir_id, a.depth, u.anc_id FROM up u JOIN dir a ON a.dir_id = u.anc_id""")
            con.execute("INSERT INTO anc SELECT dir_id, depth, anc_id FROM _anc_new")

            con.execute(
                f"CREATE OR REPLACE TEMPORARY TABLE _px_carry AS "
                f"SELECT full_path, min(path_id) AS path_id FROM prev_pathx "
                f"WHERE src_id = ? GROUP BY 1", [S])
            ids.anti_join(con, "_px_new",
                          "SELECT full_path FROM _px",
                          "SELECT full_path FROM _px_carry", ["full_path"])
            alloc.assign(con, "_px_alloc", "SELECT full_path FROM _px_new", "full_path",
                         "path_id", id_col="path_id")
            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _px_final AS
                    SELECT COALESCE(al.path_id, ca.path_id) AS path_id,
                           CAST(? AS SMALLINT) AS src_id, d.dir_id, px.name, px.ext, px.size,
                           CAST(px.versions AS INTEGER) AS versions, px.first_seen, px.last_seen,
                           px.in_latest, cr.cid_id, px.physical,
                           CAST(NULL AS UBIGINT) AS exclusive, px.full_path
                    FROM _px px
                    JOIN _pxp pp ON pp.full_path = px.full_path
                    LEFT JOIN _px_carry ca ON ca.full_path = px.full_path
                    LEFT JOIN _px_alloc al ON al.full_path = px.full_path
                    JOIN dir d ON d.src_id = ? AND d.dir_path = pp.parent
                    LEFT JOIN cid_rev cr ON cr.content_id = px.content_id""", [S, S])

            # The scratch `files` table already holds the source's COMPLETE history - the
            # append's preloaded retained rows plus the walk's fresh rows - mapped through the
            # new dictionaries. Carrying prev_hist rows here as well would duplicate them.
            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _fv_final AS
                    SELECT CAST(? AS SMALLINT) AS src_id, m.path_id, sm.snap_id, cr.cid_id,
                           f.size, f.mtime, f.ordinal
                    FROM _files_s f
                    JOIN _px_final m ON m.full_path = f.full_path
                    JOIN snap_rev sm ON sm.snapshot_id = f.snapshot_id
                    LEFT JOIN cid_rev cr ON cr.content_id = f.content_id
                    WHERE f.src_id = ?""", [S, S])

            # Per-source acceptance - the same checks the old full rebuild ran, plus
            # path-id uniqueness and the sticky-first_seen guarantee, before anything is
            # staged for publication.
            row = {
                "paths": con.execute("SELECT count(*) FROM _px").fetchone()[0],
                "pathx": con.execute("SELECT count(*) FROM _px_final").fetchone()[0],
                "dup_path_id": con.execute(
                    "SELECT count(*) - count(DISTINCT path_id) FROM _px_final").fetchone()[0],
                "mismatch": con.execute(
                    f"SELECT count(*) FROM _px_final f JOIN dir d ON d.dir_id = f.dir_id "
                    f"WHERE (d.dir_path || '{sep}' || f.name) IS DISTINCT FROM f.full_path"
                ).fetchone()[0],
                "sticky_violations": con.execute(
                    f"""SELECT count(*) FROM _px_final f JOIN _old_seen o
                        ON o.ident = {pathrules.ident_sql(key, 'f.full_path')}
                        WHERE f.first_seen > o.fs""").fetchone()[0],
                "files": con.execute("SELECT count(*) FROM _files_s WHERE src_id = ?",
                                     [S]).fetchone()[0],
                "filev": con.execute("SELECT count(*) FROM _fv_final").fetchone()[0],
                "size_files": int(con.execute(
                    "SELECT COALESCE(sum(size), 0) FROM _files_s WHERE src_id = ?",
                    [S]).fetchone()[0]),
                "size_filev": int(con.execute(
                    "SELECT COALESCE(sum(size), 0) FROM _fv_final").fetchone()[0]),
            }
            per_source[key] = row
            if (row["paths"] != row["pathx"] or row["files"] != row["filev"]
                    or row["size_files"] != row["size_filev"] or row["dup_path_id"]
                    or row["mismatch"] or row["sticky_violations"]):
                raise RuntimeError(f"repo {repo} source {key}: incremental verification "
                                   f"failed: {row}")

            prel = f"pool/pathx/src_id={S}/g{gen}.parquet"
            hrel = f"pool/history/src_id={S}/g{gen}.parquet"
            staged_p = os.path.join(staging, f"pathx_{S}.parquet")
            staged_h = os.path.join(staging, f"history_{S}.parquet")
            con.execute(
                f"COPY (SELECT path_id, src_id, dir_id, name, ext, size, versions, first_seen, "
                f"last_seen, in_latest, cid_id, physical, exclusive, full_path "
                f"FROM _px_final ORDER BY dir_id, name) TO '{_fwd(staged_p)}' ({_copy_opts()})")
            con.execute(
                f"COPY (SELECT src_id, path_id, snap_id, cid_id, size, mtime, ordinal "
                f"FROM _fv_final ORDER BY path_id, snap_id) TO '{_fwd(staged_h)}' ({_copy_opts()})")
            new_entries[S] = (hrel, prel)
            moves.append((staged_h, hrel))
            moves.append((staged_p, prel))
        con.execute("DROP TABLE IF EXISTS _paths_s")
        con.execute("DROP TABLE IF EXISTS _files_s")
        log.info("repo=%s index per-source mapping (%d source(s)) in %.1fs",
                 repo, len(targets), time.perf_counter() - t_map)
        job["phase"] = "writing"
        perf.lap("write + verify")
        t_tail = time.perf_counter()

        # Small tables and maps. isnaps: a previous row survives only while its snapshot still
        # exists in the current listing (retention expiry / manual delete must not leave
        # phantoms - a phantom keeps the source's snapshot set permanently unequal, i.e. a
        # rebuild every night), unless superseded by this build's walk or belonging to a purged
        # source.
        con.execute(
            """CREATE TABLE isnaps_final AS
               SELECT p.* FROM prev_isnaps p
               WHERE p.source NOT IN (SELECT source FROM _stale_srcs)
                 AND p.snapshot_id IN (SELECT snapshot_id FROM _cur_snaps)
                 AND p.snapshot_id NOT IN (SELECT snapshot_id FROM indexed_snapshots)
               UNION ALL
               SELECT repo, snapshot_id, source, start_time, root_obj, indexed_at,
                      file_count FROM indexed_snapshots""")
        # Layer 2 already produced these in the integer-keyed shape the artifacts are published
        # in, so the tail copies them straight through. `iphys` comes from `tpart` (every piece of
        # a split object, ordinal 0 included), so it needs no carry-forward from the previous
        # generation: the tree it is derived from is whole.
        con.execute("CREATE TABLE csize_final AS SELECT cid_id, packed FROM csize_new")
        con.execute("CREATE TABLE crefs_final AS SELECT cid_id, refs FROM crefs_new")
        con.execute("CREATE TABLE iphys_final AS SELECT cid_id, packed FROM iphys_new")

        # Assemble the manifest: unchanged sources by reference, rewritten sources fresh. With no
        # previous generation there is nothing to carry, so every source is in new_entries.
        px_map = _file_src_map(con, root, prev_art["pathx"]) if prev is not None else {}
        fv_map = _file_src_map(con, root, prev_art["filev"]) if prev is not None else {}
        target_keys = {k for k, _m, _a in targets}
        carried_ids = sorted({int(src_of[k]) for k in srcs
                              if k not in target_keys and src_of.get(k) is not None})
        artifacts_out: dict[str, list] = {"pathx": [], "filev": []}
        for sid in sorted(set(carried_ids) | set(new_entries)):
            if sid in new_entries:
                hrel, prel = new_entries[sid]
                artifacts_out["filev"].append(hrel)
                artifacts_out["pathx"].append(prel)
            else:
                artifacts_out["filev"].extend(fv_map.get(sid, []))
                artifacts_out["pathx"].extend(px_map.get(sid, []))
        if not artifacts_out["filev"]:
            # The repo holds no sources at all. The generation must still be OPENABLE, so write
            # empty but correctly-typed per-source artifacts rather than leaving the reader with
            # a view over nothing. (Carrying the previous empty pair works too, but a first
            # build of an empty repo has none to carry.)
            if prev is not None:
                artifacts_out["filev"] = list(prev_art["filev"])
                artifacts_out["pathx"] = list(prev_art["pathx"])
            else:
                for name, cols in (("filev", "src_id, path_id, snap_id, cid_id, size, mtime, "
                                             "ordinal"),
                                   ("pathx", "path_id, src_id, dir_id, name, ext, size, versions, "
                                             "first_seen, last_seen, in_latest, cid_id, physical, "
                                             "exclusive, full_path")):
                    src = "prev_hist" if name == "filev" else "prev_pathx"
                    staged = os.path.join(staging, f"{name}_empty.parquet")
                    rel = (f"pool/history/src_id=0/g{gen}.parquet" if name == "filev"
                           else f"pool/pathx/src_id=0/g{gen}.parquet")
                    con.execute(f"COPY (SELECT {cols} FROM {src} WHERE false) "
                                f"TO '{_fwd(staged)}' ({_copy_opts()})")
                    artifacts_out[name] = [rel]
                    moves.append((staged, rel))
        for name in ("src", "dir", "anc", "cid", "snap"):
            rel = f"pool/{name}/g{gen}.parquet"
            staged = os.path.join(staging, f"{name}.parquet")
            order = {"src": "src_id", "dir": "src_id, dir_path", "anc": "depth, dir_id",
                     "cid": "cid_id", "snap": "snap_id"}[name]
            con.execute(f"COPY (SELECT * FROM {name} ORDER BY {order}) "
                        f"TO '{_fwd(staged)}' ({_copy_opts()})")
            artifacts_out[name] = [rel]
            moves.append((staged, rel))
        for name, table in (("csize", "csize_final"), ("crefs", "crefs_final"),
                            ("iphys", "iphys_final")):
            rel = f"pool/maps/{name}/g{gen}.parquet"
            staged = os.path.join(staging, f"{name}.parquet")
            con.execute(f"COPY (SELECT * FROM {table} ORDER BY cid_id) "
                        f"TO '{_fwd(staged)}' ({_copy_opts()})")
            artifacts_out[name] = [rel]
            moves.append((staged, rel))
        staged = os.path.join(staging, "isnaps.parquet")
        con.execute(f"COPY (SELECT * FROM isnaps_final ORDER BY snapshot_id) "
                    f"TO '{_fwd(staged)}' ({_copy_opts()})")
        # Forward slashes like every other artifact: manifest paths are the portable relative form,
        # not host paths. os.path.join here emitted backslashes on Windows, which GC then failed to
        # match against its own keep-set (it normalizes now, but the manifest is the source).
        artifacts_out["isnaps"] = [f"pool/small/isnaps/g{gen}.parquet"]
        moves.append((staged, artifacts_out["isnaps"][0]))
        if prev is not None:
            artifacts_out["sanalysis"] = list(prev_art["sanalysis"])  # carried by reference
        else:
            staged = os.path.join(staging, "sanalysis.parquet")
            con.execute(f"COPY (SELECT * FROM snapshot_analysis WHERE false) "
                        f"TO '{_fwd(staged)}' ({_copy_opts()})")
            artifacts_out["sanalysis"] = [f"pool/small/sanalysis/g{gen}.parquet"]
            moves.append((staged, artifacts_out["sanalysis"][0]))

        # Layer 1 travels with the generation, so the tree a reader sees is always the one the
        # index was built from.
        tree_art, tree_moves = _write_tree(con, staging, gen)
        artifacts_out.update(tree_art)
        moves.extend(tree_moves)

        # Global acceptance over the final artifact lists (staged files by their staging path).
        staged_of_rel = {rel: staged for staged, rel in moves}
        checks: dict = {"sources": per_source, "stale": stale, "tree": tree_stats,
                        "allocated": {s: alloc.mark(s) - hw0[s] for s in ids.Allocator.SPACES},
                        "orphan_dir": con.execute(
                            "SELECT count(*) FROM dir WHERE depth > 1 AND parent_id IS NULL"
                        ).fetchone()[0]}
        if checks["orphan_dir"]:
            raise RuntimeError(f"repo {repo}: incremental verification failed: {checks}")

        def _abs_any(rel: str) -> str:
            return staged_of_rel.get(rel) or os.path.join(root, rel)

        fl = [_fwd(_abs_any(r)) for r in artifacts_out["pathx"]]
        n_paths = con.execute(
            f"SELECT count(*) FROM read_parquet({crypto.parquet_read_list(fl)})").fetchone()[0]
        fl = [_fwd(_abs_any(r)) for r in artifacts_out["filev"]]
        n_filev = con.execute(
            f"SELECT count(*) FROM read_parquet({crypto.parquet_read_list(fl)})").fetchone()[0]
        stats = {"paths": n_paths, "file_versions": n_filev}
        checks["stats"] = stats
        perf.count(sources=len(srcs), rebuilt_sources=len(targets), paths=n_paths,
                   file_versions=n_filev, dirs=con.execute("SELECT count(*) FROM dir").fetchone()[0],
                   content_ids=con.execute("SELECT count(*) FROM cid").fetchone()[0],
                   snapshots=con.execute("SELECT count(*) FROM snap").fetchone()[0])

        # Encryption self-check, before anything is published: every file this generation will
        # name must be in the state the manifest is about to declare. A writer that missed the
        # key produces a file that is perfectly readable and silently NOT encrypted, which is
        # the one failure the rest of the pipeline cannot notice.
        state = crypto.writes_encrypted()
        named = [_abs_any(r) for rels in artifacts_out.values() for r in rels]
        misplaced = crypto.wrong_state(named, state)
        if misplaced:
            raise RuntimeError(f"repo {repo}: {len(misplaced)} of {len(named)} artifact file(s) "
                               f"are not in the expected encryption state: {misplaced[:3]}")
        checks["encryption"] = {"state": "encrypted" if state else "plain", "files": len(named)}

        sources = {k: {"key": k, "snapshots": len(v), "updated_gen": gen}
                   for k, v in srcs.items()}
        log.info("repo=%s index tail (maps + copy + verify) in %.1fs; build total %.1fs",
                 repo, time.perf_counter() - t_tail, time.perf_counter() - t0)
        return {"generation": gen, "repo": repo, "schema_version": _MANIFEST_VERSION,
                "counters": alloc.counters(), "artifacts": artifacts_out,
                "sources": sources, "stats": stats,
                # PUBLIC keys on purpose: `_`-prefixed ones are stripped before the manifest is
                # written, and the next build has to read both of these. Capped so a repository
                # with a lot of bad content cannot bloat the manifest.
                "tree_incomplete": tree_stats["incomplete"][:_MAX_UNREADABLE],
                "tree_forced_for": forced_for[:_MAX_UNREADABLE],
                "_checks": checks, "_moves": moves, "_mode": "incremental"}
    finally:
        _close_scratch(scratch, con)


