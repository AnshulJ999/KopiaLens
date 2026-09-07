"""The M1 writer's schema transform (current DuckDB schema -> skinny integer-id tables).

Pinned with a tiny fixture: builds the current-schema tables in a scratch DB, runs
``artifacts._transform`` + ``_verify``, and asserts the skinny tables reconstruct every path and
content id. The full-scale correctness (all 16 benchmark questions, byte-identical to the frozen
control) is proven by the throwaway parity harness; this test catches SQL regressions in the
transform itself without the lab data.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time

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


def test_transform_handles_a_posix_source():
    """The separator is a property of the SOURCE: a POSIX root must split, build folders and
    reconstruct paths with '/', in the same transform pass as Windows sources."""
    con = duckdb.connect()
    try:
        init_schema(con)
        src = "u@host:/home/x"
        con.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ["1", src, "/home/x/a.txt", "a.txt", 100, "obj_p",
                     "snapP", "2025-01-01 00:00:00", 0, "2025-01-01 00:00:00"])
        con.execute("INSERT INTO paths VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ["1", src, "/home/x/a.txt", "a.txt", "txt", 100, 1,
                     "2025-01-01 00:00:00", "2025-01-01 00:00:00", True, "obj_p", 100,
                     "/home/x/a.txt", None])
        con.execute("INSERT INTO content_sizes VALUES (?,?,?)", ["1", "obj_p", 100])
        con.execute("INSERT INTO content_refs VALUES (?,?,?)", ["1", "obj_p", 1])
        artifacts._transform(con)
        ok, checks = artifacts._verify(con)
        assert ok, checks
        dirs = con.execute("SELECT dir_path, depth FROM dir ORDER BY depth").fetchall()
        assert dirs == [("", 1), ("/home", 2), ("/home/x", 3)]
        row = con.execute(
            "SELECT d.dir_path || sp.sep || p.name FROM pathx p "
            "JOIN dir d ON d.dir_id = p.dir_id "
            "JOIN src s ON s.src_id = p.src_id "
            "JOIN _seps sp ON sp.src_id = s.src_id").fetchone()[0]
        assert row == "/home/x/a.txt"
    finally:
        con.close()


def test_empty_repo_publishes_an_openable_generation(tmp_path, monkeypatch):
    """A repo with zero sources (fresh install, no snapshots) must still publish a generation
    the store can open - the compatibility views reference the per-source artifacts."""
    from app.index import publish, store
    import app.index.physical as physical

    root = str(tmp_path / "repo1")
    os.makedirs(root)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: {})
    monkeypatch.setattr(artifacts, "cache_db", lambda repo: sqlite3.connect(":memory:"))
    monkeypatch.setattr(artifacts, "_refcount", lambda repo, db: ({}, 0))
    monkeypatch.setattr(artifacts, "backfill_first_seen", lambda con, db, repo, job: 0)
    monkeypatch.setattr(physical, "cache_db", lambda repo: sqlite3.connect(":memory:"))
    monkeypatch.setattr(physical, "load_sizemap", lambda repo, refresh=False: {})
    body = artifacts.build_repo("1", root, gen=1, job={}, full=True)
    assert body["stats"] == {"paths": 0, "file_versions": 0}
    publish.publish(root, body)
    arts = {name: [os.path.join(root, rel) for rel in rels]
            for name, rels in body["artifacts"].items()}
    con = duckdb.connect()
    try:
        store._bind_generation(con, "1", arts)
        assert con.execute("SELECT count(*) FROM pathx").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM filev").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM isnaps").fetchone()[0] == 0
    finally:
        con.close()


def test_gc_keeps_protected_files_regardless_of_separator(tmp_path):
    """A manifest path written with backslashes (as os.path.join produces) must still protect
    its file - GC compares forward-slash forms."""
    from app.index import publish
    protected = tmp_path / "pool" / "small" / "isnaps" / "g1.parquet"
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b"x")
    orphan = tmp_path / "pool" / "small" / "sanalysis" / "g9.parquet"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(protected, (old, old))
    os.utime(orphan, (old, old))
    publish._gc(str(tmp_path), {"isnaps": ["pool\\small\\isnaps\\g1.parquet"]}, {})
    assert protected.exists()
    assert not orphan.exists()


def test_replace_artifacts_reads_the_manifest_inside_the_lock(tmp_path, monkeypatch):
    """A publication landing between the manifest read and the lock must not be rolled back:
    the manifest is (re)read only while the writer lock is held."""
    from app.index import publish, store
    import pyarrow as pa

    root = tmp_path / "repo1"
    root.mkdir()
    monkeypatch.setattr(store, "repo_dir", lambda repo: str(root))
    monkeypatch.setattr(publish, "repo_dir", lambda repo: str(root))  # publish imports it directly

    def fake_read_manifest(repo):
        if (root / ".writer").exists():          # we hold the lock -> the CURRENT manifest
            return {"generation": 5, "repo": "1",
                    "artifacts": {"sanalysis": ["pool/small/sanalysis/g5.parquet"]}}
        return {"generation": 4, "repo": "1",
                "artifacts": {"sanalysis": ["pool/small/sanalysis/g4.parquet"]}}

    monkeypatch.setattr(store, "read_manifest", fake_read_manifest)
    tbl = pa.table({"repo": ["1"], "source": ["s"], "snapshot_id": ["sn"], "churn": [1],
                    "physical": [1], "reclaimable": [1], "snaphash": ["h"],
                    "computed_at": [dt.datetime.now()]})
    gen = publish.replace_artifacts("1", {"sanalysis": tbl})
    assert gen == 6     # from the post-lock manifest; a pre-lock read would have published 5
    with open(root / "manifest.json", encoding="utf-8") as fh:
        assert json.load(fh)["generation"] == 6