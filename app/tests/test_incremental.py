"""The generation update (``build_repo``), now that sources are rebuilt from the tree.

The fake is at the KOPIA boundary - a dictionary of folder objects to their entries, which is
exactly what ``content show`` returns - so the tree sync, the level pass and the manifest
assembly all run for real. The previous generation is built by the real code too, rather than
hand-written, which means the full-rebuild path is exercised by every test here.

Assertions are on INVARIANTS rather than literal id numbers: ids stay stable across a rebuild,
unchanged sources carry by reference, ``first_seen`` never moves forward, a vanished snapshot
leaves no phantom. Pinning literal ids made these tests fail whenever allocation order changed
for a legitimate reason, which says nothing about correctness.
"""

from __future__ import annotations

import os

import duckdb
import pytest

from app.index import artifacts, layer2, publish, treesync
from app.tests.test_treesync import FakeKopia, _d, _f

SEP = chr(92)
S1, S2, S3 = f"s1:C:{SEP}A", f"s2:C:{SEP}B", f"s3:C:{SEP}D"
TS = {"snap0": "2024-12-15T00:00:00Z", "snap1": "2025-01-01T00:00:00Z",
      "snap2": "2025-01-02T00:00:00Z", "snap3": "2025-01-03T00:00:00Z",
      "snap4": "2025-01-04T00:00:00Z", "snap5": "2025-01-05T00:00:00Z"}

# What kopia would return for each folder object.
TREE = {
    "root_snap1": [_f("a.txt", "obj_a1", 100)],
    "root_snap2": [_f("a.txt", "obj_a2", 150), _f("b.txt", "obj_b", 200)],
    "root_snap0": [_f("x.txt", "obj_x", 50)],
    "root_snap3": [_f("c.txt", "obj_c", 300)],
    "root_snap4": [_f("c.txt", "obj_c", 300), _f("d.txt", "obj_d", 400), _d("sub", "root_sub")],
    "root_sub": [_f("d2.txt", "obj_d", 500)],
    "root_snap5": [_f("e.txt", "obj_e", 600)],
}
SMAP = {"obj_a1": 100, "obj_a2": 150, "obj_b": 200, "obj_c": 300,
        "obj_d": 400, "obj_e": 600, "obj_x": 50}


def _snap(sid: str) -> dict:
    return {"id": sid, "startTime": TS[sid], "rootEntry": {"obj": f"root_{sid}"}}


def _listing_published() -> dict:
    return {S1: [_snap("snap1"), _snap("snap2")],
            S2: [_snap("snap0"), _snap("snap3")]}


def _listing_update() -> dict:
    """The nightly case: snap4 lands on S2, S3 is brand new."""
    out = _listing_published()
    out[S2] = out[S2] + [_snap("snap4")]
    out[S3] = [_snap("snap5")]
    return out


def _clines(smap: dict) -> list[str]:
    """`kopia content list -l` lines for the given content -> packed sizes.

    Layer 2 reads the packed size from the RIGHT half of the one `original+packed` token on the
    line, with the content id in field 0 - the same rule ``core.sizemap`` has always used.
    """
    return [f"{cid} {size}+{size}" for cid, size in smap.items()]


