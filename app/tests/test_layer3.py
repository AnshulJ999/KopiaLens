"""Step 3 of: layer 3 rebuilt from the tree.

The full proof is the P1 parity run against a real frozen index (19M rows). These pin the rules
that make that run correct, on trees small enough to reason about by hand - delta attribution,
first-seen from the oldest reaching snapshot, shared folder objects at several paths, and the
snapshot tiebreak.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index import ids, layer3, tree, treesync
from app.index.schema import init_schema
from app.tests.test_treesync import FakeKopia, _d, _f


SRC = "me@host:C:\\data"


def snap(sid, when, root):
    return {"id": sid, "startTime": when, "rootEntry": {"obj": root},
            "source": {"userName": "me", "host": "host", "path": "C:\\data"}}


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    c.execute("CREATE TABLE cid_rev (content_id VARCHAR, cid_id INTEGER)")
    tree.create_tables(c)
    init_schema(c)
    yield c
    c.close()


def build_tree(con, monkeypatch, dirs, roots, parts=None):
    monkeypatch.setattr(treesync, "kopia", FakeKopia(dirs, parts))
    treesync.sync(con, "3", roots)
    treesync.materialize(con, ids.Allocator({}))
    treesync.merge(con)
    for name in ("tdir", "tent", "tpart"):
        con.execute(f"DROP TABLE {name}")
        con.execute(f"ALTER TABLE {name}_final RENAME TO {name}")


def rows(con):
    return con.execute("""SELECT full_path, snapshot_id, size FROM files
                          ORDER BY full_path, snapshot_id""").fetchall()


def test_an_unchanged_file_is_credited_to_the_newest_snapshot_only(con, monkeypatch):
    # The delta index holds ONE row for a file that did not change, on the newest snapshot that
    # reaches it - not one row per snapshot. That is what keeps 19M rows from becoming 200M.
    build_tree(con, monkeypatch, {"r1": [_f("a.txt", "c1", size=5)]}, ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-01-01T00:00:00Z", "r1"),
                                  snap("s2", "2026-01-02T00:00:00Z", "r1")]})
    assert rows(con) == [("C:\\data\\a.txt", "s2", 5)]


def test_a_changed_file_gets_a_row_per_version(con, monkeypatch):
    build_tree(con, monkeypatch,
               {"r1": [_f("a.txt", "c1", size=5)], "r2": [_f("a.txt", "c2", size=9)]},
               ["r1", "r2"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-01-01T00:00:00Z", "r1"),
                                  snap("s2", "2026-01-02T00:00:00Z", "r2")]})
    assert rows(con) == [("C:\\data\\a.txt", "s1", 5), ("C:\\data\\a.txt", "s2", 9)]


def test_first_seen_is_the_oldest_snapshot_reaching_the_file(con, monkeypatch):
    # The date comes from rmax, NOT from min(start_time) over the delta rows - which is exactly
    # the bug that made the old index need a six-minute oldest-first backfill.
    build_tree(con, monkeypatch, {"r1": [_f("a.txt", "c1")]}, ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2020-05-05T00:00:00Z", "r1"),
                                  snap("s2", "2026-01-02T00:00:00Z", "r1")]})
    fs = con.execute("SELECT first_seen FROM _tree_seen").fetchone()[0]
    assert str(fs).startswith("2020-05-05")
    # ... while the delta row itself sits on the NEWEST snapshot
    assert rows(con) == [("C:\\data\\a.txt", "s2", 1)]


def test_first_seen_is_right_when_two_snapshots_land_in_one_pass(con, monkeypatch):
    # The live bug removes: a file new to the source appearing in two snapshots added
    # together, inside an otherwise unchanged folder, used to be dated to the LATER one.
    build_tree(con, monkeypatch,
               {"r1": [_d("sub", "ksub")], "ksub": [_f("new.txt", "c9")]}, ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-03-01T09:00:00Z", "r1"),
                                  snap("s2", "2026-03-01T17:00:00Z", "r1")]})
    fs = con.execute("SELECT first_seen FROM _tree_seen").fetchone()[0]
    assert str(fs).startswith("2026-03-01 09:00"), "dated to the later snapshot of the pair"


def test_a_shared_folder_object_yields_rows_at_every_path(con, monkeypatch):
    # Kopia stores identical directories once. An object-keyed walk indexes only the first
    # location; locations are PATH-keyed for exactly this reason (it once cost 492,587 rows).
    build_tree(con, monkeypatch,
               {"r1": [_d("x", "kshared"), _d("y", "kshared")], "kshared": [_f("f.txt", "c1")]},
               ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-01-01T00:00:00Z", "r1")]})
    assert [r[0] for r in rows(con)] == ["C:\\data\\x\\f.txt", "C:\\data\\y\\f.txt"]


def test_nested_paths_use_the_source_separator(con, monkeypatch):
    build_tree(con, monkeypatch,
               {"r1": [_d("a", "ka")], "ka": [_d("b", "kb")], "kb": [_f("deep.txt", "c1")]},
               ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-01-01T00:00:00Z", "r1")]})
    assert rows(con)[0][0] == "C:\\data\\a\\b\\deep.txt"


def test_a_posix_source_keeps_forward_slashes(con, monkeypatch):
    posix = "me@host:/srv/data"
    build_tree(con, monkeypatch, {"r1": [_d("a", "ka")], "ka": [_f("x.txt", "c1")]}, ["r1"])
    layer3.build(con, "1", {posix: [{"id": "s1", "startTime": "2026-01-01T00:00:00Z",
                                     "rootEntry": {"obj": "r1"},
                                     "source": {"userName": "me", "host": "host",
                                                "path": "/srv/data"}}]})
    assert rows(con)[0][0] == "/srv/data/a/x.txt"


def test_directories_and_symlinks_do_not_become_file_rows(con, monkeypatch):
    build_tree(con, monkeypatch,
               {"r1": [_d("sub", "ksub"), {"name": "ln", "type": "s", "mode": "0777", "obj": "c2"},
                       _f("real.txt", "c1")],
                "ksub": []}, ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-01-01T00:00:00Z", "r1")]})
    assert [r[0] for r in rows(con)] == ["C:\\data\\real.txt"]


def test_tied_snapshots_break_on_snapshot_id_by_default(con, monkeypatch):
    # Kopia checkpoints share a start time exactly. The rule is fixed and reproducible:
    # start_time, then snapshot_id - never whatever order kopia listed them in.
    build_tree(con, monkeypatch, {"r1": [_f("a.txt", "c1")]}, ["r1"])
    tied = [snap("zzz", "2026-01-01T00:00:00Z", "r1"), snap("aaa", "2026-01-01T00:00:00Z", "r1")]
    layer3.build(con, "1", {SRC: tied}, tie=layer3.TIE_ID)
    # Within a tie group the LOWEST snapshot_id takes the newest rank, and it does so regardless
    # of the order kopia listed them in - which is the whole point.
    assert rows(con) == [("C:\\data\\a.txt", "aaa", 1)]


def test_the_listing_tiebreak_reproduces_todays_order(con, monkeypatch):
    build_tree(con, monkeypatch, {"r1": [_f("a.txt", "c1")]}, ["r1"])
    tied = [snap("zzz", "2026-01-01T00:00:00Z", "r1"), snap("aaa", "2026-01-01T00:00:00Z", "r1")]
    layer3.build(con, "1", {SRC: tied}, tie=layer3.TIE_LISTING)
    assert rows(con) == [("C:\\data\\a.txt", "zzz", 1)]   # first in the listing is newest


def test_a_snapshot_whose_root_is_missing_is_dropped_loudly(con, monkeypatch):
    build_tree(con, monkeypatch, {"r1": [_f("a.txt", "c1")]}, ["r1"])
    kept = layer3.snapshots(con, "1", {SRC: [snap("s1", "2026-01-01T00:00:00Z", "r1"),
                                             snap("s2", "2026-01-02T00:00:00Z", "gone")]})
    assert kept == 1        # the unknown root contributes nothing rather than silently zero rows


def test_indexed_snapshots_records_every_snapshot(con, monkeypatch):
    build_tree(con, monkeypatch, {"r1": [_f("a.txt", "c1")]}, ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-01-01T00:00:00Z", "r1"),
                                  snap("s2", "2026-01-02T00:00:00Z", "r1")]})
    got = con.execute("SELECT snapshot_id, file_count FROM indexed_snapshots "
                      "ORDER BY snapshot_id").fetchall()
    assert [g[0] for g in got] == ["s1", "s2"]
    assert dict(got)["s2"] == 1 and dict(got)["s1"] == 0   # the delta row sits on the newest


def test_mtime_survives_the_round_trip_into_files(con, monkeypatch):
    build_tree(con, monkeypatch,
               {"r1": [_f("a.txt", "c1", mtime="2024-02-25T11:56:36.4531752Z")]}, ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-01-01T00:00:00Z", "r1")]})
    mt = con.execute("SELECT mtime FROM files").fetchone()[0]
    assert str(mt).startswith("2024-02-25 11:56:36.453175")


def test_the_paths_aggregate_takes_the_older_of_index_and_tree_dates(con, monkeypatch):
    build_tree(con, monkeypatch, {"r1": [_f("a.txt", "c1")]}, ["r1"])
    layer3.build(con, "1", {SRC: [snap("s1", "2026-06-01T00:00:00Z", "r1")]})
    con.execute("INSERT INTO paths (repo, source, full_path, first_seen) "
                "VALUES ('1', ?, 'C:\\data\\a.txt', TIMESTAMP '2019-01-01 00:00:00')", [SRC])
    layer3.merge_old_seen(con, "1", SRC)
    fs = con.execute("SELECT first_seen FROM _old_seen").fetchone()[0]
    assert str(fs).startswith("2019-01-01"), "a known date must never move forward"
