"""The incremental generation update (``build_repo``'s manifest-present path).

Pinned with a synthetic published generation (real parquet files + manifest on disk, fakes at
the walk/refcount boundaries): unchanged sources carry forward by reference, an append keeps
ids and sticky first_seen and allocates only the residue, rows from snapshots older than the
previous latest survive only through the retained-rows preload, a new source allocates above
the counters, and every published file binds back through read_parquet. The fakes reproduce
the delta walk's observable contract: newest snapshot full, older snapshots emit only
(object, path) pairs not already seen.
"""

from __future__ import annotations

import json
import os
import sqlite3

import duckdb
import pytest

from app.index import artifacts

SEP = chr(92)
S1, S2, S3 = f"s1:C:{SEP}A", f"s2:C:{SEP}B", f"s3:C:{SEP}D"
TS = {"snap0": "2024-12-15 00:00:00", "snap1": "2025-01-01 00:00:00",
      "snap2": "2025-01-02 00:00:00", "snap3": "2025-01-03 00:00:00",
      "snap4": "2025-01-04 00:00:00", "snap5": "2025-01-05 00:00:00"}

# files rows per (source, snapshot): (full_path, name, size, content_id, start_time, ordinal)
WALK = {
    (S1, "snap1"): [(f"C:{SEP}A{SEP}a.txt", "a.txt", 100, "obj_a1", TS["snap1"], 0)],
    (S1, "snap2"): [(f"C:{SEP}A{SEP}a.txt", "a.txt", 150, "obj_a2", TS["snap2"], 0),
                    (f"C:{SEP}A{SEP}b.txt", "b.txt", 200, "obj_b", TS["snap2"], 1)],
    (S2, "snap0"): [(f"C:{SEP}B{SEP}x.txt", "x.txt", 50, "obj_x", TS["snap0"], 0)],
    (S2, "snap3"): [(f"C:{SEP}B{SEP}c.txt", "c.txt", 300, "obj_c", TS["snap3"], 0)],
    (S2, "snap4"): [(f"C:{SEP}B{SEP}c.txt", "c.txt", 300, "obj_c", TS["snap4"], 0),
                    (f"C:{SEP}B{SEP}d.txt", "d.txt", 400, "obj_d", TS["snap4"], 1),
                    (f"C:{SEP}B{SEP}sub{SEP}d2.txt", "d2.txt", 500, "obj_d", TS["snap4"], 2)],
    (S3, "snap5"): [(f"C:{SEP}D{SEP}e.txt", "e.txt", 600, "obj_e", TS["snap5"], 0)],
}
SMAP = {"obj_a1": 100, "obj_a2": 150, "obj_b": 200, "obj_c": 300,
        "obj_d": 400, "obj_e": 600, "obj_x": 50}
COUNT = {k: 1 for k in SMAP}


def _listing_published() -> dict:
    """Exactly the published snapshot sets (nothing to do but carry)."""
    return {S1: [{"id": "snap1", "startTime": TS["snap1"]},
                 {"id": "snap2", "startTime": TS["snap2"]}],
            S2: [{"id": "snap0", "startTime": TS["snap0"]},
                 {"id": "snap3", "startTime": TS["snap3"]}]}


def _listing_update() -> dict:
    """The nightly case: snap4 lands on S2, S3 is brand new."""
    out = _listing_published()
    out[S2] = out[S2] + [{"id": "snap4", "startTime": TS["snap4"]}]
    out[S3] = [{"id": "snap5", "startTime": TS["snap5"]}]
    return out


STARTS = {s["id"]: s["startTime"] for src in _listing_update().values() for s in src}