def _patch(monkeypatch, tree: dict | None = None, smap: dict | None = None) -> None:
    import app.index.physical as physical
    monkeypatch.setattr(treesync, "kopia", FakeKopia(tree if tree is not None else dict(TREE)))
    monkeypatch.setattr(layer2, "kopia_lines",
                        lambda repo, args: iter(_clines(smap or SMAP)))
    monkeypatch.setattr(physical, "load_sizemap",
                        lambda repo, refresh=False: dict(smap or SMAP))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A published generation built by the real code, then the listing moved on."""
    root = str(tmp_path / "repo1")
    os.makedirs(root)
    _patch(monkeypatch)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    body = artifacts.build_repo("1", root, gen=1, job={})
    publish.publish(root, body)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_update())
    return root


def _rows(root: str, rel: str) -> list[tuple]:
    con = duckdb.connect()
    try:
        p = (os.path.join(root, rel)).replace(chr(92), "/").replace("'", "''")
        return con.execute(f"SELECT * FROM read_parquet('{p}', hive_partitioning=false)").fetchall()
    finally:
        con.close()


def _pathx(root: str, body: dict) -> dict:
    """full_path -> row, across every pathx artifact the manifest names."""
    con = duckdb.connect()
    try:
        fl = ", ".join("'%s'" % (os.path.join(root, r)).replace(chr(92), "/")
                       for r in body["artifacts"]["pathx"])
        rows = con.execute(f"SELECT * FROM read_parquet([{fl}], hive_partitioning=false)").fetchall()
    finally:
        con.close()
    return {r[13]: r for r in rows}


# ---------------------------------------------------------------------------- the first build

def test_a_first_build_indexes_every_source(tmp_path, monkeypatch):
    root = str(tmp_path / "fresh")
    os.makedirs(root)
    _patch(monkeypatch)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    body = artifacts.build_repo("1", root, gen=1, job={})
    assert body["generation"] == 1
    publish.publish(root, body)     # staging -> pool; the manifest paths are final only after
    px = _pathx(root, body)
    assert set(px) == {f"C:{SEP}A{SEP}a.txt", f"C:{SEP}A{SEP}b.txt",
                       f"C:{SEP}B{SEP}c.txt", f"C:{SEP}B{SEP}x.txt"}


def test_a_first_build_publishes_the_tree_with_the_generation(tmp_path, monkeypatch):
    root = str(tmp_path / "fresh")
    os.makedirs(root)
    _patch(monkeypatch)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    body = artifacts.build_repo("1", root, gen=1, job={})
    assert {"tdir", "tent", "tpart"} <= set(body["artifacts"])
    publish.publish(root, body)
    # every folder the published snapshots reach is in the tree
    assert len(_rows(root, body["artifacts"]["tdir"][0])) == 4   # the four snapshot roots


def test_a_deleted_file_survives_in_older_snapshots(tmp_path, monkeypatch):
    # x.txt exists only in snap0; snap3 is the latest and does not have it.
    root = str(tmp_path / "fresh")
    os.makedirs(root)
    _patch(monkeypatch)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    body = artifacts.build_repo("1", root, gen=1, job={})
    publish.publish(root, body)
    x = _pathx(root, body)[f"C:{SEP}B{SEP}x.txt"]
    assert x[9] is False                       # in_latest
    assert str(x[7])[:10] == "2024-12-15"      # first_seen, from the snapshot that holds it


# ---------------------------------------------------------------------------- the update

def test_an_unchanged_source_is_carried_by_reference(env):
    body = artifacts.build_repo("1", env, gen=1, job={})
    assert body["_mode"] == "incremental"
    assert body["generation"] == 2
    # S1 did not change: its artifact strings still point at generation 1's files
    assert any(r.endswith("g1.parquet") and "src_id=1" in r
               for r in body["artifacts"]["pathx"]), body["artifacts"]["pathx"]
    # S2 did change, so it was rewritten
    assert any(r.endswith("g2.parquet") and "src_id=2" in r
               for r in body["artifacts"]["pathx"])


def test_the_update_picks_up_new_files_and_a_new_source(env):
    body = artifacts.build_repo("1", env, gen=1, job={})
    publish.publish(env, body)
    px = _pathx(env, body)
    assert f"C:{SEP}B{SEP}d.txt" in px
    assert f"C:{SEP}B{SEP}sub{SEP}d2.txt" in px      # a folder that is new this pass
    assert f"C:{SEP}D{SEP}e.txt" in px               # the brand-new source


def test_path_ids_are_stable_across_an_update(env):
    before = _pathx(env, {"artifacts": {"pathx": _manifest(env)["artifacts"]["pathx"]}})
    body = artifacts.build_repo("1", env, gen=1, job={})
    publish.publish(env, body)
    after = _pathx(env, body)
    for full_path, row in before.items():
        assert after[full_path][0] == row[0], f"{full_path} was renumbered"


def test_first_seen_never_moves_forward(env):
    before = _pathx(env, {"artifacts": {"pathx": _manifest(env)["artifacts"]["pathx"]}})
    body = artifacts.build_repo("1", env, gen=1, job={})
    publish.publish(env, body)
    after = _pathx(env, body)
    for full_path, row in before.items():
        assert after[full_path][7] <= row[7], f"{full_path} first_seen moved forward"


def test_a_forced_rebuild_reproduces_the_same_ids(env, monkeypatch):
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    before = _pathx(env, {"artifacts": {"pathx": _manifest(env)["artifacts"]["pathx"]}})
    body = artifacts.build_repo("1", env, gen=1, job={}, force={S1})
    publish.publish(env, body)
    after = _pathx(env, body)
    assert set(after) == set(before)
    for full_path, row in before.items():
        assert after[full_path][0] == row[0]
        assert after[full_path][7] == row[7]


def test_an_empty_listing_against_a_published_generation_is_refused(env, monkeypatch):
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
    publish.publish(env, body)
    isnaps = {r[1] for r in _rows(env, body["artifacts"]["isnaps"][0])}
    assert isnaps == {"snap1", "snap2", "snap3"}      # snap0 gone, nothing phantom
    snaps = {r[1] for r in _rows(env, body["artifacts"]["snap"][0])}
    assert "snap0" not in snaps
    # a second pass over the SAME listing: every source carried, nothing rebuilt
    body2 = artifacts.build_repo("1", env, gen=2, job={})
    assert S2 not in body2["_checks"]["sources"]
    assert body2["stats"] == body["stats"]


def test_the_tree_drops_folders_no_snapshot_reaches(env, monkeypatch):
    """snap0's root stops being reachable once retention removes it, so the GC drops it."""
    listing = _listing_published()
    listing[S2] = [s for s in listing[S2] if s["id"] != "snap0"]
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: listing)
    body = artifacts.build_repo("1", env, gen=1, job={})
    publish.publish(env, body)
    con = duckdb.connect()
    try:
        td = (os.path.join(env, body["artifacts"]["tdir"][0])).replace(chr(92), "/")
        cd = (os.path.join(env, body["artifacts"]["cid"][0])).replace(chr(92), "/")
        objs = {r[0] for r in con.execute(
            f"SELECT c.content_id FROM read_parquet('{td}') d "
            f"JOIN read_parquet('{cd}') c USING (cid_id)").fetchall()}
    finally:
        con.close()
    assert "root_snap0" not in objs
    assert {"root_snap1", "root_snap2", "root_snap3"} <= objs


