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

import duckdb
import pyarrow as pa

from config import CACHEDIR

from ..core import paths as pathrules
from ..core.applog import get_logger
from ..core.cache import cache_db
from ..core.sizemap import load_sizemap
from ..core.sources import sources_index
from . import ids
from .builder import (_append_set, _backfill_source_first_seen, _build_source, _drop_indexes,
                      _extend_source, backfill_first_seen)
from .physical import materialize_physical, materialize_refcount
from .schema import init_schema

log = get_logger("store")

_SEP = chr(92)
_SEPS = ("/", _SEP)   # the only separators core.paths produces (POSIX root vs drive/UNC/unknown)
_ZSTD = "FORMAT parquet, COMPRESSION zstd"
_MANIFEST_VERSION = 2  # a published manifest below this forces the full rebuild path


def _fwd(p: str) -> str:
    return p.replace("\\", "/").replace("'", "''")


def _sep_table(con) -> None:
    """(src_id, sep) for every source, from the per-source path rules, as a TEMP table so the
    published ``src`` artifact keeps its schema. DuckDB's array_to_string only takes a CONSTANT
    separator, so the transform's SQL switches on the two real values (``_ajts``); a third
    separator would silently build wrong directory rows, so it fails loudly here instead."""
    rows = con.execute("SELECT src_id, source FROM src ORDER BY src_id").fetchall()
    seps = [pathrules.sep(source) for _sid, source in rows]
    bad = sorted({s for s in seps if s not in _SEPS})
    if bad:
        raise RuntimeError(f"source separator(s) {bad!r} are not supported by the transform")
    tbl = pa.table({"src_id": pa.array([r[0] for r in rows], pa.int16()),
                    "sep": pa.array(seps, pa.string())})
    con.register("_seps_arrow", tbl)
    con.execute("CREATE OR REPLACE TEMP TABLE _seps AS SELECT src_id, sep FROM _seps_arrow")
    con.unregister("_seps_arrow")


