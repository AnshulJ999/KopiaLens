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

    monkeypatch.setattr(foldercache, "compute", fake_compute)
    monkeypatch.setattr(foldercache, "_load", lambda repo: {})   # hermetic: no real cache reads
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
@pytest.fixture
def priced(monkeypatch):
    """A single file whose three versions each hold a different 1 KB chunk, plus one chunk shared
    by every version. Latest-only should price 2 chunks; all-versions should price 4."""
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, 'src')")
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    con.execute("INSERT INTO cid VALUES (1,'obj_new'),(2,'obj_mid'),(3,'obj_old')")
    con.execute("CREATE TABLE pathx(path_id INTEGER, src_id INTEGER, full_path VARCHAR)")
    con.execute("INSERT INTO pathx VALUES (1, 1, ?)", ["C:" + SEP + "f.dat"])
    con.execute("CREATE TABLE snap(snap_id INTEGER, snapshot_id VARCHAR, start_time TIMESTAMP)")
    # Newest version has the latest start_time: the per-path DESC ordering must pick obj_new.
    con.execute("INSERT INTO snap VALUES (1, 's-old', '2026-01-01 00:00:00'), "
                "(2, 's-mid', '2026-02-01 00:00:00'), (3, 's-new', '2026-03-01 00:00:00')")
    con.execute("CREATE TABLE filev(path_id INTEGER, snap_id INTEGER, cid_id INTEGER, size BIGINT)")
    con.execute("INSERT INTO filev VALUES (1, 3, 1, 1000), (1, 2, 2, 1000), (1, 1, 3, 1000)")
    chunks = {"obj_new": {"c_common", "c_new"},
              "obj_mid": {"c_common", "c_mid"},
              "obj_old": {"c_common", "c_old"}}
    sizes = {"c_common": 100, "c_new": 100, "c_mid": 100, "c_old": 100}

    monkeypatch.setattr(physical, "read_cursor", lambda repo: con.cursor())
    monkeypatch.setattr(physical, "cache_db", lambda repo: None)
    monkeypatch.setattr(physical, "load_sizemap", lambda repo: sizes)
    monkeypatch.setattr(physical, "_chunks_for_obj", lambda repo, db, obj: set(chunks[obj]))
    # Nothing outside the selection references these chunks, so everything found is freeable.
    monkeypatch.setattr(physical, "_refcount_for", lambda repo, cids: {c: 1 for c in cids})
    return [{"source": "src", "full_path": "C:" + SEP + "f.dat"}]


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


# ---- per-folder stamps + prewarm skip-when-unchanged ------------------------------

class _Cursor:
    """Minimal read_cursor double bound to one duckdb connection (close is a no-op)."""

    def __init__(self, con):
        self._con = con

    def execute(self, *a, **kw):
        return self._con.execute(*a, **kw)

    def close(self):
        pass


def test_folder_stamp_covers_only_sources_under_the_prefix(monkeypatch):
    """The stamp changes only when a COVERING source's snapshot set changes - a stamp identical
    for unchanged folders is the whole point, and one that moves on any other source's change is
    the old repo-wide behaviour sneaking back in."""
    import duckdb

    from app.index import foldercache

    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.execute(f"INSERT INTO src VALUES (1, 'a@h:C:{SEP}One'), (2, 'b@h:C:{SEP}Two')")
    con.execute("CREATE TABLE dir(dir_id INTEGER, src_id INTEGER, dir_path VARCHAR)")
    con.execute(f"INSERT INTO dir VALUES "
                f"(1, 1, 'C:{SEP}One'), (2, 1, 'C:{SEP}One{SEP}Sub'), (3, 2, 'C:{SEP}Two')")
    con.execute("CREATE TABLE isnaps(source VARCHAR, snapshot_id VARCHAR)")
    con.execute(f"INSERT INTO isnaps VALUES "
                f"('a@h:C:{SEP}One', 's1'), ('a@h:C:{SEP}One', 's2'), ('b@h:C:{SEP}Two', 's9')")
    monkeypatch.setattr(foldercache, "read_cursor", lambda repo: _Cursor(con))

    one = foldercache.folder_stamp("1", f"C:{SEP}One")
    assert one != "0"
    assert one == foldercache.folder_stamp("1", f"C:{SEP}One")     # stable on no change
    assert one != foldercache.folder_stamp("1", f"C:{SEP}Two")     # different folder, other source
    con.execute(f"INSERT INTO isnaps VALUES ('a@h:C:{SEP}One', 's3')")
    assert foldercache.folder_stamp("1", f"C:{SEP}One") != one     # covering source changed