def _delta_walk(con, repo: str, key: str, snaps_newest_first: list[str]) -> None:
    """The observable contract of builder._build_source / _extend_source on a fresh scratch:
    newest snapshot full, older snapshots emit only (object, path) pairs not already seen."""
    seen: set[tuple[str, str]] = set()
    for sid in snaps_newest_first:
        emitted = 0
        for full_path, name, size, cid, start, ordinal in WALK[(key, sid)]:
            if (cid, full_path) in seen:
                continue
            seen.add((cid, full_path))
            con.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [repo, key, full_path, name, size, cid, sid, start, ordinal, start])
            emitted += 1
        con.execute("INSERT OR REPLACE INTO indexed_snapshots VALUES (?,?,?,?,?,now(),?)",
                    [repo, sid, key, STARTS[sid], f"root_{sid}", emitted])
    from app.index.builder import _refresh_paths
    _refresh_paths(con, repo, key)


def _write_prev_gen(root: str) -> None:
    """A real published generation on disk: two sources, four snapshots, delta-built rows."""
    con = duckdb.connect()
    pool = os.path.join(root, "pool")
    for sub in ("src", "dir", "anc", "cid", "snap", "maps/csize", "maps/crefs", "maps/iphys",
                "small/sanalysis", "small/isnaps"):
        os.makedirs(os.path.join(pool, sub), exist_ok=True)
    for s in (1, 2):
        os.makedirs(os.path.join(pool, f"history/src_id={s}"), exist_ok=True)
        os.makedirs(os.path.join(pool, f"pathx/src_id={s}"), exist_ok=True)

    con.execute("CREATE TABLE src (src_id SMALLINT, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, ?), (2, ?)", [S1, S2])
    con.execute("CREATE TABLE dir (dir_id INTEGER, src_id SMALLINT, dir_path VARCHAR, "
                "depth SMALLINT, name VARCHAR, parent_id INTEGER)")
    con.execute(f"INSERT INTO dir VALUES (1, 1, 'C:', 1, 'C:', NULL), "
                f"(2, 1, 'C:{SEP}A', 2, 'A', 1), (3, 2, 'C:', 1, 'C:', NULL), "
                f"(4, 2, 'C:{SEP}B', 2, 'B', 3)")
    con.execute("CREATE TABLE anc (dir_id INTEGER, depth SMALLINT, anc_id INTEGER)")
    con.execute(f"INSERT INTO anc VALUES (2, 2, 2), (2, 1, 1), (4, 2, 4), (4, 1, 3)")
    con.execute("CREATE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    con.execute("INSERT INTO cid VALUES (1, 'obj_a1'), (2, 'obj_a2'), (3, 'obj_b'), "
                "(4, 'obj_c'), (5, 'obj_x')")
    con.execute("CREATE TABLE snap (snap_id INTEGER, snapshot_id VARCHAR, "
                "start_time TIMESTAMP, src_id SMALLINT)")
    con.execute("INSERT INTO snap VALUES (1, 'snap0', ?, 2), (2, 'snap1', ?, 1), "
                "(3, 'snap2', ?, 1), (4, 'snap3', ?, 2)",
                [TS["snap0"], TS["snap1"], TS["snap2"], TS["snap3"]])
    con.execute("CREATE TABLE pathx (path_id INTEGER, src_id BIGINT, dir_id INTEGER, "
                "name VARCHAR, ext VARCHAR, size UBIGINT, versions INTEGER, "
                "first_seen TIMESTAMP, last_seen TIMESTAMP, in_latest BOOLEAN, "
                "cid_id INTEGER, physical UBIGINT, exclusive UBIGINT, full_path VARCHAR)")
    # a.txt keeps a STICKY first_seen earlier than any snapshot (backfill product);
    # x.txt was deleted from disk (in_latest false) and survives only in old snapshots.
    con.execute(
        f"INSERT INTO pathx VALUES "
        f"(1, 1, 2, 'a.txt', 'txt', 150, 2, '2024-12-01', '{TS['snap2']}', true, 2, 150, NULL, "
        f"'C:{SEP}A{SEP}a.txt'), "
        f"(2, 1, 2, 'b.txt', 'txt', 200, 1, '{TS['snap2']}', '{TS['snap2']}', true, 3, 200, "
        f"NULL, 'C:{SEP}A{SEP}b.txt'), "
        f"(3, 2, 4, 'c.txt', 'txt', 300, 1, '2024-12-15', '{TS['snap3']}', true, 4, 300, "
        f"NULL, 'C:{SEP}B{SEP}c.txt'), "
        f"(4, 2, 4, 'x.txt', 'txt', 50, 1, '{TS['snap0']}', '{TS['snap0']}', false, 5, 50, "
        f"NULL, 'C:{SEP}B{SEP}x.txt')")
    con.execute("CREATE TABLE history (src_id BIGINT, path_id INTEGER, snap_id INTEGER, "
                "cid_id INTEGER, size UBIGINT, mtime TIMESTAMP, ordinal BIGINT)")
    con.execute(
        f"INSERT INTO history VALUES "
        f"(1, 1, 3, 2, 150, '{TS['snap2']}', 0), (1, 1, 2, 1, 100, '{TS['snap1']}', 0), "
        f"(1, 2, 3, 3, 200, '{TS['snap2']}', 1), (2, 3, 4, 4, 300, '{TS['snap3']}', 0), "
        f"(2, 4, 1, 5, 50, '{TS['snap0']}', 0)")
    con.execute("CREATE TABLE csize (cid_id INTEGER, packed UBIGINT)")
    con.execute("INSERT INTO csize VALUES (1, 100), (2, 150), (3, 200), (4, 300), (5, 50)")
    con.execute("CREATE TABLE crefs (cid_id INTEGER, refs INTEGER)")
    con.execute("INSERT INTO crefs VALUES (1, 1), (2, 1), (3, 1), (4, 1), (5, 1)")
    con.execute("CREATE TABLE iphys (cid_id INTEGER, packed UBIGINT)")
    con.execute("CREATE TABLE sanalysis (repo VARCHAR, source VARCHAR, snapshot_id VARCHAR, "
                "churn UBIGINT, physical UBIGINT, reclaimable UBIGINT, snaphash VARCHAR, "
                "computed_at TIMESTAMP)")
    con.execute("CREATE TABLE isnaps (repo VARCHAR, snapshot_id VARCHAR, source VARCHAR, "
                "start_time TIMESTAMP, root_obj VARCHAR, indexed_at TIMESTAMP, "
                "file_count BIGINT)")
    con.execute(
        f"INSERT INTO isnaps VALUES "
        f"('1', 'snap0', ?, '{TS['snap0']}', 'root_snap0', now(), 1), "
        f"('1', 'snap1', ?, '{TS['snap1']}', 'root_snap1', now(), 1), "
        f"('1', 'snap2', ?, '{TS['snap2']}', 'root_snap2', now(), 2), "
        f"('2', 'snap3', ?, '{TS['snap3']}', 'root_snap3', now(), 1)", [S2, S1, S1, S2])

    def copy(table: str, rel: str, order: str) -> None:
        target = (os.path.join(pool, rel)).replace(chr(92), "/")
        con.execute(f"COPY (SELECT * FROM {table} ORDER BY {order}) "
                    f"TO '{target}' (FORMAT parquet, COMPRESSION zstd)")

    for s in (1, 2):
        con.execute(f"COPY (SELECT * FROM history WHERE src_id = {s} ORDER BY path_id, snap_id) "
                    f"TO '{(os.path.join(pool, f'history/src_id={s}/g1.parquet')).replace(chr(92), '/')}' "
                    f"(FORMAT parquet, COMPRESSION zstd)")
        con.execute(f"COPY (SELECT * FROM pathx WHERE src_id = {s} ORDER BY dir_id, name) "
                    f"TO '{(os.path.join(pool, f'pathx/src_id={s}/g1.parquet')).replace(chr(92), '/')}' "
                    f"(FORMAT parquet, COMPRESSION zstd)")
    for table, rel, order in (("src", "src/g1.parquet", "src_id"),
                              ("dir", "dir/g1.parquet", "src_id, dir_path"),
                              ("anc", "anc/g1.parquet", "depth, dir_id"),
                              ("cid", "cid/g1.parquet", "cid_id"),
                              ("snap", "snap/g1.parquet", "snap_id"),
                              ("csize", "maps/csize/g1.parquet", "cid_id"),
                              ("crefs", "maps/crefs/g1.parquet", "cid_id"),
                              ("iphys", "maps/iphys/g1.parquet", "cid_id"),
                              ("sanalysis", "small/sanalysis/g1.parquet", "snapshot_id"),
                              ("isnaps", "small/isnaps/g1.parquet", "snapshot_id")):
        copy(table, rel, order)
    con.close()
    manifest = {
        "generation": 1, "repo": "1", "schema_version": 2,
        "counters": {"src_id": 2, "dir_id": 4, "path_id": 4, "cid_id": 5, "snap_id": 4},
        "artifacts": {
            "filev": ["pool/history/src_id=1/g1.parquet", "pool/history/src_id=2/g1.parquet"],
            "pathx": ["pool/pathx/src_id=1/g1.parquet", "pool/pathx/src_id=2/g1.parquet"],
            "src": ["pool/src/g1.parquet"], "dir": ["pool/dir/g1.parquet"],
            "anc": ["pool/anc/g1.parquet"], "cid": ["pool/cid/g1.parquet"],
            "snap": ["pool/snap/g1.parquet"], "csize": ["pool/maps/csize/g1.parquet"],
            "crefs": ["pool/maps/crefs/g1.parquet"], "iphys": ["pool/maps/iphys/g1.parquet"],
            "sanalysis": ["pool/small/sanalysis/g1.parquet"],
            "isnaps": ["pool/small/isnaps/g1.parquet"],
        },
        "sources": {S1: {"key": S1, "snapshots": 2}, S2: {"key": S2, "snapshots": 2}},
        "stats": {"paths": 4, "file_versions": 5},
    }
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = str(tmp_path / "repo1")
    os.makedirs(root)
    _write_prev_gen(root)

    import app.index.physical as physical
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_update())
    monkeypatch.setattr(artifacts, "cache_db", lambda repo: sqlite3.connect(":memory:"))
    monkeypatch.setattr(artifacts, "_refcount", lambda repo, db: (COUNT, 0))
    monkeypatch.setattr(artifacts, "backfill_first_seen", lambda con, db, repo, job: 0)
    # real-data walk machinery is faked at the walk boundary; the new-source backfill is part
    # of that boundary here (a dedicated test replaces this with a recorder)
    monkeypatch.setattr(artifacts, "_backfill_source_first_seen",
                        lambda con, db, repo, key, snaps: 0)
    monkeypatch.setattr(physical, "cache_db", lambda repo: sqlite3.connect(":memory:"))
    monkeypatch.setattr(physical, "load_sizemap", lambda repo, refresh=False: dict(SMAP))

    def fake_extend(con, db, repo, key, snaps, new_snaps, job):
        # newest-first: the new snapshots, then the previous latest as a delta
        new_ids = {s["id"] for s in new_snaps}
        ordered = sorted(snaps, key=lambda s: s.get("startTime", ""), reverse=True)
        rewalk = [s["id"] for s in ordered
                  if s["id"] in new_ids or s["id"] == next(
                      (p["id"] for p in ordered if p["id"] not in new_ids), None)]
        _delta_walk(con, repo, key, rewalk)
        job["indexed"] += len(rewalk)
        return len(rewalk)

    def fake_build(con, db, repo, key, snaps, job):
        ordered = sorted(snaps, key=lambda s: s.get("startTime", ""), reverse=True)
        _delta_walk(con, repo, key, [s["id"] for s in ordered])
        job["indexed"] += len(snaps)

    monkeypatch.setattr(artifacts, "_extend_source", fake_extend)
    monkeypatch.setattr(artifacts, "_build_source", fake_build)
    return root


