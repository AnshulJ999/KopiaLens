"""The store reader's compatibility views - portable, synthesized fixture.

Proves the Parquet artifacts, through ``store._bind_generation``, reassemble today's
``files``/``paths``/``content_sizes``/``content_refs``/``indirect_phys``/``snapshot_analysis``/
``indexed_snapshots`` shapes exactly: full_path, source, content_id and snapshot_id all map
through the integer id joins, ``path_lc`` is ``lower(full_path)``, and the repo id is a literal.
The real-data parity (16 benchmark questions + app functions on both repos) is the throwaway
smoke check and, later, the parity runner; this test pins the view SQL without the lab data.
"""

from __future__ import annotations

import datetime as dt
import os

import duckdb
import pytest

from app.index import store

SEP = chr(92)


def _dump(con: duckdb.DuckDBPyConnection, dirpath: str, name: str, create_sql: str) -> str:
    """Create a table in the scratch DB and COPY it to one parquet file; return the path."""
    con.execute(create_sql)
    p = os.path.join(dirpath, name + ".parquet")
    con.execute(f"COPY {name} TO '{p.replace(chr(92), chr(47))}' (FORMAT parquet)")
    return p


@pytest.fixture
def artifacts(tmp_path):
    con = duckdb.connect()
    d = str(tmp_path)
    _dump(con, d, "src", f"CREATE TABLE src AS SELECT * FROM (VALUES "
          f"(1::SMALLINT, 's1:C:{SEP}A'), (2::SMALLINT, 's2:C:{SEP}B')) t(src_id, source)")
    _dump(con, d, "cid", "CREATE TABLE cid AS SELECT * FROM (VALUES "
          "(10::INTEGER, 'obj_a'), (20, 'obj_b'), (30, 'obj_c')) t(cid_id, content_id)")
    _dump(con, d, "snap", "CREATE TABLE snap AS SELECT * FROM (VALUES "
          "(100::INTEGER, 'snap1', TIMESTAMP '2025-01-01 00:00:00', 1::SMALLINT), "
          "(200, 'snap2', TIMESTAMP '2025-01-02 00:00:00', 1)) t(snap_id, snapshot_id, start_time, src_id)")
    _dump(con, d, "pathx", f"CREATE TABLE pathx AS SELECT * FROM (VALUES "
          f"(1::INTEGER, 1::BIGINT, 2::INTEGER, 'a.txt', 'txt', 100::UBIGINT, 2::INTEGER, "
          f"TIMESTAMP '2025-01-01', TIMESTAMP '2025-01-02', true, 10::INTEGER, 100::UBIGINT, "
          f"NULL::UBIGINT, 'C:{SEP}A{SEP}sub{SEP}a.txt'), "
          f"(2, 1, 1, 'b.txt', 'txt', 200, 1, TIMESTAMP '2025-01-02', TIMESTAMP '2025-01-02', "
          f"true, 20, 200, NULL, 'C:{SEP}A{SEP}b.txt'), "
          f"(3, 2, 1, 'c.txt', 'txt', 300, 1, TIMESTAMP '2025-01-03', TIMESTAMP '2025-01-03', "
          f"true, 30, 300, NULL, 'C:{SEP}B{SEP}c.txt')) "
          f"t(path_id, src_id, dir_id, name, ext, size, versions, first_seen, last_seen, "
          f"in_latest, cid_id, physical, exclusive, full_path)")
    _dump(con, d, "filev", "CREATE TABLE filev AS SELECT * FROM (VALUES "
          "(1::BIGINT, 1::INTEGER, 100::INTEGER, 10::INTEGER, 100::UBIGINT, TIMESTAMP '2025-01-01', 0::BIGINT), "
          "(1, 1, 200, 10, 100, TIMESTAMP '2025-01-02', 1), "
          "(1, 2, 200, 20, 200, TIMESTAMP '2025-01-02', 0), "
          "(2, 3, 200, 30, 300, TIMESTAMP '2025-01-03', 0)) "
          "t(src_id, path_id, snap_id, cid_id, size, mtime, ordinal)")
    _dump(con, d, "csize", "CREATE TABLE csize AS SELECT * FROM (VALUES "
          "(10::INTEGER, 100::UBIGINT), (30, 300)) t(cid_id, packed)")
    _dump(con, d, "crefs", "CREATE TABLE crefs AS SELECT * FROM (VALUES "
          "(10::INTEGER, 2::INTEGER), (20, 1)) t(cid_id, refs)")
    _dump(con, d, "iphys", "CREATE TABLE iphys AS SELECT * FROM (VALUES "
          "(30::INTEGER, 300::UBIGINT)) t(cid_id, packed)")
    _dump(con, d, "sanalysis", "CREATE TABLE sanalysis(repo VARCHAR, source VARCHAR, snapshot_id VARCHAR, "
          "churn UBIGINT, physical UBIGINT, reclaimable UBIGINT, snaphash VARCHAR, computed_at TIMESTAMP)")
    _dump(con, d, "isnaps", "CREATE TABLE isnaps(repo VARCHAR, snapshot_id VARCHAR, source VARCHAR, "
          "start_time TIMESTAMP, root_obj VARCHAR, indexed_at TIMESTAMP, file_count BIGINT)")
    con.close()
    a = {n: [os.path.join(d, n + ".parquet")] for n in
         ("src", "cid", "snap", "pathx", "filev", "csize", "crefs", "iphys", "sanalysis", "isnaps")}
    return a


