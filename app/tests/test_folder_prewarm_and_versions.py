"""Invariants behind the folder pre-compute and the all-versions/latest-only split.

Both of these were real bugs, and both are the silent kind: the pre-compute one leaves the stage
looking like it ran, and the version one only shows up as a number that is wrong by the churn
multiplier. Neither would fail a smoke test.
"""

from __future__ import annotations

import pytest

from app.index import foldercache, physical

SEP = chr(92)


# ---- the nightly folder pre-compute -----------------------------------------------------------
def test_prewarm_never_serves_a_stale_entry(monkeypatch):
    """The stale path hands the work to a daemon thread and returns immediately.

    The index stamp changes on every index run, so by the time prewarm runs EVERY cached folder is
    stale. Serving stale there would race through the whole list without touching the time budget,
    leave the background threads querying DuckDB while the worker closes the database and exits,
    and cache nothing - the stage would log success having done no work at all.
    """
    seen = []

    def fake_compute(repo, prefix, source=None, allow_stale=True):
        seen.append(allow_stale)
        return {"freeable": 0}

    monkeypatch.setattr(foldercache, "compute", fake_compute)
    monkeypatch.setattr(foldercache, "group_by_folder", lambda repo, **kw: {
        "rows": [{"folder": "C:" + SEP + "One", "files": 10},
                 {"folder": "C:" + SEP + "Two", "files": 20}]})
    monkeypatch.setattr(foldercache, "_prewarm_depths", lambda: (3,))

    foldercache.prewarm("1")

    assert seen, "prewarm computed nothing at all"
    assert all(flag is False for flag in seen), f"prewarm served stale entries: {seen}"


def test_prewarm_depths_follow_the_two_ui_depths_and_nothing_deeper(monkeypatch):
    """Depths track what the UI actually browses. The one-deeper level was dropped deliberately:
    the rollup caps at the path's own length, so depth N+1 returns substantially the same folders
    as depth N and spends a third of the budget recomputing them."""
    monkeypatch.setattr("app.settings.load_settings",
                        lambda: {"kopialens": {"group_depth": 3, "churn_depth": 8}})
    assert foldercache._prewarm_depths() == (3, 8)


def test_prewarm_depths_collapse_when_both_settings_agree(monkeypatch):
    monkeypatch.setattr("app.settings.load_settings",
                        lambda: {"kopialens": {"group_depth": 4, "churn_depth": 4}})
    assert foldercache._prewarm_depths() == (4,)


def test_prewarm_depths_reject_nonsense_and_fall_back(monkeypatch):
    """Depth <= 2 computed drive-level containers nobody would ever delete (a Users folder measured
    165s on its own), so those are filtered out rather than pre-computed."""
    monkeypatch.setattr("app.settings.load_settings",
                        lambda: {"kopialens": {"group_depth": 1, "churn_depth": 99}})
    assert foldercache._prewarm_depths() == foldercache._PREWARM_DEPTHS


# ---- latest-only vs all-versions pricing -------------------------------------------------------
class _FakeCursor:
    """Stands in for the DuckDB cursor: one path with three versions, newest first (the real query
    is ORDER BY start_time DESC)."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, _params):
        return self

    def fetchall(self):
        return self._rows

    def register(self, _name, _table):
        return self

    def unregister(self, _name):
        return self

    def close(self):
        pass


@pytest.fixture
def priced(monkeypatch):
    """A single file whose three versions each hold a different 1 KB chunk, plus one chunk shared
    by every version. Latest-only should price 2 chunks; all-versions should price 4."""
    # (source, full_path, content_id, size) - batch_detail scans every selected path in one
    # query now, ordered by start_time DESC within a path, so these are newest first.
    fp = "C:" + SEP + "f.dat"
    rows = [("src", fp, "obj_new", 1000),
            ("src", fp, "obj_mid", 1000),
            ("src", fp, "obj_old", 1000)]
    chunks = {"obj_new": {"c_common", "c_new"},
              "obj_mid": {"c_common", "c_mid"},
              "obj_old": {"c_common", "c_old"}}
    sizes = {"c_common": 100, "c_new": 100, "c_mid": 100, "c_old": 100}

    monkeypatch.setattr(physical, "read_cursor", lambda repo: _FakeCursor(rows))
    monkeypatch.setattr(physical, "cache_db", lambda repo: None)
    monkeypatch.setattr(physical, "load_sizemap", lambda repo: sizes)
    monkeypatch.setattr(physical, "_chunks_for_obj", lambda repo, db, obj: set(chunks[obj]))
    # Nothing outside the selection references these chunks, so everything found is freeable.
    monkeypatch.setattr(physical, "_refcount_for", lambda repo, cids: {c: 1 for c in cids})
    return [{"source": "src", "full_path": fp}]


def test_all_versions_prices_every_version(priced):
    out = physical.batch_detail("1", priced, all_versions=True)
    assert out["chunk_count"] == 4
    assert out["physical"] == 400
    assert out["versions"] == 3
    assert out["all_versions"] is True


def test_latest_only_prices_just_the_newest_version(priced):
    """A latest-only delete plan must be priced against latest-only objects. Pricing the
    all-versions union for it overstates freeable by the churn multiplier, which on a
    rewrite-heavy path is two orders of magnitude."""
    out = physical.batch_detail("1", priced, all_versions=False)
    assert out["chunk_count"] == 2
    assert out["physical"] == 200
    assert out["versions"] == 1
    assert out["all_versions"] is False


def test_latest_content_is_the_same_either_way(priced):
    """Only the chunk union narrows; the logical size still describes the current file."""
    every = physical.batch_detail("1", priced, all_versions=True)
    latest = physical.batch_detail("1", priced, all_versions=False)
    assert every["logical"] == latest["logical"] == 1000
    assert every["files"] == latest["files"] == 1


def test_all_versions_defaults_to_true(priced):
    """The batch panel deletes across all versions and passes nothing, so the default must not
    quietly become latest-only."""
    assert physical.batch_detail("1", priced)["chunk_count"] == 4
