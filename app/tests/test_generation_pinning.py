"""The generation lifecycle contract (review fixes 2.3 + 2.5).

``current()`` returns a generation with its reference ALREADY held - selection and acquisition
happen under one lock, so a publication can never retire-and-reap a generation out from under
the caller between "look it up" and "register interest". And a request pin scope pins ONE
generation per repo across every ``read_cursor`` call in the scope - which is what covers POST
routes whose repo arrives in the JSON body, where the old query-string middleware pinned the
wrong repo. Pinned with a real published generation on disk.
"""

from __future__ import annotations

import json
import os
import time

import duckdb
import pytest

from app.index import store

SEP = chr(92)


def _write_generation(root: str, generation: int = 7) -> None:
    con = duckdb.connect()
    pool = os.path.join(root, "pool")
    for sub in ("src", "cid", "snap", "pathx", "filev", "maps/csize", "maps/crefs",
                "maps/iphys", "small/sanalysis", "small/isnaps"):
        os.makedirs(os.path.join(pool, sub), exist_ok=True)
    con.execute("CREATE TABLE src (src_id SMALLINT, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (?, ?)", [1, f"s1:C:{SEP}A"])
    con.execute("CREATE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    con.execute("INSERT INTO cid VALUES (10, 'obj_a')")
    con.execute("CREATE TABLE snap (snap_id INTEGER, snapshot_id VARCHAR, "
                "start_time TIMESTAMP, src_id SMALLINT)")
    con.execute("INSERT INTO snap VALUES (100, 'snap1', TIMESTAMP '2025-01-01', 1)")
    con.execute("CREATE TABLE pathx (path_id INTEGER, src_id BIGINT, dir_id INTEGER, "
                "name VARCHAR, ext VARCHAR, size UBIGINT, versions INTEGER, "
                "first_seen TIMESTAMP, last_seen TIMESTAMP, in_latest BOOLEAN, "
                "cid_id INTEGER, physical UBIGINT, exclusive UBIGINT, full_path VARCHAR)")
    con.execute("INSERT INTO pathx VALUES (1, 1, 1, 'a.txt', 'txt', 100, 1, "
                "TIMESTAMP '2025-01-01', TIMESTAMP '2025-01-01', true, 10, 100, NULL, ?)",
                [f"C:{SEP}A{SEP}a.txt"])
    con.execute("CREATE TABLE filev (src_id BIGINT, path_id INTEGER, snap_id INTEGER, "
                "cid_id INTEGER, size UBIGINT, mtime TIMESTAMP, ordinal BIGINT)")
    con.execute("INSERT INTO filev VALUES (1, 1, 100, 10, 100, TIMESTAMP '2025-01-01', 0)")
    for name in ("csize", "iphys"):
        con.execute(f"CREATE TABLE {name} (cid_id INTEGER, packed UBIGINT)")
    con.execute("CREATE TABLE crefs (cid_id INTEGER, refs INTEGER)")
    con.execute("CREATE TABLE sanalysis (repo VARCHAR, source VARCHAR, snapshot_id VARCHAR, "
                "churn UBIGINT, physical UBIGINT, reclaimable UBIGINT, snaphash VARCHAR, "
                "computed_at TIMESTAMP)")
    con.execute("CREATE TABLE isnaps (repo VARCHAR, snapshot_id VARCHAR, source VARCHAR, "
                "start_time TIMESTAMP, root_obj VARCHAR, indexed_at TIMESTAMP, "
                "file_count BIGINT)")
    rels = {"src": "src/g.parquet", "cid": "cid/g.parquet", "snap": "snap/g.parquet",
            "pathx": "pathx/src_id=1/g.parquet", "filev": "history/src_id=1/g.parquet",
            "csize": "maps/csize/g.parquet", "crefs": "maps/crefs/g.parquet",
            "iphys": "maps/iphys/g.parquet", "sanalysis": "small/sanalysis/g.parquet",
            "isnaps": "small/isnaps/g.parquet"}
    for table, rel in rels.items():
        target = os.path.join(pool, rel).replace(chr(92), "/")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        con.execute(f"COPY {table} TO '{target}' (FORMAT parquet)")
    con.close()
    manifest = {
        "generation": generation, "repo": "1", "schema_version": 2,
        "artifacts": {name: [f"pool/{rel}"] for name, rel in rels.items()},
        "sources": {f"s1:C:{SEP}A": {"key": f"s1:C:{SEP}A", "snapshots": 1}},
        "counters": {"src_id": 1, "dir_id": 1, "path_id": 1, "cid_id": 1, "snap_id": 1},
        "stats": {"paths": 1, "file_versions": 1},
    }
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


@pytest.fixture
def gen_repo(tmp_path, monkeypatch):
    root = str(tmp_path / "data" / "repo1")
    os.makedirs(root)
    _write_generation(root)
    monkeypatch.setattr(store, "manifest_path", lambda repo: os.path.join(root, "manifest.json"))
    monkeypatch.setattr(store, "repo_dir", lambda repo: root)
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path / "data"))
    return root


def _bump_manifest(root: str) -> None:
    """Republish: new generation content AND a newer mtime, as the atomic swap would."""
    p = os.path.join(root, "manifest.json")
    with open(p, encoding="utf-8") as fh:
        m = json.load(fh)
    m["generation"] = int(m["generation"]) + 1
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(m, fh)
    future = time.time() + 5
    os.utime(p, (future, future))


def test_current_returns_an_acquired_generation(gen_repo):
    gen = store.current("1")
    try:
        assert gen.refs == 1          # the reference is held on return, not left to the caller
        again = store.current("1")
        assert again is gen
        assert gen.refs == 2
        again.release()
    finally:
        gen.release()
    assert gen.refs == 0


def test_pin_scope_shares_one_generation_across_reads(gen_repo):
    token = store.begin_pins()
    try:
        c1 = store.read_cursor("1")
        c2 = store.read_cursor("1")
        g = c1._gen
        assert c2._gen is g                     # one generation per repo for the whole scope
        assert g.refs == 3                      # the scope's own reference + two cursors
        c1.close()
        c2.close()
        assert g.refs == 1
        c3 = store.read_cursor("1")             # still pinned after the cursors closed
        assert c3._gen is g
        c3.close()
    finally:
        store.end_pins(token)
    assert g.refs == 0


def test_pinned_scope_survives_a_publication(gen_repo):
    """The one failure the pin exists to prevent: a publish landing mid-request must not mix
    generations across reads."""
    token = store.begin_pins()
    try:
        c1 = store.read_cursor("1")
        g = c1._gen
        _bump_manifest(gen_repo)
        c2 = store.read_cursor("1")
        try:
            assert c2._gen is g                 # still the pinned generation, not the new one
            assert c2.execute("SELECT count(*) FROM paths").fetchone()[0] == 1
        finally:
            c1.close()
            c2.close()
    finally:
        store.end_pins(token)
    fresh = store.current("1")                  # outside the scope: the new generation resolves
    try:
        assert fresh.id == 8
    finally:
        fresh.release()


def test_end_pins_releases_when_the_request_raised(gen_repo):
    token = store.begin_pins()
    c = store.read_cursor("1")
    g = c._gen
    c.close()
    try:
        raise RuntimeError("route blew up")
    except RuntimeError:
        store.end_pins(token)
    assert g.refs == 0