def _bind(artifacts):
    con = duckdb.connect()
    store._bind_generation(con, "1", artifacts)
    return con


def test_paths_reassembles_every_join(artifacts):
    con = _bind(artifacts)
    rows = con.execute(
        "SELECT full_path, source, content_id, path_lc, versions, in_latest FROM paths "
        "ORDER BY full_path").fetchall()
    assert rows == [
        (f"C:{SEP}A{SEP}b.txt", f"s1:C:{SEP}A", "obj_b", f"c:{SEP}a{SEP}b.txt", 1, True),
        (f"C:{SEP}A{SEP}sub{SEP}a.txt", f"s1:C:{SEP}A", "obj_a",
         f"c:{SEP}a{SEP}sub{SEP}a.txt", 2, True),
        (f"C:{SEP}B{SEP}c.txt", f"s2:C:{SEP}B", "obj_c", f"c:{SEP}b{SEP}c.txt", 1, True),
    ]


def test_files_maps_snapshot_and_content(artifacts):
    con = _bind(artifacts)
    rows = con.execute(
        "SELECT full_path, snapshot_id, start_time, content_id, ordinal FROM files "
        "ORDER BY full_path, snapshot_id").fetchall()
    assert rows == [
        (f"C:{SEP}A{SEP}b.txt", "snap2", dt.datetime(2025, 1, 2), "obj_b", 0),
        (f"C:{SEP}A{SEP}sub{SEP}a.txt", "snap1", dt.datetime(2025, 1, 1), "obj_a", 0),
        (f"C:{SEP}A{SEP}sub{SEP}a.txt", "snap2", dt.datetime(2025, 1, 2), "obj_a", 1),
        (f"C:{SEP}B{SEP}c.txt", "snap2", dt.datetime(2025, 1, 2), "obj_c", 0),
    ]


def test_maps_translate_cid_id_to_content_string(artifacts):
    con = _bind(artifacts)
    assert con.execute("SELECT repo, cid, packed FROM content_sizes ORDER BY cid").fetchall() == [
        ("1", "obj_a", 100), ("1", "obj_c", 300)]
    assert con.execute("SELECT repo, cid, refs FROM content_refs ORDER BY cid").fetchall() == [
        ("1", "obj_a", 2), ("1", "obj_b", 1)]
    assert con.execute("SELECT repo, oid, packed FROM indirect_phys").fetchall() == [("1", "obj_c", 300)]


def test_small_tables_and_readonly_stubs(artifacts):
    con = _bind(artifacts)
    assert con.execute("SELECT count(*) FROM snapshot_analysis").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM indexed_snapshots").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM first_seen_meta").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM kl_meta").fetchone()[0] == 0