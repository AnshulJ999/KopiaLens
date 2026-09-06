"""M1 writer: build a repo's Parquet generation (full rebuild from the warm walk cache).

Reuses the existing DuckDB builder (``builder._build_source`` + ``backfill_first_seen``) against a
SCRATCH database to produce the current ``files``/``paths``/maps, then transforms that into the
skinny integer-id schema (ported from ``experiments/build_skinny.py``), verifies it, and writes
zstd Parquet - per-source ``history``/``pathx`` partitions plus single ``dir``/``anc``/``src``/
``cid``/``snap``/maps files. Returns the manifest's artifact lists and counters.

The walk reads the shared sqlite walk cache and the sizemap pkl; the only NEW analysis is the
schema transform. Nothing here touches the live index or the ``pool/`` tree until ``publish`` moves
the staged files in.
"""

from __future__ import annotations

import os

import duckdb

from config import CACHEDIR

from ..core.applog import get_logger
from ..core.cache import cache_db
from ..core.sizemap import load_sizemap
from ..core.sources import sources_index
from .builder import _build_source, _drop_indexes, backfill_first_seen
from .physical import materialize_physical, materialize_refcount
from .schema import init_schema

log = get_logger("store")

_SEP = chr(92)
_ZSTD = "FORMAT parquet, COMPRESSION zstd"


def _transform(con: duckdb.DuckDBPyConnection) -> None:
    """The current schema -> skinny integer-id tables. Ported verbatim from build_skinny.py
    (the same transform that produced the byte-verified C7p artifacts); the source tables live
    in the same database here, so the ``s.`` prefix is dropped. Do not redesign it."""
    con.execute("CREATE TABLE src AS SELECT row_number() OVER (ORDER BY source)::SMALLINT AS src_id, "
                "source FROM (SELECT DISTINCT source FROM paths)")
    con.execute(f"""CREATE TEMPORARY TABLE _dseg AS
        SELECT DISTINCT sr.src_id, array_to_string(t.parts[1:g.k], '{_SEP}') AS dir_path,
               g.k::SMALLINT AS depth
        FROM (SELECT source, string_split(full_path, '{_SEP}') AS parts FROM paths) t
        JOIN src sr ON sr.source = t.source,
             unnest(generate_series(1, len(t.parts) - 1)) AS g(k)""")
    con.execute(f"""CREATE TABLE dir AS
        SELECT row_number() OVER (ORDER BY src_id, dir_path)::INTEGER AS dir_id, src_id, dir_path,
               depth,
               array_to_string(string_split(dir_path, '{_SEP}')
                   [len(string_split(dir_path, '{_SEP}')):], '{_SEP}') AS name,
               CASE WHEN depth <= 1 THEN NULL
                    ELSE array_to_string(string_split(dir_path, '{_SEP}')[1:depth - 1],
                                         '{_SEP}') END AS parent_path
        FROM _dseg""")
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
    con.execute(f"""CREATE TABLE pathx AS
        SELECT row_number() OVER (ORDER BY p.source, p.full_path)::INTEGER AS path_id,
               sr.src_id, d.dir_id, p.name, p.ext, p.size, p.versions::INTEGER AS versions,
               p.first_seen, p.last_seen, p.in_latest, c.cid_id, p.physical, p.exclusive,
               p.full_path
        FROM paths p
        JOIN src sr ON sr.source = p.source
        JOIN dir d ON d.src_id = sr.src_id AND d.dir_path = array_to_string(
             string_split(p.full_path, '{_SEP}')[1:len(string_split(p.full_path, '{_SEP}')) - 1],
             '{_SEP}')
        LEFT JOIN cid c ON c.content_id = p.content_id""")
    con.execute(f"""CREATE TABLE filev AS
        SELECT px.path_id, sn.snap_id, c.cid_id, f.size, f.mtime, f.ordinal
        FROM files f
        JOIN src sr ON sr.source = f.source
        JOIN dir d ON d.src_id = sr.src_id AND d.dir_path = array_to_string(
             string_split(f.full_path, '{_SEP}')[1:len(string_split(f.full_path, '{_SEP}')) - 1],
             '{_SEP}')
        JOIN pathx px ON px.src_id = sr.src_id AND px.dir_id = d.dir_id
             AND px.name = array_to_string(string_split(f.full_path, '{_SEP}')
                 [len(string_split(f.full_path, '{_SEP}')):], '{_SEP}')
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
            LEFT JOIN paths p ON p.source = sr.source
                 AND p.full_path = d.dir_path || '{_SEP}' || px.name
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


def build_repo(repo: str, root: str, gen: int = 1, job: dict | None = None) -> dict:
    """Full rebuild: produce a Parquet generation under ``root`` (the repo's data dir) and return
    the manifest body (artifacts + counters + sources + stats). Does not write manifest.json -
    that is publish.py's atomic job. ``root/_staging`` holds the scratch DB and staged files."""
    pool = os.path.join(root, "pool")
    staging = os.path.join(root, "_staging")
    os.makedirs(pool, exist_ok=True)
    os.makedirs(staging, exist_ok=True)
    job = {} if job is None else job
    srcs = sources_index(repo)
    job.update({"total": sum(len(v) for v in srcs.values()), "indexed": 0,
                "targets": len(srcs), "current_source": None, "error": None})

    scratch = os.path.join(staging, "build.duckdb")
    for suffix in ("", ".wal"):
        if os.path.exists(scratch + suffix):
            os.remove(scratch + suffix)
    con = duckdb.connect(scratch)
    con.execute("SET memory_limit='%s'" % os.environ.get("KOPIALENS_DUCKDB_MEMORY", "3GB"))
    tmp = os.path.join(staging, "tmp")
    os.makedirs(tmp, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp.replace(chr(92), chr(47))}'")
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

    sources = {k: {"key": k, "snapshots": len(v)} for k, v in srcs.items()}
    return {"generation": gen, "repo": repo, "schema_version": 2,
            "counters": counters, "artifacts": artifacts, "sources": sources,
            "stats": {"paths": checks["pathx"], "file_versions": checks["filev"]},
            "_checks": checks, "_moves": moves}


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