def _rows(root: str, rel: str) -> list[tuple]:
    con = duckdb.connect()
    try:
        p = (os.path.join(root, rel)).replace(chr(92), "/").replace("'", "''")
        return con.execute(f"SELECT * FROM read_parquet('{p}')").fetchall()
    finally:
        con.close()


def test_append_carries_and_allocates(env):
    body = artifacts.build_repo("1", env, gen=1, job={})
    assert body["_mode"] == "incremental"
    assert body["generation"] == 2  # never restamps the published generation's names
    assert body["counters"] == {"src_id": 3, "dir_id": 7, "path_id": 7,
                                "cid_id": 7, "snap_id": 6}

    # publish: staged files move into pool/, the manifest swaps atomically
    from app.index import publish
    publish.publish(env, body)

    # unchanged source carried BY REFERENCE - identical manifest strings
    assert "pool/pathx/src_id=1/g1.parquet" in body["artifacts"]["pathx"]
    assert "pool/history/src_id=1/g1.parquet" in body["artifacts"]["filev"]
    assert "pool/pathx/src_id=2/g2.parquet" in body["artifacts"]["pathx"]
    assert body["artifacts"]["sanalysis"] == ["pool/small/sanalysis/g1.parquet"]

    # rewritten source: existing paths keep ids + sticky first_seen; new paths above counters
    px = {r[3]: r for r in _rows(env, "pool/pathx/src_id=2/g2.parquet")}
    c, x, d, d2 = px["c.txt"], px["x.txt"], px["d.txt"], px["d2.txt"]
    assert c[0] == 3 and str(c[7])[:10] == "2024-12-15" and c[6] == 1 and c[9] is True
    # x.txt was deleted from disk: its row survives through the retained-rows preload
    assert x[0] == 4 and x[9] is False and str(x[7])[:10] == "2024-12-15"
    assert d[0] == 5 and d[10] == 6  # obj_d -> cid_id 6
    assert d2[0] == 6 and d2[2] == 5  # new sub dir -> dir_id 5
    # new source: dirs and paths above s2's allocations
    px3 = _rows(env, "pool/pathx/src_id=3/g2.parquet")
    assert px3[0][0] == 7 and px3[0][10] == 7  # e.txt, obj_e -> cid_id 7

    dir_rows = _rows(env, "pool/dir/g2.parquet")
    sub = [r for r in dir_rows if r[2] == f"C:{SEP}B{SEP}sub"][0]
    assert sub[0] == 5 and sub[5] == 4  # parented to C:\B
    anc = _rows(env, "pool/anc/g2.parquet")
    # (dir_id, ancestor's depth, ancestor_id) - the _transform anc semantics
    assert {(r[0], r[1], r[2]) for r in anc if r[0] == 5} == {(5, 3, 5), (5, 2, 4), (5, 1, 3)}
    snaps = {r[1]: r[0] for r in _rows(env, "pool/snap/g2.parquet")}
    assert snaps["snap0"] == 1 and snaps["snap4"] == 5 and snaps["snap5"] == 6

    # history: the retained row + the walked rows, no duplication
    hist = _rows(env, "pool/history/src_id=2/g2.parquet")
    assert sorted((r[1], r[2]) for r in hist) == [(3, 5), (4, 1), (5, 5), (6, 5)]

    csize = {r[0]: r[1] for r in _rows(env, "pool/maps/csize/g2.parquet")}
    assert csize == {1: 100, 2: 150, 3: 200, 4: 300, 5: 50, 6: 400, 7: 600}
    crefs = {r[0]: r[1] for r in _rows(env, "pool/maps/crefs/g2.parquet")}
    assert crefs == {k: 1 for k in range(1, 8)}
    isnaps = {r[1] for r in _rows(env, "pool/small/isnaps/g2.parquet")}
    assert isnaps == {"snap0", "snap1", "snap2", "snap3", "snap4", "snap5"}
    assert body["stats"] == {"paths": 7, "file_versions": 8}

    # every published file binds back through read_parquet (mixed g1/g2 lists stay unionable)
    con = duckdb.connect()
    try:
        fl = ", ".join(f"'{(os.path.join(env, r)).replace(chr(92), '/')}'"
                       for r in body["artifacts"]["pathx"])
        assert con.execute(f"SELECT count(*) FROM read_parquet([{fl}])").fetchone()[0] == 7
    finally:
        con.close()


