"""`build_repo` and `publish` edge cases.

The schema transform and its verifier were deleted in step 7 - layer 3 rebuilds the history
straight from the tree, so the old current-schema -> skinny-id transform has no caller. What is
pinned here is the behaviour around it: a repo with no sources still publishes an openable
generation, GC protects a manifest path written with backslashes, and `replace_artifacts` reads the
manifest under its own lock.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time

import duckdb

from app.index import artifacts


def test_empty_repo_publishes_an_openable_generation(tmp_path, monkeypatch):
    """A repo with zero sources (fresh install, no snapshots) must still publish a generation
    the store can open - the compatibility views reference the per-source artifacts."""
    from app.index import publish, store
    from app.index import layer2

    root = str(tmp_path / "repo1")
    os.makedirs(root)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: {})
    # An empty repo legitimately lists no content, so layer 2's stream is empty here.
    monkeypatch.setattr(layer2, "kopia_lines", lambda repo, args: iter([]))
    body = artifacts.build_repo("1", root, gen=1, job={}, full=True)
    assert body["stats"] == {"paths": 0, "file_versions": 0}
    publish.publish(root, body)
    arts = {name: [os.path.join(root, rel) for rel in rels]
            for name, rels in body["artifacts"].items()}
    con = duckdb.connect()
    try:
        store._bind_generation(con, "1", arts, encrypted=False)
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