def _skip_store(monkeypatch, entries: dict) -> dict:
    """An in-memory cache store + fakes so prewarm never touches disk or DuckDB."""
    store = dict(entries)
    monkeypatch.setattr(foldercache, "_load", lambda repo: store)
    monkeypatch.setattr(foldercache, "_save", lambda repo: None)
    return store


def _prewarm_env(monkeypatch, folders: list[str]):
    computed: list[str] = []
    monkeypatch.setattr(
        foldercache, "group_by_folder", lambda repo, **kw:
        {"rows": [{"folder": f, "files": 10} for f in folders]})
    monkeypatch.setattr(foldercache, "_prewarm_depths", lambda: (3,))
    monkeypatch.setattr(foldercache, "compute",
                        lambda repo, prefix, allow_stale=True, source=None:
                        computed.append(prefix))
    return computed


def test_prewarm_skips_unchanged_folders_and_refreshes_changed_ones(monkeypatch):
    import time as _time

    from app.index import foldercache

    now = _time.time()
    _skip_store(monkeypatch, {
        foldercache._key(f"C:{SEP}One", None): {"stamp": "S1", "computed_at": now},
        foldercache._key(f"C:{SEP}Two", None): {"stamp": "OLD", "computed_at": now},
    })
    stamps = {f"C:{SEP}One": "S1", f"C:{SEP}Two": "S2"}
    monkeypatch.setattr(foldercache, "folder_stamp",
                        lambda repo, prefix, source=None: stamps[prefix])
    computed = _prewarm_env(monkeypatch, [f"C:{SEP}One", f"C:{SEP}Two"])

    foldercache.prewarm("1")

    assert computed == [f"C:{SEP}Two"]   # One unchanged (skipped), Two changed (recomputed)


def test_prewarm_fully_refreshes_entries_older_than_the_drift_bound(monkeypatch):
    """Option (a): unchanged folders may serve slightly stale freeable/shared - but only for a
    bounded time. An entry past _FULL_REFRESH_DAYS recomputes even with a matching stamp."""
    import time as _time

    from app.index import foldercache

    old = _time.time() - (foldercache._FULL_REFRESH_DAYS + 1) * 86400.0
    _skip_store(monkeypatch, {
        foldercache._key(f"C:{SEP}One", None): {"stamp": "S1", "computed_at": old},
    })
    monkeypatch.setattr(foldercache, "folder_stamp",
                        lambda repo, prefix, source=None: "S1")   # stamp says unchanged
    computed = _prewarm_env(monkeypatch, [f"C:{SEP}One"])

    foldercache.prewarm("1")

    assert computed == [f"C:{SEP}One"]   # aged out -> recomputed anyway


def test_prewarm_skipped_unchanged_count_is_reported(monkeypatch):
    """The stage summary separates 'skipped (unchanged)' from 'computed' so a night that skipped
    everything reads as a healthy cheap pass, not as a pass that did nothing."""
    import time as _time

    from app.index import foldercache

    _skip_store(monkeypatch, {
        foldercache._key(f"C:{SEP}One", None): {"stamp": "S1", "computed_at": _time.time()},
    })
    monkeypatch.setattr(foldercache, "folder_stamp",
                        lambda repo, prefix, source=None: "S1")
    _prewarm_env(monkeypatch, [f"C:{SEP}One"])
    lines: list[str] = []

    def _info(fmt, *a):
        lines.append(fmt % a if a else str(fmt))

    monkeypatch.setattr(foldercache, "log",
                        type("L", (), {"info": staticmethod(_info),
                                       "warning": staticmethod(lambda *a, **kw: None),
                                       "debug": staticmethod(lambda *a, **kw: None)})())

    foldercache.prewarm("1")

    summary = " ".join(lines)
    assert "1 skipped (unchanged)" in summary and "0 computed" in summary
