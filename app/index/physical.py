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

import json

import pyarrow as pa

from ..core.cache import cache_db
from ..core.kopia import snapshot_delete_commands
from ..core.sizemap import load_sizemap
from ..core.walk import expand_indirect
from .queries import snapshots_for_content
from .schema import read_cursor


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
    existing = con.execute("SELECT count(*) FROM content_sizes WHERE repo=?", [repo]).fetchone()[0]
    if existing != len(smap):
        con.execute("DELETE FROM content_sizes WHERE repo=?", [repo])
        cids = list(smap.keys())
        _arrow_insert(
            con,
            "INSERT INTO content_sizes SELECT repo, cid, packed FROM _phys_ingest",
            {"repo": [repo] * len(cids), "cid": cids, "packed": [smap[c] for c in cids]},
        )

    # 2) indirect_phys - packed footprint per indirect (large/split) file object.
    objs = [r[0] for r in con.execute(
        "SELECT DISTINCT content_id FROM paths WHERE repo=? AND content_id LIKE 'I%'", [repo]
    ).fetchall()]
    if objs:
        expand_indirect(repo, dbsql, set(objs))  # ensure all expanded + cached in sqlite
        cur = dbsql.cursor()
        oid_list: list[str] = []
        packed_list: list[int] = []
        for oid in objs:
            parts = {oid[1:]}  # the index content itself
            row = cur.execute("SELECT parts FROM indirect WHERE oid=?", (oid,)).fetchone()
            if row:
                parts |= set(json.loads(row[0]))
            oid_list.append(oid)
            packed_list.append(sum(smap.get(p, 0) for p in parts))
        con.execute("DELETE FROM indirect_phys WHERE repo=?", [repo])
        _arrow_insert(
            con,
            "INSERT INTO indirect_phys SELECT repo, oid, packed FROM _phys_ingest",
            {"repo": [repo] * len(oid_list), "oid": oid_list, "packed": packed_list},
        )
    else:
        con.execute("DELETE FROM indirect_phys WHERE repo=?", [repo])

    # 3) paths.physical via joins: direct content -> content_sizes; indirect -> indirect_phys.
    con.execute("UPDATE paths SET physical = 0 WHERE repo=?", [repo])
    con.execute(
        "UPDATE paths SET physical = cs.packed FROM content_sizes cs "
        "WHERE paths.repo=? AND cs.repo=? AND paths.content_id = cs.cid AND paths.content_id NOT LIKE 'I%'",
        [repo, repo],
    )
    con.execute(
        "UPDATE paths SET physical = ip.packed FROM indirect_phys ip "
        "WHERE paths.repo=? AND ip.repo=? AND paths.content_id = ip.oid",
        [repo, repo],
    )


def materialize_refcount(con, repo: str, count: dict) -> None:
    """Persist the global content reference-count map into DuckDB (called from the pre-warm
    pass / dashboard build, which compute it anyway). Powers accurate ``freeable``. Skips the
    reinsert when the row count already matches (cheap to call every warm cycle)."""
    existing = con.execute("SELECT count(*) FROM content_refs WHERE repo=?", [repo]).fetchone()[0]
    if existing == len(count):
        return
    con.execute("DELETE FROM content_refs WHERE repo=?", [repo])
    cids = list(count.keys())
    _arrow_insert(
        con,
        "INSERT INTO content_refs SELECT repo, cid, refs FROM _phys_ingest",
        {"repo": [repo] * len(cids), "cid": cids, "refs": [int(count[c]) for c in cids]},
    )


def _refcount_for(repo: str, cids) -> dict[str, int] | None:
    """Targeted refcount for just these content chunks (one file's chunks), via batched IN
    queries - far faster + lighter than loading the whole 4M-row content_refs table into a dict
    on every file-detail open. Returns None when content_refs isn't built yet (cold start), so
    the caller can fall back to the full map."""
    cids = list(cids)
    cur = read_cursor(repo)
    out: dict[str, int] = {}
    try:
        if not cur.execute("SELECT count(*) FROM content_refs WHERE repo=?", [repo]).fetchone()[0]:
            return None
        for i in range(0, len(cids), 900):
            grp = cids[i:i + 900]
            if not grp:
                continue
            q = "SELECT cid, refs FROM content_refs WHERE repo=? AND cid IN (%s)" % ",".join("?" * len(grp))
            for c, r in cur.execute(q, [repo, *grp]).fetchall():
                out[c] = r
    finally:
        cur.close()
    return out


def _refcount_map(repo: str) -> dict[str, int]:
    """Global content refcount: prefer the materialized DuckDB table; fall back to the engine
    pkl (rebuilds once if absent)."""
    cur = read_cursor(repo)
    try:
        rows = cur.execute("SELECT cid, refs FROM content_refs WHERE repo=?", [repo]).fetchall()
    finally:
        cur.close()
    if rows:
        return {cid: refs for cid, refs in rows}
    from ..core.metrics import cached_refcount  # lazy: avoids a heavy import on hot paths
    count, _ = cached_refcount(repo, cache_db(repo), load_sizemap(repo), mode="all")
    return count


def file_detail(repo: str, source: str, full_path: str) -> dict:
    """Accurate per-file footprint: logical/physical/freeable + dedup chunk breakdown +
    other paths sharing the same content object."""
    cur = read_cursor(repo)
    try:
        versions = cur.execute(
            "SELECT snapshot_id, start_time, size, content_id FROM files "
            "WHERE repo=? AND source=? AND full_path=? ORDER BY start_time DESC",
            [repo, source, full_path],
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
        others = cur.execute(
            "SELECT DISTINCT source, full_path FROM files "
            "WHERE repo=? AND content_id=? AND NOT (source=? AND full_path=?) LIMIT 500",
            [repo, latest_obj, source, full_path],
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