def _transform(con: duckdb.DuckDBPyConnection) -> None:
    """The current schema -> skinny integer-id tables. Ported verbatim from build_skinny.py
    (the same transform that produced the byte-verified C7p artifacts); the source tables live
    in the same database here, so the ``s.`` prefix is dropped. Do not redesign it.

    Paths are split and rebuilt with each SOURCE's own separator (``_seps``, from the per-source
    path rules) - a repo can hold POSIX and Windows sources side by side. ``_ajts`` exists
    because array_to_string rejects a column separator; it switches on the two real values."""
    con.execute("CREATE TABLE src AS SELECT row_number() OVER (ORDER BY source)::SMALLINT AS src_id, "
                "source FROM (SELECT DISTINCT source FROM paths)")
    _sep_table(con)
    con.execute("CREATE OR REPLACE TEMP MACRO _ajts(l, sep) AS CASE WHEN sep = '/' "
                f"THEN array_to_string(l, '/') ELSE array_to_string(l, '{_SEP}') END")
    con.execute("""CREATE TEMPORARY TABLE _dseg AS
        SELECT DISTINCT t.src_id, _ajts(t.parts[1:g.k], t.sep) AS dir_path, g.k::SMALLINT AS depth
        FROM (SELECT sr.src_id, sp.sep, string_split(p.full_path, sp.sep) AS parts
              FROM paths p
              JOIN src sr ON sr.source = p.source
              JOIN _seps sp ON sp.src_id = sr.src_id) t,
             unnest(generate_series(1, len(t.parts) - 1)) AS g(k)""")
    con.execute("""CREATE TABLE dir AS
        SELECT row_number() OVER (ORDER BY d.src_id, d.dir_path)::INTEGER AS dir_id, d.src_id,
               d.dir_path, d.depth,
               _ajts(string_split(d.dir_path, sp.sep)[len(string_split(d.dir_path, sp.sep)):],
                     sp.sep) AS name,
               CASE WHEN d.depth <= 1 THEN NULL
                    ELSE _ajts(string_split(d.dir_path, sp.sep)[1:d.depth - 1], sp.sep) END
                   AS parent_path
        FROM _dseg d JOIN _seps sp ON sp.src_id = d.src_id""")
    con.execute("ALTER TABLE dir ADD COLUMN parent_id INTEGER")
    con.execute("UPDATE dir SET parent_id = p.dir_id FROM dir p "
                "WHERE p.src_id = dir.src_id AND p.dir_path = dir.parent_path")
    con.execute("ALTER TABLE dir DROP COLUMN parent_path")
    con.execute("""CREATE TABLE anc AS
        WITH RECURSIVE up(dir_id, anc_id) AS (
            SELECT dir_id, dir_id FROM dir
            UNION ALL
            SELECT u.dir_id, d.parent_id FROM up u JOIN dir d ON d.dir_id = u.anc_id
            WHERE d.parent_id IS NOT NULL)
        SELECT u.dir_id, a.depth, u.anc_id FROM up u JOIN dir a ON a.dir_id = u.anc_id""")
    con.execute("""CREATE TABLE cid AS
        SELECT row_number() OVER (ORDER BY c)::INTEGER AS cid_id, c AS content_id FROM (
            SELECT content_id AS c FROM files WHERE content_id IS NOT NULL
            UNION SELECT cid FROM content_sizes
            UNION SELECT oid FROM indirect_phys)""")
    con.execute("""CREATE TABLE snap AS
        SELECT row_number() OVER (ORDER BY start_time, snapshot_id)::INTEGER AS snap_id,
               snapshot_id, source, start_time FROM (
            SELECT snapshot_id, any_value(source) AS source, min(start_time) AS start_time
            FROM files GROUP BY snapshot_id)""")
    con.execute("ALTER TABLE snap ADD COLUMN src_id SMALLINT")
    con.execute("UPDATE snap SET src_id = sr.src_id FROM src sr WHERE sr.source = snap.source")
    con.execute("ALTER TABLE snap DROP COLUMN source")
    con.execute("""CREATE TABLE pathx AS
        SELECT row_number() OVER (ORDER BY p.source, p.full_path)::INTEGER AS path_id,
               sr.src_id, d.dir_id, p.name, p.ext, p.size, p.versions::INTEGER AS versions,
               p.first_seen, p.last_seen, p.in_latest, c.cid_id, p.physical, p.exclusive,
               p.full_path
        FROM paths p
        JOIN src sr ON sr.source = p.source
        JOIN _seps sp ON sp.src_id = sr.src_id
        JOIN dir d ON d.src_id = sr.src_id AND d.dir_path = _ajts(
             string_split(p.full_path, sp.sep)[1:len(string_split(p.full_path, sp.sep)) - 1],
             sp.sep)
        LEFT JOIN cid c ON c.content_id = p.content_id""")
    con.execute("""CREATE TABLE filev AS
        SELECT px.path_id, sn.snap_id, c.cid_id, f.size, f.mtime, f.ordinal
        FROM files f
        JOIN src sr ON sr.source = f.source
        JOIN _seps sp ON sp.src_id = sr.src_id
        JOIN dir d ON d.src_id = sr.src_id AND d.dir_path = _ajts(
             string_split(f.full_path, sp.sep)[1:len(string_split(f.full_path, sp.sep)) - 1],
             sp.sep)
        JOIN pathx px ON px.src_id = sr.src_id AND px.dir_id = d.dir_id
             AND px.name = _ajts(string_split(f.full_path, sp.sep)
                 [len(string_split(f.full_path, sp.sep)):], sp.sep)
        JOIN snap sn ON sn.snapshot_id = f.snapshot_id
        LEFT JOIN cid c ON c.content_id = f.content_id
        ORDER BY px.path_id, sn.snap_id""")
    con.execute("CREATE TABLE csize AS SELECT c.cid_id, m.packed FROM content_sizes m "
                "JOIN cid c ON c.content_id = m.cid")
    con.execute("CREATE TABLE crefs AS SELECT c.cid_id, m.refs::INTEGER AS refs FROM content_refs m "
                "JOIN cid c ON c.content_id = m.cid")
    con.execute("CREATE TABLE iphys AS SELECT c.cid_id, m.packed FROM indirect_phys m "
                "JOIN cid c ON c.content_id = m.oid")
    con.execute("CREATE TABLE sanalysis AS SELECT * FROM snapshot_analysis")
    con.execute("CREATE TABLE isnaps AS SELECT * FROM indexed_snapshots")


def _verify(con: duckdb.DuckDBPyConnection) -> tuple[bool, dict]:
    """Reconstruct every path and diff against the source - a wrong transform fails loudly here
    instead of shipping a subtly short index. Same checks as build_skinny.py."""
    checks = {}
    checks["src_paths"] = con.execute("SELECT count(*) FROM paths").fetchone()[0]
    checks["pathx"] = con.execute("SELECT count(*) FROM pathx").fetchone()[0]
    checks["src_files"] = con.execute("SELECT count(*) FROM files").fetchone()[0]
    checks["filev"] = con.execute("SELECT count(*) FROM filev").fetchone()[0]
    checks["path_mismatch"] = con.execute(
        f"""SELECT count(*) FROM pathx px
            JOIN dir d ON d.dir_id = px.dir_id
            JOIN src sr ON sr.src_id = px.src_id
            JOIN _seps sp ON sp.src_id = sr.src_id
            LEFT JOIN paths p ON p.source = sr.source
                 AND p.full_path = d.dir_path || sp.sep || px.name
            WHERE p.full_path IS NULL""").fetchone()[0]
    checks["size_src"] = con.execute("SELECT sum(size) FROM files").fetchone()[0]
    checks["size_new"] = con.execute("SELECT sum(size) FROM filev").fetchone()[0]
    checks["orphan_dir"] = con.execute(
        "SELECT count(*) FROM dir WHERE depth > 1 AND parent_id IS NULL").fetchone()[0]
    ok = (checks["pathx"] == checks["src_paths"]
          and checks["filev"] == checks["src_files"]
          and checks["path_mismatch"] == 0
          and checks["orphan_dir"] == 0
          and checks["size_src"] == checks["size_new"])
    return ok, checks


