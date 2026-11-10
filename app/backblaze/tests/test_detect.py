""" detect_changes + the detect/refresh kv handshake (reindex_pending, freshness stamps).

The invariant: the detect flags a pending reindex exactly when refresh() would actually rewrite
rows - a moved watermark with a new body, a drive never indexed, or a rules change - and NEVER
for a header-only rewrite (Backblaze rewrites the scan-start timestamp every pass) or a stub.

Run with pytest or directly (`python app/backblaze/tests/test_detect.py`).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import pytest  # noqa: E402

from app.backblaze import bbdb, indexer, paths, queries, rules  # noqa: E402

HEADER = "# GmtMillisThisListWasStarted 1234567890"


def _body_lines(seed: str) -> list:
    """A filelist body over the 1000-byte stub threshold. Two real f-lines plus filler; a
    different seed produces a different body digest."""
    lines = [f"f\t{i:016x}\t{1000 + i}\tC:\\data\\file{i}.bin" for i in range(2)]
    lines += ["# " + seed * 100] * 12
    return lines


def _write_filelist(root: str, letter: str, seed: str) -> str:
    d = os.path.join(root, "bzfilelists")
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, f"v0005_{letter.lower()}____filelist.dat")
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(HEADER + "\n")
        for line in _body_lines(seed):
            fh.write(line + "\n")
    return fp


def _seed_watermark(con, fp: str) -> None:
    st = os.stat(fp)
    bbdb.ingest_set(con, "filelist:C", st.st_mtime, st.st_size, indexer._filelist_digest(fp))


@pytest.fixture
def bz_env(tmp_path, monkeypatch):
    """Fake bzdata with one drive + an in-memory bz DB on this thread.

    Pins the LEGACY sqlite store: these tests exercise the detect/handshake and the sqlite
    refresh path. The parquet refresh path has its own tests (test_pq_refresh.py)."""
    root = str(tmp_path / "bzdata")
    os.makedirs(os.path.join(root, "bzfilelists"))
    monkeypatch.setattr(paths, "_bzdata_root", root)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    bbdb._init_schema(con)
    bbdb._local.con = con
    # The fake tree carries no rules XML; a fixed empty engine keeps the rules digest stable.
    monkeypatch.setattr(rules, "load_engine", lambda: rules.RuleEngine([], [], []))
    queries._STORE_OVERRIDE = "sqlite"
    yield con, root
    queries._STORE_OVERRIDE = None
    bbdb._local.con = None


def _add_drive_row(con) -> None:
    con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                ("C", r"C:\data\file0.bin", r"c:\data\file0.bin", "file0.bin", "bin",
                 1000, "0", 0, None))
    con.commit()


def test_unchanged_drive_is_stat_only(bz_env):
    con, root = bz_env
    fp = _write_filelist(root, "C", "aaa")
    _seed_watermark(con, fp)
    _add_drive_row(con)
    out = indexer.detect_changes()
    assert out["ok"] and not out["changed"]
    assert not out["drives"]["C"]["watermark_moved"]
    assert bbdb.kv_get(con, "reindex_pending") != "1"


def test_header_rewrite_same_body_updates_watermark_without_pending(bz_env):
    con, root = bz_env
    fp = _write_filelist(root, "C", "aaa")
    _seed_watermark(con, fp)
    _add_drive_row(con)
    future = time.time() + 3600
    os.utime(fp, (future, future))  # Backblaze rewrote the scan-start header: mtime moved
    out = indexer.detect_changes()
    assert out["ok"] and not out["changed"]
    assert out["drives"]["C"]["watermark_moved"]
    row = con.execute("SELECT mtime FROM ingest WHERE source='filelist:C'").fetchone()
    assert abs(row["mtime"] - future) < 1e-6  # watermark advanced -> next detect is pure stat
    assert indexer.detect_changes()["drives"]["C"]["watermark_moved"] is False
    assert bbdb.kv_get(con, "reindex_pending") != "1"


def test_body_change_sets_pending_and_keeps_the_old_watermark(bz_env):
    con, root = bz_env
    fp = _write_filelist(root, "C", "aaa")
    _seed_watermark(con, fp)
    _add_drive_row(con)
    old_mtime = os.stat(fp).st_mtime
    _write_filelist(root, "C", "bbb")  # same path, different body
    out = indexer.detect_changes()
    assert out["changed"]
    assert bbdb.kv_get(con, "reindex_pending") == "1"
    row = con.execute("SELECT mtime FROM ingest WHERE source='filelist:C'").fetchone()
    assert abs(row["mtime"] - old_mtime) < 1e-6  # refresh() re-derives it when it reindexes


def test_stub_drive_flags_nothing(bz_env):
    con, root = bz_env
    fp = os.path.join(root, "bzfilelists", "v0005_c____filelist.dat")
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(HEADER)  # 77-byte-style aborted scan
    out = indexer.detect_changes()
    assert out["ok"] and not out["changed"]
    assert bbdb.kv_get(con, "reindex_pending") != "1"


def test_never_indexed_drive_counts_as_changed(bz_env):
    con, root = bz_env
    _write_filelist(root, "C", "aaa")  # no watermark, no rows -> the first build
    out = indexer.detect_changes()
    assert out["changed"]
    assert bbdb.kv_get(con, "reindex_pending") == "1"


def test_rules_change_sets_pending_but_bootstrap_does_not(bz_env):
    con, root = bz_env
    fp = _write_filelist(root, "C", "aaa")
    _seed_watermark(con, fp)
    _add_drive_row(con)
    engine = rules.RuleEngine([], [], [])
    # Bootstrap: no digest on record is NOT a rules change (mirrors refresh()'s upgrade rule).
    out = indexer.detect_changes()
    assert not out["rules_changed"] and not out["changed"]
    # A digest on record that differs IS one - even with every filelist body unchanged.
    bbdb.kv_set(con, "rules_digest", "0" * 64)
    out = indexer.detect_changes()
    assert out["rules_changed"] and out["changed"]
    assert bbdb.kv_get(con, "reindex_pending") == "1"


def test_refresh_clears_pending_and_stamps_the_anchor(bz_env):
    con, root = bz_env
    fp = _write_filelist(root, "C", "aaa")
    assert indexer.detect_changes()["changed"]  # never indexed -> pending
    assert bbdb.kv_get(con, "reindex_pending") == "1"
    summary = indexer.refresh()
    assert summary["ok"] and summary["changed"] == ["C"]
    assert bbdb.kv_get(con, "reindex_pending") == "0"
    anchor = float(bbdb.kv_get(con, "last_reindex_ts"))
    assert anchor > 0
    assert indexer.freshness()["last_reindex_ts"] == anchor
    # Now fully current: the next detect is quiet and a no-change refresh must NOT move the anchor.
    assert not indexer.detect_changes()["changed"]
    before = float(bbdb.kv_get(con, "last_reindex_ts"))
    assert indexer.refresh()["ok"]
    assert float(bbdb.kv_get(con, "last_reindex_ts")) == before


def test_forced_refresh_counts_as_a_reindex(bz_env):
    con, root = bz_env
    fp = _write_filelist(root, "C", "aaa")
    _seed_watermark(con, fp)
    _add_drive_row(con)
    assert indexer.refresh(force=True)["ok"]  # nothing changed, but a manual Index is a reindex
    assert float(bbdb.kv_get(con, "last_reindex_ts")) > 0


def test_freshness_on_an_unbuilt_db_reads_never(bz_env):
    con, root = bz_env
    fresh = indexer.freshness()
    assert fresh["last_check_ts"] is None and fresh["last_reindex_ts"] is None
    assert fresh["pending"] is False and fresh["waiting_for_scan"] is False
    assert fresh["folder_change_pending"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
