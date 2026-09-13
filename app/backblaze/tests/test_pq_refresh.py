"""++ tests for the parquet refresh path, the worker progress file, and the
transmit-side light refresh.

Run with pytest or directly (`python app/backblaze/tests/test_pq_refresh.py`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import pytest  # noqa: E402

from app.backblaze import bbdb, indexer, paths, pq, queries, rules  # noqa: E402

HEADER = "# GmtMillisThisListWasStarted 1234567890"


def _write_filelist(root: str, letter: str, seed: str) -> str:
    """A filelist over the 1000-byte stub threshold; a different seed produces a different body."""
    d = os.path.join(root, "bzfilelists")
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, f"v0005_{letter.lower()}____filelist.dat")
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(HEADER + "\n")
        fh.write(f"f\t{'0' * 16}\t1000\t{letter}:\\data\\file0.bin\n")
        if seed != "aaa":  # a different body for the rebuild step
            fh.write(f"f\t{'1' * 16}\t2000\t{letter}:\\data\\file1.bin\n")
        fh.write(("# " + seed * 100 + "\n") * 12)
    return fp


@pytest.fixture
def pq_env(tmp_path, monkeypatch):
    """Fake bzdata + a Parquet store under tmp + an in-memory bz DB on this thread."""
    root = str(tmp_path / "bzdata")
    os.makedirs(os.path.join(root, "bzfilelists"))
    monkeypatch.setattr(paths, "_bzdata_root", root)
    monkeypatch.setattr(rules, "load_engine", lambda: rules.RuleEngine([], [], []))
    monkeypatch.setattr(pq, "ROOT", str(tmp_path / "bz"))
    import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    bbdb._init_schema(con)
    bbdb._local.con = con
    queries._STORE_OVERRIDE = "parquet"
    yield con, root, str(tmp_path / "bz")
    queries._STORE_OVERRIDE = None
    bbdb._local.con = None


# ---- the parquet refresh path ---------------------------------------------------------

def test_parquet_refresh_bootstraps_and_rebuilds(pq_env):
    con, root, bzroot = pq_env
    _write_filelist(root, "C", "aaa")
    assert indexer.detect_changes()["changed"]  # empty store -> pending
    summary = indexer.refresh()
    assert summary["ok"] and summary["changed"] == ["C"]
    m = pq.read_manifest()
    assert m and set(m["drives"]) == {"C"}
    with pq.files_cursor() as cur:
        assert cur.execute("SELECT count(*) FROM files").fetchone()[0] == 1
    # the legacy sqlite `files` table stays untouched in parquet mode
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    assert bbdb.kv_get(con, "reindex_pending") == "0"
    # a rebuilt body -> generation 2 with the extra row; unchanged drives carried by reference
    _write_filelist(root, "C", "bbb")
    assert indexer.detect_changes()["changed"]
    assert indexer.refresh()["ok"]
    assert pq.read_manifest()["generation"] == 2
    with pq.files_cursor() as cur:
        assert cur.execute("SELECT count(*) FROM files").fetchone()[0] == 2


def test_parquet_store_switch_leaves_queue_and_kv_in_sqlite(pq_env):
    con, root, bzroot = pq_env
    dcenter = os.path.join(root, "bzbackup", "bzdatacenter")
    os.makedirs(dcenter, exist_ok=True)
    with open(os.path.join(dcenter, "bz_todo_20260913_0.dat"), "w", encoding="utf-8") as fh:
        fh.write("1\t+\t000000000034fcee\t0000019edfcf9d4b\t4096\tG:\\Playnite\\cef.log\n")
    _write_filelist(root, "C", "aaa")
    assert indexer.refresh(force=True)["ok"]
    # queue rows land in the slim SQLite even though `files` went to Parquet
    q = con.execute("SELECT drive, full_path, size FROM queue").fetchall()
    assert len(q) == 1 and q[0]["drive"] == "G" and q[0]["size"] == 4096
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 0


# ---- the worker progress file --------------------------------------------------------

def test_progress_file_matches_the_tray_and_reaper_glob(pq_env, tmp_path):
    import config
    assert os.path.basename(indexer._progress_path()) == "index_progress_repobz.json"
    assert "index_progress_repo" in os.path.basename(indexer._progress_path())
    indexer._write_progress("testing")
    p = os.path.join(config.DATA_DIR, "index_progress_repobz.json")
    assert os.path.exists(p)
    with open(p, encoding="utf-8") as fh:
        info = json.load(fh)
    assert info["repo"] == "bz" and info["stage"] == "testing" and info["pid"] == os.getpid()
    assert "updated" in info
    indexer._clear_progress()
    assert not os.path.exists(p)


def test_refresh_writes_and_clears_the_progress_file(pq_env, monkeypatch):
    con, root, bzroot = pq_env
    _write_filelist(root, "C", "aaa")
    stages: list[str] = []
    real = indexer._write_progress

    def capture(stage):
        stages.append(stage)
        real(stage)

    monkeypatch.setattr(indexer, "_write_progress", capture)
    indexer.refresh(force=True)
    assert "starting" in stages and "publishing" in stages and any(s.startswith("reindexing") for s in stages)
    import config
    assert not os.path.exists(os.path.join(config.DATA_DIR, "index_progress_repobz.json"))


# ---- the transmit-side light refresh --------------------------------------------------

@pytest.fixture
def transmit_env(tmp_path, monkeypatch):
    """Fake bzdata carrying ONLY transmit-side files (queue + report XMLs)."""
    root = str(tmp_path / "bzdata")
    os.makedirs(os.path.join(root, "bzbackup", "bzdatacenter"))
    os.makedirs(os.path.join(root, "bzreports"))
    os.makedirs(os.path.join(root, "bzfilelists"))
    monkeypatch.setattr(paths, "_bzdata_root", root)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    bbdb._init_schema(con)
    bbdb._local.con = con
    yield con, root
    bbdb._local.con = None


def _write_transmit_files(root: str) -> None:
    with open(os.path.join(root, "bzbackup", "bzdatacenter", "bz_todo_20260913_0.dat"), "w",
              encoding="utf-8") as fh:
        fh.write("1\t+\t000000000034fcee\t0000019edfcf9d4b\t4096\tG:\\Playnite\\cef.log\n"
                 "1\t+\t00000000003ddf4a\t0000019ee005e345\t0\tG:\\Playnite\\new.tmp\n")
    with open(os.path.join(root, "bzreports", "bzstat_remainingbackup.xml"), "w", encoding="utf-8") as fh:
        fh.write('<stat remainingnumfilesforbackup="606" remainingnumbytesforbackup="3270000000" />')
    with open(os.path.join(root, "bzreports", "bzstat_totalbackup.xml"), "w", encoding="utf-8") as fh:
        fh.write('<stat totnumfilesforbackup="1101650" totnumbytesforbackup="10611281402956" />')
    with open(os.path.join(root, "bzreports", "bzstat_lastbackupcompleted.xml"), "w", encoding="utf-8") as fh:
        fh.write('<stat localdatetime="1789000000" gmt_millis="1789000000000" />')


def test_transmit_refresh_writes_queue_and_kv(transmit_env):
    con, root = transmit_env
    _write_transmit_files(root)
    out = indexer.refresh_transmit_side()
    assert out["ok"] and out["queue_rows"] == 2
    assert con.execute("SELECT count(*) FROM queue").fetchone()[0] == 2
    remaining = json.loads(bbdb.kv_get(con, "remaining"))
    assert remaining["remaining_files"] == "606"
    total = json.loads(bbdb.kv_get(con, "totalbackup"))
    assert total["grand"] == [1101650, 10611281402956]
    # running it again is idempotent
    assert indexer.refresh_transmit_side()["ok"]


def test_transmit_refresh_skips_while_building(transmit_env):
    con, root = transmit_env
    _write_transmit_files(root)
    indexer._STATE["building"] = True
    try:
        out = indexer.refresh_transmit_side()
        assert not out["ok"] and out["reason"] == "refresh in flight"
        assert con.execute("SELECT count(*) FROM queue").fetchone()[0] == 0
    finally:
        indexer._STATE["building"] = False


def test_transmit_refresh_missing_bzdata_is_soft(transmit_env, monkeypatch):
    monkeypatch.setattr(paths, "_bzdata_root", str(transmit_env[1] + "_missing"))
    out = indexer.refresh_transmit_side()
    assert not out["ok"] and "bzdata not found" in out["reason"]


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} pq-refresh/transmit tests passed.")


if __name__ == "__main__":
    _run_all()