def _write(con: duckdb.DuckDBPyConnection, staging: str, gen: int) -> tuple[dict, list]:
    """Write each artifact to staging; return (manifest artifact lists, staged->final moves).
    Paths are repo-relative (``pool/...``); publish.py moves the staged files into ``pool/`` and
    swaps the manifest, so a reader only ever sees a complete generation."""
    fwd = lambda p: p.replace("\\", "/").replace("'", "''")  # noqa: E731
    artifacts: dict[str, list[str]] = {}
    moves: list[tuple[str, str]] = []
    for r in con.execute("SELECT src_id FROM src ORDER BY src_id").fetchall():
        n = r[0]
        h = os.path.join(staging, f"history_{n}.parquet")
        p = os.path.join(staging, f"pathx_{n}.parquet")
        con.execute(
            f"COPY (SELECT p.src_id, f.path_id, f.snap_id, f.cid_id, f.size, f.mtime, f.ordinal "
            f"FROM filev f JOIN pathx p USING (path_id) WHERE p.src_id = {n} "
            f"ORDER BY f.path_id, f.snap_id) TO '{fwd(h)}' ({_ZSTD})")
        con.execute(f"COPY (SELECT * FROM pathx WHERE src_id = {n} ORDER BY dir_id, name) "
                    f"TO '{fwd(p)}' ({_ZSTD})")
        hrel = f"pool/history/src_id={n}/g{gen}.parquet"
        prel = f"pool/pathx/src_id={n}/g{gen}.parquet"
        artifacts.setdefault("filev", []).append(hrel)
        artifacts.setdefault("pathx", []).append(prel)
        moves += [(h, hrel), (p, prel)]
    if "filev" not in artifacts:
        # A repo with no sources (fresh install, zero snapshots) must still publish an openable
        # generation: the store's compatibility views reference these tables. Empty but
        # correctly-typed files, written under src_id=0 so the incremental path's file->source
        # attribution finds a name to attribute (and no real source ever claims it).
        h = os.path.join(staging, "history_empty.parquet")
        p = os.path.join(staging, "pathx_empty.parquet")
        con.execute(f"COPY (SELECT p.src_id, f.path_id, f.snap_id, f.cid_id, f.size, f.mtime, "
                    f"f.ordinal FROM filev f JOIN pathx p USING (path_id) WHERE false) "
                    f"TO '{fwd(h)}' ({_ZSTD})")
        con.execute(f"COPY (SELECT * FROM pathx WHERE false) TO '{fwd(p)}' ({_ZSTD})")
        hrel = f"pool/history/src_id=0/g{gen}.parquet"
        prel = f"pool/pathx/src_id=0/g{gen}.parquet"
        artifacts["filev"] = [hrel]
        artifacts["pathx"] = [prel]
        moves += [(h, hrel), (p, prel)]
    for name, cols in (("src", "src_id"), ("dir", "src_id, dir_path"), ("anc", "depth, dir_id"),
                       ("cid", "cid_id"), ("snap", "snap_id")):
        out = os.path.join(staging, f"{name}.parquet")
        con.execute(f"COPY (SELECT * FROM {name} ORDER BY {cols}) TO '{fwd(out)}' ({_ZSTD})")
        rel = f"pool/{name}/g{gen}.parquet"
        artifacts[name] = [rel]
        moves.append((out, rel))
    for name in ("csize", "crefs", "iphys"):
        out = os.path.join(staging, f"{name}.parquet")
        con.execute(f"COPY (SELECT * FROM {name} ORDER BY cid_id) TO '{fwd(out)}' ({_ZSTD})")
        rel = f"pool/maps/{name}/g{gen}.parquet"
        artifacts[name] = [rel]
        moves.append((out, rel))
    for name in ("sanalysis", "isnaps"):
        out = os.path.join(staging, f"{name}.parquet")
        con.execute(f"COPY (SELECT * FROM {name}) TO '{fwd(out)}' ({_ZSTD})")
        rel = f"pool/small/{name}/g{gen}.parquet"
        artifacts[name] = [rel]
        moves.append((out, rel))
    return artifacts, moves