def test_force_rebuild_is_id_stable(env, monkeypatch):
    # hold the listing at exactly the published snapshot sets so nothing else is a target
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    body = artifacts.build_repo("1", env, gen=1, job={}, force={S1})
    assert body["_mode"] == "incremental"
    from app.index import publish
    publish.publish(env, body)
    # nothing new anywhere: the rebuilt source reproduces its own ids exactly. cid_id grows
    # because the sizemap namespace now completes the dictionary (obj_d/obj_e were missing
    # from the hand-written fixture generation - the map-completeness fix doing its job)
    assert body["counters"] == {"src_id": 2, "dir_id": 4, "path_id": 4,
                                "cid_id": 7, "snap_id": 4}
    assert body["artifacts"]["filev"] == ["pool/history/src_id=1/g2.parquet",
                                          "pool/history/src_id=2/g1.parquet"]
    assert body["stats"] == {"paths": 4, "file_versions": 5}
    # a.txt's id and backfilled sticky first_seen survive the rebuild
    px = _rows(env, "pool/pathx/src_id=1/g2.parquet")
    a = [r for r in px if r[3] == "a.txt"][0]
    assert a[0] == 1 and str(a[7])[:10] == "2024-12-01" and a[6] == 2


def test_no_manifest_full_rebuild(tmp_path, env):
    empty = str(tmp_path / "fresh")
    os.makedirs(empty)
    body = artifacts.build_repo("1", empty, gen=1, job={})
    assert body["generation"] == 1
    assert body["stats"]["paths"] == 7
    assert set(body["artifacts"]) == {"filev", "pathx", "src", "dir", "anc", "cid", "snap",
                                      "csize", "crefs", "iphys", "sanalysis", "isnaps"}


