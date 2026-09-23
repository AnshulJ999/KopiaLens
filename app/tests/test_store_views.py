"""The store reader's raw artifact binding - portable, synthesized fixture.

Proves ``store._bind_generation`` opens a generation from explicit parquet file lists: every
raw artifact (``src``/``cid``/``snap``/``pathx``/``filev``/``csize``/``crefs``/``iphys`` plus
the passthrough ``sanalysis``/``isnaps``) is bound and cross-readable, and the builder-only
stubs answer empty. M2 deleted the compatibility views this file used to pin; the rewritten
query modules are pinned by their own tests and by the parity runner on real data.
"""

from __future__ import annotations

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
    store._bind_generation(con, "1", artifacts, encrypted=False)
    return con


def test_raw_artifacts_bind_and_cross_read(artifacts):
    con = _bind(artifacts)
    assert con.execute("SELECT count(*) FROM pathx").fetchone()[0] == 3
    rows = con.execute(
        "SELECT p.full_path, sn.snapshot_id, c.content_id FROM filev f "
        "JOIN pathx p ON p.path_id = f.path_id "
        "JOIN snap sn ON sn.snap_id = f.snap_id "
        "JOIN cid c ON c.cid_id = f.cid_id "
        "ORDER BY p.full_path, sn.snapshot_id").fetchall()
    assert rows == [
        (f"C:{SEP}A{SEP}b.txt", "snap2", "obj_b"),
        (f"C:{SEP}A{SEP}sub{SEP}a.txt", "snap1", "obj_a"),
        (f"C:{SEP}A{SEP}sub{SEP}a.txt", "snap2", "obj_a"),
        (f"C:{SEP}B{SEP}c.txt", "snap2", "obj_c"),
    ]
    assert con.execute("SELECT cid_id, refs FROM crefs ORDER BY cid_id").fetchall() == [
        (10, 2), (20, 1)]
    assert con.execute("SELECT cid_id, packed FROM csize ORDER BY cid_id").fetchall() == [
        (10, 100), (30, 300)]


def test_small_tables_and_readonly_stubs(artifacts):
    con = _bind(artifacts)
    assert con.execute("SELECT count(*) FROM sanalysis").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM isnaps").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM first_seen_meta").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM kl_meta").fetchone()[0] == 0