def _scratch(staging: str) -> tuple[str, duckdb.DuckDBPyConnection]:
    """A fresh scratch DuckDB in ``_staging/`` - spilled to disk, closed and deleted after."""
    scratch = os.path.join(staging, "build.duckdb")
    for suffix in ("", ".wal"):
        if os.path.exists(scratch + suffix):
            os.remove(scratch + suffix)
    con = duckdb.connect(scratch)
    con.execute("SET memory_limit='%s'" % os.environ.get("KOPIALENS_DUCKDB_MEMORY", "3GB"))
    tmp = os.path.join(staging, "tmp")
    os.makedirs(tmp, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp.replace(chr(92), chr(47))}'")
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
      files -> full rebuild from the warm walk cache (``_build_full``).
    - otherwise -> per-source update (``_build_incremental``): unchanged sources carried by
      reference, only changed sources re-walked.

    ``gen`` is bumped past the published generation either way, so artifact file names are
    never reused while a reader can hold them. ``force`` names sources to rebuild even when
    their snapshot set is unchanged (manual rebuild)."""
    job = {} if job is None else job
    prev = _read_prev_manifest(root)
    if prev is not None:
        gen = max(int(gen or 1), int(prev.get("generation") or 0) + 1)
        if (not full and prev.get("schema_version") == _MANIFEST_VERSION
                and _manifest_usable(root, prev)):
            return _build_incremental(repo, root, prev, gen, job, frozenset(force or ()))
    return _build_full(repo, root, gen, job)


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
    fl = ", ".join("'%s'" % _fwd(os.path.join(root, r)) for r in rels)
    for sid, fn in con.execute(
            f"SELECT DISTINCT src_id, filename FROM read_parquet([{fl}], filename=true, "
            f"hive_partitioning=false)").fetchall():
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


def _rewalk_ids(snaps: list[dict], new_snaps: list[dict]) -> set[str]:
    """The snapshots an append re-walks: the new ones plus the previous latest - the same
    selection ``builder._extend_source`` makes."""
    new_ids = {s["id"] for s in new_snaps}
    snaps_sorted = sorted(snaps, key=lambda s: s.get("startTime", "") or "", reverse=True)
    prev_latest = next((s for s in snaps_sorted if s["id"] not in new_ids), None)
    return {s["id"] for s in snaps_sorted
            if s["id"] in new_ids or (prev_latest and s["id"] == prev_latest["id"])}


def _preload_retained(con, repo: str, key: str, sid: int, rewalk: set[str]) -> None:
    """Append path: load the source's RETAINED history rows (snapshots the append will not
    re-walk) into the scratch as old-schema ``files`` rows, so the paths aggregate recomputes
    over the source's FULL version set - ``versions``, ``last_seen`` and the
    arg_max-by-start_time latest version stay correct even though only the new snapshots were
    walked. Rows for re-walked snapshots are excluded; the walk re-emits those."""
    ph = ",".join("?" * len(rewalk))
    miss = con.execute(
        """SELECT count(*) FROM prev_hist h
           LEFT JOIN cid_rev c ON c.cid_id = h.cid_id
           WHERE h.src_id = ? AND h.cid_id IS NOT NULL AND c.content_id IS NULL""",
        [sid]).fetchone()[0]
    if miss:
        raise RuntimeError(
            f"repo {repo} source {key}: {miss} retained history row(s) reference a content id "
            "missing from the previous cid dictionary - carrying them forward would silently "
            "corrupt the content mapping. Run a full rebuild.")
    con.execute(
        f"""INSERT INTO files (repo, source, full_path, name, size, content_id, snapshot_id,
                              start_time, ordinal, mtime)
        SELECT ?, ?, px.full_path, px.name, h.size, c.content_id,
               sn.snapshot_id, sn.start_time, h.ordinal, h.mtime
        FROM prev_hist h
        JOIN prev_pathx px ON px.path_id = h.path_id
        JOIN snap sn ON sn.snap_id = h.snap_id
        LEFT JOIN cid_rev c ON c.cid_id = h.cid_id
        WHERE h.src_id = ? AND sn.snapshot_id NOT IN ({ph})""",
        [repo, key, sid, *rewalk])


def _build_full(repo: str, root: str, gen: int, job: dict) -> dict:
    """Full rebuild: produce a Parquet generation under ``root`` and return the manifest body.
    Does not write manifest.json - that is publish.py's atomic job."""
    pool = os.path.join(root, "pool")
    staging = os.path.join(root, "_staging")
    os.makedirs(pool, exist_ok=True)
    os.makedirs(staging, exist_ok=True)
    srcs = sources_index(repo)
    job.update({"total": sum(len(v) for v in srcs.values()), "indexed": 0,
                "targets": len(srcs), "current_source": None, "error": None})

    scratch, con = _scratch(staging)
    db = cache_db(repo)
    try:
        init_schema(con)
        _drop_indexes(con)  # scratch dies after the build; bulk inserts are faster unindexed
        for key in sorted(srcs):
            job["current_source"] = key
            _build_source(con, db, repo, key, srcs[key], job)
        job["current_source"] = "(first-seen backfill)"
        backfill_first_seen(con, db, repo, job)
        job["current_source"] = "(physical + refcount)"
        materialize_physical(con, repo)
        count, _union = _refcount(repo, db)
        materialize_refcount(con, repo, count)
        job["current_source"] = "(transform)"
        _transform(con)
        ok, checks = _verify(con)
        if not ok:
            raise RuntimeError(f"transform verification failed: {checks}")
        counters = {
            "src_id": int(con.execute("SELECT max(src_id) FROM src").fetchone()[0] or 0),
            "dir_id": int(con.execute("SELECT max(dir_id) FROM dir").fetchone()[0] or 0),
            "path_id": int(con.execute("SELECT max(path_id) FROM pathx").fetchone()[0] or 0),
            "cid_id": int(con.execute("SELECT max(cid_id) FROM cid").fetchone()[0] or 0),
            "snap_id": int(con.execute("SELECT max(snap_id) FROM snap").fetchone()[0] or 0),
        }
        artifacts, moves = _write(con, staging, gen)
    finally:
        _close_scratch(scratch, con)

    sources = {k: {"key": k, "snapshots": len(v)} for k, v in srcs.items()}
    return {"generation": gen, "repo": repo, "schema_version": _MANIFEST_VERSION,
            "counters": counters, "artifacts": artifacts, "sources": sources,
            "stats": {"paths": checks["pathx"], "file_versions": checks["filev"]},
            "_checks": checks, "_moves": moves}