def test_empty_listing_against_published_generation_refused(env, monkeypatch):
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: {})
    with pytest.raises(RuntimeError, match="refusing to treat that as a removal"):
        artifacts.build_repo("1", env, gen=2, job={})


def test_snapshot_expiry_is_purged_and_stops_rebuilding(env, monkeypatch):
    """S2 loses snap0 to retention: the vanished snapshot must not survive in isnaps (wrong
    counts) nor in the snap dictionary, and - the expensive part - the NEXT unchanged pass must
    classify S2 as carried again, not rebuild it every night forever."""
    listing = _listing_published()
    listing[S2] = [s for s in listing[S2] if s["id"] != "snap0"]
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: listing)
    body = artifacts.build_repo("1", env, gen=1, job={})
    assert body["_mode"] == "incremental"
    from app.index import publish
    publish.publish(env, body)
    isnaps = {r[1] for r in _rows(env, "pool/small/isnaps/g2.parquet")}
    assert isnaps == {"snap1", "snap2", "snap3"}      # snap0 gone, nothing phantom
    snaps = {r[1] for r in _rows(env, "pool/snap/g2.parquet")}
    assert "snap0" not in snaps
    # a second pass over the SAME listing: every source carried, nothing rebuilt
    body2 = artifacts.build_repo("1", env, gen=2, job={})
    assert S2 not in body2["_checks"]["sources"]
    assert body2["stats"] == body["stats"]


