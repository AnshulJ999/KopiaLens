""": per-snapshot churn, physical and reclaimable as set operations over the tree.

The shape this replaced ran two whole-tree reaches plus a Python folder walk PER snapshot. These
pin the answers, not the speed - a rewrite from three passes to three group-bys can be fast and
wrong, and two of the rules below are deliberate changes in behaviour rather than invariants:
a folder at two paths is now counted once, and tied checkpoint snapshots order by snapshot id.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index import ids, snapanalysis, tree, treesync
from app.index.schema import init_schema
from app.tests.test_layer3 import build_tree, snap
from app.tests.test_treesync import _d, _f


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    c.execute("CREATE TABLE cid_rev (content_id VARCHAR, cid_id INTEGER)")
    tree.create_tables(c)
    init_schema(c)
    yield c
    c.close()


class _Cur:
    """``read_cursor`` double bound to one connection; close is a no-op so the fixture survives."""

    def __init__(self, con):
        self._con = con

    def __getattr__(self, name):
        return getattr(self._con, name)

    def close(self):
        pass


def analyse(con, monkeypatch, dirs, snaps, sizes, *, parts=None, refs=None, heavy=True):
    """Build a tree, give its content sizes and refcounts, and run the analysis over it.

    ``sizes``/``refs`` are keyed by content id rather than by the integer the dictionary assigned,
    so a failing assertion names the object the test wrote.
    """
    build_tree(con, monkeypatch, dirs, [s["rootEntry"]["obj"] for s in snaps], parts)
    con.execute("CREATE OR REPLACE TABLE csize (cid_id INTEGER, packed UBIGINT)")
    con.execute("CREATE OR REPLACE TABLE crefs (cid_id INTEGER, refs INTEGER)")
    for content_id, packed in sizes.items():
        con.execute("INSERT INTO csize SELECT cid_id, ? FROM cid WHERE content_id = ?",
                    [packed, content_id])
    for content_id, n in (refs or {}).items():
        con.execute("INSERT INTO crefs SELECT cid_id, ? FROM cid WHERE content_id = ?",
                    [n, content_id])
    # A split object's footprint is the sum of its pieces - the same definition layer 2 writes.
    con.execute("""CREATE OR REPLACE TABLE iphys AS
        SELECT p.iobj_cid AS cid_id, sum(coalesce(s.packed, 0))::UBIGINT AS packed
        FROM tpart p LEFT JOIN csize s ON s.cid_id = p.part_cid GROUP BY p.iobj_cid""")
    monkeypatch.setattr(snapanalysis, "read_cursor", lambda repo: _Cur(con))
    return snapanalysis._analyse("3", snaps, heavy=heavy)


# One file added in the newer snapshot, one carried over from the older one.
TWO_SNAPS = (
    {"r1": [_f("a.txt", "c1")], "r2": [_f("a.txt", "c1"), _f("b.txt", "c2")]},
    [snap("s1", "2026-01-01T00:00:00Z", "r1"), snap("s2", "2026-01-02T00:00:00Z", "r2")],
    {"c1": 100, "c2": 50},
)


def test_churn_is_credited_to_the_snapshot_that_introduced_the_content(con, monkeypatch):
    churn, _phys, _recl = analyse(con, monkeypatch, *TWO_SNAPS)
    assert churn == {"s1": 100, "s2": 50}


def test_reclaimable_is_the_content_that_lives_in_exactly_one_snapshot(con, monkeypatch):
    # c1 survives into s2, so deleting s1 frees nothing; c2 exists only in s2.
    _churn, _phys, recl = analyse(con, monkeypatch, *TWO_SNAPS)
    assert recl == {"s1": 0, "s2": 50}


def test_content_another_source_also_holds_is_not_reclaimable(con, monkeypatch):
    dirs, snaps, sizes = TWO_SNAPS
    _churn, _phys, recl = analyse(con, monkeypatch, dirs, snaps, sizes, refs={"c2": 2})
    assert recl["s2"] == 0, "a chunk two sources reach is not freed by deleting one snapshot"


def test_a_chunk_missing_from_the_refcount_map_counts_as_unreferenced(con, monkeypatch):
    # The map is written to over-count, so an absent id means zero references, NOT unknown.
    # Reading it the other way would understate what a delete frees.
    _churn, _phys, recl = analyse(con, monkeypatch, *TWO_SNAPS, refs={})
    assert recl["s2"] == 50


def test_physical_counts_a_folder_at_two_paths_only_once(con, monkeypatch):
    # Kopia stores identical directories once, so one object legitimately sits at several paths.
    # The Python walk this replaced added its bytes per path; SQL counts the folder once per
    # snapshot, which is the correct footprint ( rule 4, a deliberate change).
    churn, phys, _recl = analyse(
        con, monkeypatch,
        {"r1": [_d("x", "kd"), _d("y", "kd")], "kd": [_f("big.bin", "c9")]},
        [snap("s1", "2026-01-01T00:00:00Z", "r1")], {"c9": 1000})
    assert phys == {"s1": 1000}
    assert churn == {"s1": 1000}


def test_a_split_object_contributes_its_index_content_and_every_piece(con, monkeypatch):
    # Priced as just its index object, a multi-gigabyte file reads as a few kilobytes.
    churn, phys, _recl = analyse(
        con, monkeypatch, {"r1": [_f("big.bin", "Ixidx", size=99)]},
        [snap("s1", "2026-01-01T00:00:00Z", "r1")],
        {"xidx": 10, "p1": 100, "p2": 200}, parts={"xidx": ["p1", "p2"]})
    assert churn == {"s1": 310}
    assert phys == {"s1": 310}


def test_snapshots_sharing_a_start_time_are_ordered_by_snapshot_id(con, monkeypatch):
    # Kopia checkpoints share a start time exactly, so the tiebreak decides which snapshot gets
    # credited with the content. Layer 3 ranks newest-first by (start time DESC, id ASC), so the
    # HIGHER id is the older snapshot and the content is attributed there ( rule 3).
    tie = "2026-01-01T00:00:00Z"
    churn, _phys, _recl = analyse(
        con, monkeypatch, {"ra": [_f("a.txt", "c1")], "rb": [_f("a.txt", "c1")]},
        [snap("s-a", tie, "ra"), snap("s-b", tie, "rb")], {"c1": 100})
    assert churn == {"s-a": 0, "s-b": 100}


def test_the_light_pass_computes_churn_and_leaves_the_heavy_fields_alone(con, monkeypatch):
    # compute_source restores the persisted physical/reclaimable on a churn-only pass, so
    # _analyse must not invent zeros that look computed.
    churn, phys, recl = analyse(con, monkeypatch, *TWO_SNAPS, heavy=False)
    assert churn == {"s1": 100, "s2": 50}
    assert phys == {"s1": 0, "s2": 0} and recl == {"s1": 0, "s2": 0}


def test_a_source_whose_snapshots_have_no_root_object_analyses_to_zero(con, monkeypatch):
    build_tree(con, monkeypatch, {"r1": [_f("a.txt", "c1")]}, ["r1"])
    monkeypatch.setattr(snapanalysis, "read_cursor", lambda repo: _Cur(con))
    rootless = {"id": "s1", "startTime": "2026-01-01T00:00:00Z", "rootEntry": {}}
    assert snapanalysis._analyse("3", [rootless], heavy=True) == ({"s1": 0}, {"s1": 0}, {"s1": 0})