def test_a_chunk_known_only_to_the_content_listing_still_reaches_the_dictionary(env, monkeypatch):
    """A split file's underlying chunk ids exist only in the content listing - the tree holds the
    split object, not its pieces' sizes. The cid dictionary must grow to hold them, and csize
    must not drop them in its join (a missing chunk reads as zero references = 'safe to
    delete')."""
    monkeypatch.setattr(layer2, "kopia_lines",
                        lambda repo, args: iter(_clines({**SMAP, "chunk_z": 42})))
    body = artifacts.build_repo("1", env, gen=1, job={})
    publish.publish(env, body)
    cid = {r[1]: r[0] for r in _rows(env, body["artifacts"]["cid"][0])}
    assert "chunk_z" in cid
    csize = {r[0]: r[1] for r in _rows(env, body["artifacts"]["csize"][0])}
    assert csize[cid["chunk_z"]] == 42


def test_src_id_is_written_as_smallint(env):
    """A mixed generation binds one artifact kind across several files, so the column types
    have to agree or read_parquet refuses the union."""
    body = artifacts.build_repo("1", env, gen=1, job={})
    publish.publish(env, body)
    rewritten = [r for r in body["artifacts"]["pathx"] if r.endswith("g2.parquet")][0]
    con = duckdb.connect()
    try:
        p = (os.path.join(env, rewritten)).replace(chr(92), "/")
        # hive_partitioning=false: the src_id=N directory auto-detects as a partition column
        # and would override the file's own column type (the documented DuckDB trap)
        cols = {r[0]: r[1] for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{p}', hive_partitioning=false)").fetchall()}
    finally:
        con.close()
    assert cols["src_id"] == "SMALLINT"


def test_a_shared_folder_object_indexes_every_path_it_sits_at(tmp_path, monkeypatch):
    """Kopia stores identical directories once. Indexing only the first location is how 492,587
    file rows once went missing."""
    tree = {"root_snapA": [_d("one", "kshared"), _d("two", "kshared")],
            "kshared": [_f("f.txt", "obj_f", 10)]}
    root = str(tmp_path / "fresh")
    os.makedirs(root)
    _patch(monkeypatch, tree=tree, smap={"obj_f": 10})
    listing = {S1: [{"id": "snapA", "startTime": TS["snap1"],
                     "rootEntry": {"obj": "root_snapA"}}]}
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: listing)
    body = artifacts.build_repo("1", root, gen=1, job={})
    publish.publish(root, body)
    assert set(_pathx(root, body)) == {f"C:{SEP}A{SEP}one{SEP}f.txt",
                                       f"C:{SEP}A{SEP}two{SEP}f.txt"}