def test_incremental_includes_sizemap_only_chunks(env, monkeypatch):
    """A split file's underlying chunk ids exist only in the sizemap. The cid dictionary must
    grow to hold them, and csize must not drop them in its join (a missing chunk reads as zero
    references = 'safe to delete')."""
    import app.index.physical as physical
    smap = {**SMAP, "chunk_z": 42}                    # chunk_z backs obj_d, never a file row
    monkeypatch.setattr(physical, "load_sizemap", lambda repo, refresh=False: dict(smap))
    body = artifacts.build_repo("1", env, gen=1, job={})
    from app.index import publish
    publish.publish(env, body)
    cid = {r[1]: r[0] for r in _rows(env, "pool/cid/g2.parquet")}
    assert "chunk_z" in cid
    csize = {r[0]: r[1] for r in _rows(env, "pool/maps/csize/g2.parquet")}
    assert csize[cid["chunk_z"]] == 42


def test_new_source_gets_first_seen_backfill(env, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(artifacts, "_backfill_source_first_seen",
                        lambda con, db, repo, key, snaps: seen.append(key) or 0)
    artifacts.build_repo("1", env, gen=1, job={})
    assert seen == [S3]   # S3 is new; S1 (carried) and S2 (append) keep their carried dates


def test_case_only_folder_rename_gets_distinct_ids(env, monkeypatch):
    """A case-only FOLDER rename leaves the old spelling's rows in old snapshots and the new
    spelling's in the new one - two display rows sharing one folded identity AND one name.
    Keying the id carry on the raw full path is what keeps two ids: grouping by identity handed
    both rows the same id, the dup_path_id check caught it, and the whole build aborted."""
    import app.index.physical as physical
    count = {**COUNT, "obj_f": 1, "obj_f2": 1}
    monkeypatch.setattr(artifacts, "_refcount", lambda repo, db: (count, 0))
    monkeypatch.setattr(physical, "load_sizemap", lambda repo, refresh=False:
                        {**SMAP, "obj_f": 180, "obj_f2": 210})
    listing = _listing_update()
    listing[S1] = listing[S1] + [{"id": "snap3", "startTime": TS["snap3"]}]
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: listing)
    # old spelling lives in snap2 (re-walked as the previous latest), new spelling in snap3
    monkeypatch.setitem(WALK, (S1, "snap2"), WALK[(S1, "snap2")] + [
        (f"C:{SEP}A{SEP}NVIDIA app{SEP}f.txt", "f.txt", 180, "obj_f", TS["snap2"], 2)])
    monkeypatch.setitem(WALK, (S1, "snap3"), [
        (f"C:{SEP}A{SEP}NVIDIA App{SEP}f.txt", "f.txt", 210, "obj_f2", TS["snap3"], 0)])

    body = artifacts.build_repo("1", env, gen=1, job={})
    from app.index import publish
    publish.publish(env, body)

    px = _rows(env, "pool/pathx/src_id=1/g2.parquet")
    frows = [r for r in px if r[3] == "f.txt"]
    assert len(frows) == 2
    assert len({r[0] for r in frows}) == 2  # the old failure mode was one shared id for both
    assert {r[13] for r in frows} == {f"C:{SEP}A{SEP}NVIDIA app{SEP}f.txt",
                                      f"C:{SEP}A{SEP}NVIDIA App{SEP}f.txt"}
    dirs = _rows(env, "pool/dir/g2.parquet")
    assert {r[2] for r in dirs} >= {f"C:{SEP}A{SEP}NVIDIA app", f"C:{SEP}A{SEP}NVIDIA App"}


def test_incremental_writes_smallint_src_id(env):
    """The full build writes src_id as SMALLINT; the incremental must match, or a mixed
    generation binds two different column types for one artifact kind."""
    body = artifacts.build_repo("1", env, gen=1, job={})
    from app.index import publish
    publish.publish(env, body)
    con = duckdb.connect()
    try:
        p = (env + "/pool/pathx/src_id=2/g2.parquet").replace(chr(92), "/")
        # hive_partitioning=false: the src_id=N directory auto-detects as a partition column
        # and would override the file's own column type (the documented DuckDB trap)
        cols = {r[0]: r[1] for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{p}', hive_partitioning=false)").fetchall()}
    finally:
        con.close()
    assert cols["src_id"] == "SMALLINT"
