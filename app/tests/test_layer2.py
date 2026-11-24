"""The object maps rebuilt from the tree.

``crefs`` decides what the app calls freeable, and a content id MISSING from it does not read as
unknown - it reads as zero references, i.e. safe to delete. So these tests lean on the things
that could silently drop an id: split objects, shared content, and the snapshot roots themselves.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index import ids, layer2, layer3, tree, treesync
from app.index.schema import init_schema
from app.tests.test_layer3 import build_tree, snap
from app.tests.test_treesync import _d, _f


A = "me@host:C:\\a"
B = "me@host:C:\\b"


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    c.execute("CREATE TABLE cid_rev (content_id VARCHAR, cid_id INTEGER)")
    tree.create_tables(c)
    init_schema(c)
    yield c
    c.close()


def refs(con):
    """content_id -> refs, as strings so the assertions read like the data."""
    return dict(con.execute("""SELECT c.content_id, r.refs FROM crefs_new r
                               JOIN cid c USING (cid_id)""").fetchall())


def prepare(con, dirs, srcs, parts=None, monkeypatch=None):
    build_tree(con, monkeypatch, dirs, sorted({s["rootEntry"]["obj"]
                                               for v in srcs.values() for s in v}), parts)
    layer3._seps(con, sorted(srcs))
    layer3.snapshots(con, "1", srcs)
    layer2.roots_table(con)
    layer2.source_reach(con)
    layer2.refcount(con)


def test_content_in_one_source_has_one_reference(con, monkeypatch):
    prepare(con, {"r1": [_f("a.txt", "c1")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")]}, monkeypatch=monkeypatch)
    assert refs(con)["c1"] == 1


def test_content_shared_by_two_sources_has_two_references(con, monkeypatch):
    # This is the number the delete preview prices against: content another source also holds is
    # not freed by deleting this one.
    prepare(con, {"r1": [_f("a.txt", "shared")], "r2": [_f("b.txt", "shared")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")],
             B: [snap("s2", "2026-01-01T00:00:00Z", "r2")]}, monkeypatch=monkeypatch)
    assert refs(con)["shared"] == 2


def test_the_same_content_twice_in_one_source_counts_once(con, monkeypatch):
    prepare(con, {"r1": [_f("a.txt", "dup"), _f("b.txt", "dup")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")]}, monkeypatch=monkeypatch)
    assert refs(con)["dup"] == 1      # references are per SOURCE, not per occurrence


def test_snapshot_roots_are_counted(con, monkeypatch):
    # No entry points at a root object, so a reach built only from entries drops it - and a root
    # with zero references reads as freeable.
    prepare(con, {"r1": [_f("a.txt", "c1")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")]}, monkeypatch=monkeypatch)
    assert refs(con)["r1"] == 1


def test_folder_objects_are_counted_not_just_files(con, monkeypatch):
    prepare(con, {"r1": [_d("sub", "ksub")], "ksub": [_f("a.txt", "c1")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")]}, monkeypatch=monkeypatch)
    got = refs(con)
    assert got["ksub"] == 1 and got["c1"] == 1


def test_a_split_object_contributes_its_index_content_and_every_piece(con, monkeypatch):
    prepare(con, {"r1": [_f("big.bin", "Ixidx", size=99)]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")]},
            parts={"xidx": ["p1", "p2"]}, monkeypatch=monkeypatch)
    got = refs(con)
    # The I-prefixed id is a POINTER, not stored content: what actually occupies space is the
    # index object (the id without the I) plus the pieces behind it. The old walker counts it
    # the same way - indirect objects never enter its content set directly.
    for k in ("xidx", "p1", "p2"):
        assert got.get(k) == 1, f"{k} missing from the refcount - it would read as freeable"
    assert "Ixidx" not in got


def test_pieces_shared_between_sources_count_both(con, monkeypatch):
    prepare(con, {"r1": [_f("x.bin", "Ixidx")], "r2": [_f("y.bin", "Ixidx")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")],
             B: [snap("s2", "2026-01-01T00:00:00Z", "r2")]},
            parts={"xidx": ["p1"]}, monkeypatch=monkeypatch)
    assert refs(con)["p1"] == 2


def test_indirect_physical_sums_the_pieces(con, monkeypatch):
    prepare(con, {"r1": [_f("big.bin", "Ixidx")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")]},
            parts={"xidx": ["p1", "p2"]}, monkeypatch=monkeypatch)
    con.execute("""CREATE TABLE csize_new AS
        SELECT cid_id, 100::UBIGINT AS packed FROM cid WHERE content_id IN ('xidx','p1','p2')""")
    layer2.indirect_phys(con)
    got = con.execute("""SELECT sum(packed) FROM iphys_new i JOIN cid c USING (cid_id)
                         WHERE c.content_id = 'Ixidx'""").fetchone()[0]
    assert got == 300                 # index content + both pieces


def test_a_piece_with_no_known_size_does_not_void_the_object(con, monkeypatch):
    # Dropping the whole iphys row would make a multi-gigabyte file read as zero physical bytes.
    prepare(con, {"r1": [_f("big.bin", "Ixidx")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")]},
            parts={"xidx": ["p1", "p2"]}, monkeypatch=monkeypatch)
    con.execute("""CREATE TABLE csize_new AS
        SELECT cid_id, 100::UBIGINT AS packed FROM cid WHERE content_id = 'p1'""")
    layer2.indirect_phys(con)
    got = con.execute("""SELECT packed FROM iphys_new i JOIN cid c USING (cid_id)
                         WHERE c.content_id = 'Ixidx'""").fetchone()[0]
    assert got == 100                 # the known piece still counts


def test_a_deeper_tree_is_reached_completely(con, monkeypatch):
    prepare(con, {"r1": [_d("a", "ka")], "ka": [_d("b", "kb")], "kb": [_f("deep.txt", "cdeep")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1")]}, monkeypatch=monkeypatch)
    got = refs(con)
    for k in ("r1", "ka", "kb", "cdeep"):
        assert got.get(k) == 1


def test_content_only_in_an_older_snapshot_is_still_counted(con, monkeypatch):
    # Reach is over every snapshot of the source, not just the newest - otherwise deleting the
    # newest snapshot would look like it frees content the older ones still hold.
    prepare(con, {"r1": [_f("old.txt", "cold")], "r2": [_f("new.txt", "cnew")]},
            {A: [snap("s1", "2026-01-01T00:00:00Z", "r1"),
                 snap("s2", "2026-01-02T00:00:00Z", "r2")]}, monkeypatch=monkeypatch)
    got = refs(con)
    assert got.get("cold") == 1 and got.get("cnew") == 1
