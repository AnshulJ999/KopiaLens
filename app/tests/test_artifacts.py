"""The M1 writer's schema transform (current DuckDB schema -> skinny integer-id tables).

Pinned with a tiny fixture: builds the current-schema tables in a scratch DB, runs
``artifacts._transform`` + ``_verify``, and asserts the skinny tables reconstruct every path and
content id. The full-scale correctness (all 16 benchmark questions, byte-identical to the frozen
control) is proven by the throwaway parity harness; this test catches SQL regressions in the
transform itself without the lab data.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index import artifacts
from app.index.schema import init_schema

SEP = chr(92)


@pytest.fixture
def con():
    con = duckdb.connect()
    init_schema(con)
    con.execute("INSERT INTO files VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?, ?, ?, ?), "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["1", f"s1:C:{SEP}A", f"C:{SEP}A{SEP}sub{SEP}a.txt", "a.txt", 100, "obj_a",
                 "snap1", "2025-01-01 00:00:00", 0, "2025-01-01 00:00:00",
                 "1", f"s1:C:{SEP}A", f"C:{SEP}A{SEP}sub{SEP}a.txt", "a.txt", 100, "obj_a",
                 "snap2", "2025-01-02 00:00:00", 1, "2025-01-02 00:00:00",
                 "1", f"s1:C:{SEP}A", f"C:{SEP}A{SEP}b.txt", "b.txt", 200, "obj_b",
                 "snap2", "2025-01-02 00:00:00", 0, "2025-01-02 00:00:00",
                 "1", f"s2:C:{SEP}B", f"C:{SEP}B{SEP}c.txt", "c.txt", 300, "obj_c",
                 "snap3", "2025-01-03 00:00:00", 0, "2025-01-03 00:00:00"])
    con.execute("INSERT INTO paths VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?), "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["1", f"s1:C:{SEP}A", f"C:{SEP}A{SEP}sub{SEP}a.txt", "a.txt", "txt", 100, 2,
                 "2025-01-01 00:00:00", "2025-01-02 00:00:00", True, "obj_a", 100,
                 f"c:{SEP}a{SEP}sub{SEP}a.txt", None,
                 "1", f"s1:C:{SEP}A", f"C:{SEP}A{SEP}b.txt", "b.txt", "txt", 200, 1,
                 "2025-01-02 00:00:00", "2025-01-02 00:00:00", True, "obj_b", 200,
                 f"c:{SEP}a{SEP}b.txt", None,
                 "1", f"s2:C:{SEP}B", f"C:{SEP}B{SEP}c.txt", "c.txt", "txt", 300, 1,
                 "2025-01-03 00:00:00", "2025-01-03 00:00:00", True, "obj_c", 300,
                 f"c:{SEP}b{SEP}c.txt", None])
    con.execute("INSERT INTO content_sizes VALUES (?,?,?), (?,?,?), (?,?,?)",
                ["1", "obj_a", 100, "1", "obj_b", 200, "1", "obj_c", 300])
    con.execute("INSERT INTO content_refs VALUES (?,?,?), (?,?,?), (?,?,?)",
                ["1", "obj_a", 2, "1", "obj_b", 1, "1", "obj_c", 1])
    return con


def test_transform_verifies_and_reconstructs(con):
    artifacts._transform(con)
    ok, checks = artifacts._verify(con)
    assert ok, checks
    assert checks["pathx"] == 3
    assert checks["filev"] == 4
    assert checks["path_mismatch"] == 0
    assert checks["orphan_dir"] == 0


def test_cid_namespace_holds_every_content(con):
    artifacts._transform(con)
    ids = {r[0] for r in con.execute("SELECT content_id FROM cid").fetchall()}
    assert {"obj_a", "obj_b", "obj_c"} <= ids


def test_snap_and_pathx_join_back_to_strings(con):
    artifacts._transform(con)
    # Every pathx row reconstructs to an existing full_path through dir + name (same check
    # _verify runs, but proving the string side round-trips for a known row).
    row = con.execute(
        f"SELECT d.dir_path || '{SEP}' || p.name FROM pathx p JOIN dir d USING (dir_id) "
        f"WHERE p.name = 'a.txt'").fetchone()[0]
    assert row == f"C:{SEP}A{SEP}sub{SEP}a.txt"
    snaps = {r[0] for r in con.execute("SELECT snapshot_id FROM snap").fetchall()}
    assert snaps == {"snap1", "snap2", "snap3"}