def _build_incremental(repo: str, root: str, prev: dict, gen: int, job: dict,
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
    (accepted, ~40 MB); ``sanalysis`` is carried by reference unchanged."""
    staging = os.path.join(root, "_staging")
    os.makedirs(staging, exist_ok=True)
    srcs = sources_index(repo)
    prev_sources = set(prev.get("sources") or ())
    if not srcs and prev_sources:
        raise RuntimeError(
            f"repo {repo}: the snapshot listing came back empty while the published "
            f"generation holds {len(prev_sources)} source(s) - refusing to treat that as a "
            "removal. The repository is most likely unreachable; the current generation is "
            "untouched.")
    scratch, con = _scratch(staging)
    db = cache_db(repo)
    prev_art = prev["artifacts"]
    moves: list[tuple[str, str]] = []
    try:
        init_schema(con)
        _drop_indexes(con)

        # The previous generation's dictionaries, loaded as scratch tables - new rows are
        # appended and the whole tables are rewritten as this generation's dict artifacts.
        # hive_partitioning=false: the pool paths look like `src_id=N/`, and DuckDB's
        # auto-detection would let the DIRECTORY name override the file's own src_id column.
        def _read_prev_art(name: str, out: str) -> None:
            fl = ", ".join("'%s'" % _fwd(os.path.join(root, r)) for r in prev_art[name])
            con.execute(f"CREATE TABLE {out} AS SELECT * FROM "
                        f"read_parquet([{fl}], hive_partitioning=false)")

        for name in ("src", "dir", "anc", "cid", "snap"):
            _read_prev_art(name, name)
        _read_prev_art("isnaps", "prev_isnaps")
        con.execute("CREATE TABLE src_rev AS SELECT source, src_id FROM src")
        con.execute("CREATE TABLE cid_rev AS SELECT content_id, cid_id FROM cid")
        con.execute("CREATE TABLE snap_rev AS SELECT snapshot_id, snap_id FROM snap")

        # Classification - the _run_build rules, against the published generation.
        prev_sets: dict[str, set] = {}
        for source, sid in con.execute("SELECT source, snapshot_id FROM prev_isnaps").fetchall():
            prev_sets.setdefault(source, set()).add(sid)
        stale = sorted((prev_sources | set(prev_sets)) - set(srcs))
        targets: list[tuple[str, str, list | None]] = []
        for key in sorted(srcs):
            cur_ids = {s["id"] for s in srcs[key]}
            if key in prev_sources and key not in force and cur_ids == prev_sets.get(key, set()):
                continue  # carried forward by reference
            ap = (_append_set(srcs[key], prev_sets.get(key, set()))
                  if (key in prev_sources and key not in force) else None)
            targets.append((key, "append" if ap else "rebuild", ap))
        job.update({"total": sum(len(srcs[k]) for k, _m, _a in targets), "indexed": 0,
                    "targets": len(targets), "current_source": None, "error": None})

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

        # The previous per-source artifacts for changed sources: id/first_seen carry and the
        # append's retained rows. src_id=-1 matches nothing when the list is empty, which also
        # yields an empty clone with the published schema.
        src_of = dict(con.execute("SELECT source, src_id FROM src_rev").fetchall())
        tgt_ids = sorted({int(src_of[k]) for k, _m, _a in targets if src_of.get(k) is not None})
        ap_ids = sorted({int(src_of[k]) for k, m, _a in targets
                         if m == "append" and src_of.get(k) is not None})
        fl = ", ".join("'%s'" % _fwd(os.path.join(root, r)) for r in prev_art["pathx"])
        con.execute(f"CREATE TABLE prev_pathx AS SELECT * FROM "
                    f"read_parquet([{fl}], hive_partitioning=false) "
                    f"WHERE src_id IN ({','.join('?' * len(tgt_ids or [-1]))})",
                    tgt_ids or [-1])
        fl = ", ".join("'%s'" % _fwd(os.path.join(root, r)) for r in prev_art["filev"])
        con.execute(f"CREATE TABLE prev_hist AS SELECT * FROM "
                    f"read_parquet([{fl}], hive_partitioning=false) "
                    f"WHERE src_id IN ({','.join('?' * len(ap_ids or [-1]))})",
                    ap_ids or [-1])

        # Walk what changed. Append preloads the retained rows first so the paths aggregate
        # sees the source's full version set; a rebuild starts from an empty scratch.
        for key, mode, ap in targets:
            job["current_source"] = key
            if mode == "append":
                _preload_retained(con, repo, key, int(src_of[key]), _rewalk_ids(srcs[key], ap))
                _extend_source(con, db, repo, key, srcs[key], ap, job)
            else:
                _build_source(con, db, repo, key, srcs[key], job)

        # Sources NEW to the index have no previous artifact to carry sticky first_seen from,
        # and the delta-built history alone dates every untouched file at its newest snapshot.
        # Run the oldest-first backfill for exactly those sources, before the per-source
        # mapping below reads the paths aggregate. Existing sources keep their carried dates.
        new_keys = [key for key, _m, _a in targets if key not in prev_sources]
        if new_keys:
            job["current_source"] = "(first-seen backfill)"
            for key in new_keys:
                _backfill_source_first_seen(con, db, repo, key, srcs[key])

        # Sources new to the dictionary, then the physical fill and the global refcount.
        tbl = pa.table({"source": sorted(srcs)})
        con.register("_cur_keys", tbl)
        ids.anti_join(con, "_src_new", "SELECT source FROM _cur_keys",
                      "SELECT source FROM src", ["source"])
        con.unregister("_cur_keys")
        alloc = ids.Allocator(prev["counters"])
        hw0 = alloc.counters()
        alloc.assign(con, "_src_alloc", "SELECT source FROM _src_new", "source", "src_id",
                     id_col="src_id", id_type="SMALLINT")
        con.execute("INSERT INTO src SELECT src_id, source FROM _src_alloc")
        con.execute("INSERT INTO src_rev SELECT source, src_id FROM _src_alloc")
        src_of = dict(con.execute("SELECT source, src_id FROM src_rev").fetchall())

        job["current_source"] = "(physical + refcount)"
        materialize_physical(con, repo)
        count, _union = _refcount(repo, db)

        # The cid namespace must cover everything the maps will join against - the full
        # transform's definition (files/paths contents + the whole sizemap + indirect objects).
        # Sizing the dictionary from file rows alone drops split files' underlying chunk ids,
        # and a chunk missing from crefs reads as zero references, i.e. "safe to delete".
        ids.anti_join(con, "_cid_new",
                      "SELECT DISTINCT content_id FROM ("
                      "SELECT content_id FROM files WHERE content_id IS NOT NULL "
                      "UNION SELECT content_id FROM paths WHERE content_id IS NOT NULL "
                      "UNION SELECT cid FROM content_sizes "
                      "UNION SELECT oid FROM indirect_phys)",
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

        # Per-source mapping: carry path ids by (identity, name), sticky first_seen from the
        # immutable prev artifact, allocate the residue, rewrite the source's two artifacts.
        # (ident, name) rather than ident alone: a case-only rename on a case-insensitive
        # source leaves TWO display rows sharing one identity while both spellings' file rows
        # exist, and each keeps its own id.
        new_entries: dict[int, tuple[str, str]] = {}
        per_source: dict[str, dict] = {}
        for key, mode, ap in targets:
            S = int(src_of[key])
            sep = pathrules.sep(key)
            ident_raw = pathrules.ident_sql(key)
            ident_px = pathrules.ident_sql(key, "px.full_path")
            job["current_source"] = key

            con.execute(
                f"CREATE OR REPLACE TEMPORARY TABLE _old_seen AS "
                f"SELECT {ident_raw} AS ident, min(first_seen) AS fs FROM prev_pathx "
                f"WHERE src_id = ? AND first_seen IS NOT NULL GROUP BY 1", [S])
            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _px AS
                    SELECT p.full_path, p.name, p.ext, p.size, p.versions,
                           least(coalesce(o.fs, p.first_seen), p.first_seen) AS first_seen,
                           p.last_seen, p.in_latest, p.content_id, p.physical
                    FROM paths p
                    LEFT JOIN _old_seen o ON o.ident = {pathrules.ident_sql(key, 'p.full_path')}
                    WHERE p.repo = ? AND p.source = ?""", [repo, key])

            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _dseg AS
                    SELECT DISTINCT CAST(? AS SMALLINT) AS src_id,
                           array_to_string(t.parts[1:g.k], '{sep}') AS dir_path,
                           g.k::SMALLINT AS depth
                    FROM (SELECT string_split(full_path, '{sep}') AS parts FROM _px) t,
                         unnest(generate_series(1, len(t.parts) - 1)) AS g(k)""", [S])
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
                f"SELECT {ident_raw} AS ident, name, min(path_id) AS path_id FROM prev_pathx "
                f"WHERE src_id = ? GROUP BY 1, 2", [S])
            ids.anti_join(con, "_px_new",
                          f"SELECT {ident_raw} AS ident, name FROM _px",
                          "SELECT ident, name FROM _px_carry", ["ident", "name"])
            alloc.assign(con, "_px_alloc", "SELECT ident, name FROM _px_new", "ident, name",
                         "path_id", id_col="path_id")
            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _px_final AS
                    SELECT COALESCE(al.path_id, ca.path_id) AS path_id,
                           CAST(? AS SMALLINT) AS src_id, d.dir_id, px.name, px.ext, px.size,
                           CAST(px.versions AS INTEGER) AS versions, px.first_seen, px.last_seen,
                           px.in_latest, cr.cid_id, px.physical,
                           CAST(NULL AS UBIGINT) AS exclusive, px.full_path
                    FROM _px px
                    LEFT JOIN _px_carry ca ON ca.ident = {ident_px} AND ca.name = px.name
                    LEFT JOIN _px_alloc al ON al.ident = {ident_px} AND al.name = px.name
                    JOIN dir d ON d.src_id = ? AND d.dir_path = array_to_string(
                         string_split(px.full_path, '{sep}')
                             [1:len(string_split(px.full_path, '{sep}')) - 1], '{sep}')
                    LEFT JOIN cid_rev cr ON cr.content_id = px.content_id""", [S, S])

            # The scratch `files` table already holds the source's COMPLETE history - the
            # append's preloaded retained rows plus the walk's fresh rows - mapped through the
            # new dictionaries. Carrying prev_hist rows here as well would duplicate them.
            con.execute(
                f"""CREATE OR REPLACE TEMPORARY TABLE _fv_final AS
                    SELECT CAST(? AS SMALLINT) AS src_id, m.path_id, sm.snap_id, cr.cid_id,
                           f.size, f.mtime, f.ordinal
                    FROM files f
                    JOIN _px_final m ON m.full_path = f.full_path
                    JOIN snap_rev sm ON sm.snapshot_id = f.snapshot_id
                    LEFT JOIN cid_rev cr ON cr.content_id = f.content_id
                    WHERE f.repo = ? AND f.source = ?""", [S, repo, key])

            # Per-source acceptance - the same checks _verify runs on a full rebuild, plus
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
                "files": con.execute("SELECT count(*) FROM files WHERE repo=? AND source=?",
                                     [repo, key]).fetchone()[0],
                "filev": con.execute("SELECT count(*) FROM _fv_final").fetchone()[0],
                "size_files": int(con.execute(
                    "SELECT COALESCE(sum(size), 0) FROM files WHERE repo=? AND source=?",
                    [repo, key]).fetchone()[0]),
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
                f"FROM _px_final ORDER BY dir_id, name) TO '{_fwd(staged_p)}' ({_ZSTD})")
            con.execute(
                f"COPY (SELECT src_id, path_id, snap_id, cid_id, size, mtime, ordinal "
                f"FROM _fv_final ORDER BY path_id, snap_id) TO '{_fwd(staged_h)}' ({_ZSTD})")
            new_entries[S] = (hrel, prel)
            moves.append((staged_h, hrel))
            moves.append((staged_p, prel))

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
        con.execute("CREATE TABLE csize_final AS SELECT c.cid_id, cs.packed "
                    "FROM content_sizes cs JOIN cid c ON c.content_id = cs.cid")
        tbl = pa.table({"content_id": list(count.keys()), "refs": list(count.values())})
        con.register("_rc", tbl)
        con.execute("CREATE TABLE crefs_final AS SELECT c.cid_id, CAST(r.refs AS INTEGER) AS refs "
                    "FROM _rc r JOIN cid c ON c.content_id = r.content_id")
        # Both maps inner-join the dictionary: a content id the join drops would silently lose
        # its packed size (physical reads 0) or its refcount (reads as "safe to delete").
        dropped = con.execute(
            "SELECT (SELECT count(*) FROM content_sizes cs WHERE NOT EXISTS "
            "           (SELECT 1 FROM cid c WHERE c.content_id = cs.cid)) + "
            "       (SELECT count(*) FROM _rc r WHERE NOT EXISTS "
            "           (SELECT 1 FROM cid c WHERE c.content_id = r.content_id))").fetchone()[0]
        con.unregister("_rc")
        if dropped:
            raise RuntimeError(f"repo {repo}: {dropped} content id(s) present in the size/refcount "
                               "maps are missing from the cid dictionary - csize/crefs would "
                               "silently drop them. Run a full rebuild.")
        _read_prev_art("iphys", "prev_iphys")
        con.execute("CREATE TABLE iphys_final AS SELECT cid_id, packed FROM prev_iphys "
                    "UNION ALL SELECT c.cid_id, np.packed FROM indirect_phys np "
                    "JOIN cid_rev c ON c.content_id = np.oid "
                    "WHERE c.cid_id NOT IN (SELECT cid_id FROM prev_iphys)")

        # Assemble the manifest: unchanged sources by reference, rewritten sources fresh.
        px_map = _file_src_map(con, root, prev_art["pathx"])
        fv_map = _file_src_map(con, root, prev_art["filev"])
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
            # The repo still holds no sources (e.g. a no-op nightly on an empty generation):
            # carry the previous generation's empty per-source artifacts so the new manifest
            # stays openable. (A populated repo always carries or rewrites something.)
            artifacts_out["filev"] = list(prev_art["filev"])
            artifacts_out["pathx"] = list(prev_art["pathx"])
        for name in ("src", "dir", "anc", "cid", "snap"):
            rel = f"pool/{name}/g{gen}.parquet"
            staged = os.path.join(staging, f"{name}.parquet")
            order = {"src": "src_id", "dir": "src_id, dir_path", "anc": "depth, dir_id",
                     "cid": "cid_id", "snap": "snap_id"}[name]
            con.execute(f"COPY (SELECT * FROM {name} ORDER BY {order}) "
                        f"TO '{_fwd(staged)}' ({_ZSTD})")
            artifacts_out[name] = [rel]
            moves.append((staged, rel))
        for name, table in (("csize", "csize_final"), ("crefs", "crefs_final"),
                            ("iphys", "iphys_final")):
            rel = f"pool/maps/{name}/g{gen}.parquet"
            staged = os.path.join(staging, f"{name}.parquet")
            con.execute(f"COPY (SELECT * FROM {table} ORDER BY cid_id) "
                        f"TO '{_fwd(staged)}' ({_ZSTD})")
            artifacts_out[name] = [rel]
            moves.append((staged, rel))
        staged = os.path.join(staging, "isnaps.parquet")
        con.execute(f"COPY (SELECT * FROM isnaps_final ORDER BY snapshot_id) "
                    f"TO '{_fwd(staged)}' ({_ZSTD})")
        artifacts_out["isnaps"] = [os.path.join("pool", "small", "isnaps", f"g{gen}.parquet")]
        moves.append((staged, artifacts_out["isnaps"][0]))
        artifacts_out["sanalysis"] = list(prev_art["sanalysis"])  # carried by reference

        # Global acceptance over the final artifact lists (staged files by their staging path).
        staged_of_rel = {rel: staged for staged, rel in moves}
        checks: dict = {"sources": per_source, "stale": stale,
                        "allocated": {s: alloc.mark(s) - hw0[s] for s in ids.Allocator.SPACES},
                        "orphan_dir": con.execute(
                            "SELECT count(*) FROM dir WHERE depth > 1 AND parent_id IS NULL"
                        ).fetchone()[0]}
        if checks["orphan_dir"]:
            raise RuntimeError(f"repo {repo}: incremental verification failed: {checks}")

        def _abs_any(rel: str) -> str:
            return staged_of_rel.get(rel) or os.path.join(root, rel)

        fl = ", ".join("'%s'" % _fwd(_abs_any(r)) for r in artifacts_out["pathx"])
        n_paths = con.execute(f"SELECT count(*) FROM read_parquet([{fl}])").fetchone()[0]
        fl = ", ".join("'%s'" % _fwd(_abs_any(r)) for r in artifacts_out["filev"])
        n_filev = con.execute(f"SELECT count(*) FROM read_parquet([{fl}])").fetchone()[0]
        stats = {"paths": n_paths, "file_versions": n_filev}
        checks["stats"] = stats

        sources = {k: {"key": k, "snapshots": len(v), "updated_gen": gen}
                   for k, v in srcs.items()}
        return {"generation": gen, "repo": repo, "schema_version": _MANIFEST_VERSION,
                "counters": alloc.counters(), "artifacts": artifacts_out,
                "sources": sources, "stats": stats, "_checks": checks, "_moves": moves,
                "_mode": "incremental"}
    finally:
        _close_scratch(scratch, con)


def _refcount(repo: str, db):
    """The global content refcount Counter, from the persisted pkl when it matches the current
    snapshot set, else a fresh streaming build. The live-DB persist inside build_global is
    suppressed - this build owns a scratch index, not the live one."""
    import time
    from ..core import metrics
    srcs = sources_index(repo)
    membership, _ = metrics.split_sources(srcs, "all")
    key = metrics.snaphash(srcs, membership, None, "all")
    hit = metrics._read_refcount_pkl(repo, "all", key)
    if hit is not None:
        return hit
    smap = load_sizemap(repo)
    metrics._LAST_REFCOUNT_WRITE[repo] = time.time()  # block the live-DB write throttle
    _, _, count, union = metrics.build_global(repo, db, smap, mode="all")
    return count, union