def test_crefs_counts_the_sources_that_reach_a_content(tmp_path, monkeypatch):
    """`crefs` is the delete-safety number: it must count SOURCES, not rows. The same content
    reachable from two sources has to read 2, or a content shared across backups looks freeable."""
    tree = {"root_snap1": [_f("dup.txt", "obj_shared", 10)],
            "root_snap2": [_f("dup.txt", "obj_shared", 10)]}
    root = str(tmp_path / "fresh")
    os.makedirs(root)
    _patch(monkeypatch, tree=tree, smap={"obj_shared": 10})
    listing = {S1: [_snap("snap1")], S2: [_snap("snap2")]}
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: listing)
    body = artifacts.build_repo("1", root, gen=1, job={})
    publish.publish(root, body)
    cid = {r[1]: r[0] for r in _rows(root, body["artifacts"]["cid"][0])}
    crefs = {r[0]: r[1] for r in _rows(root, body["artifacts"]["crefs"][0])}
    assert crefs[cid["obj_shared"]] == 2


def test_a_split_file_is_priced_from_its_piece_list(tmp_path, monkeypatch):
    """`iphys` comes from the tree's piece list (`tpart`), and it is what makes a split file's
    physical size right: the index object alone would read as a few bytes for a multi-GB file."""
    tree = {"root_snap1": [_f("big.bin", "Ixidx", 99)]}
    smap = {"xidx": 5, "p1": 40, "p2": 55}
    root = str(tmp_path / "fresh")
    os.makedirs(root)
    _patch(monkeypatch, tree=tree, smap=smap)
    monkeypatch.setattr(treesync, "kopia", FakeKopia(tree, parts={"xidx": ["p1", "p2"]}))
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: {S1: [_snap("snap1")]})
    body = artifacts.build_repo("1", root, gen=1, job={})
    publish.publish(root, body)
    cid = {r[1]: r[0] for r in _rows(root, body["artifacts"]["cid"][0])}
    iphys = {r[0]: r[1] for r in _rows(root, body["artifacts"]["iphys"][0])}
    assert iphys[cid["Ixidx"]] == 100                     # 5 + 40 + 55, index object included
    px = _pathx(root, body)[f"C:{SEP}A{SEP}big.bin"]
    assert px[11] == 100                                  # physical, from the pieces


def _manifest(root: str) -> dict:
    import json
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)
