"""Backblaze Health checks read from Backblaze's own files. A rolled-back upload record keeps the
Uploaded history and is reported only when a meaningful amount disappears (routine trims stay quiet);
the retransmit report is reported only when it holds wrong_size lines."""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import pytest  # noqa: E402

from app.backblaze import bbdb, checks, indexer, paths  # noqa: E402

GB = 1_000_000_000


@pytest.fixture
def bz(tmp_path, monkeypatch):
    root = str(tmp_path / "bzdata")
    for d in ("bzbackup/bzdatacenter", "bzreports"):
        os.makedirs(os.path.join(root, d))
    monkeypatch.setattr(paths, "_bzdata_root", root)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    bbdb._init_schema(con)
    bbdb._local.con = con
    yield con, root
    bbdb._local.con = None


def _bz_done(root: str, rows: list[tuple[str, int]], age: float) -> None:
    path = os.path.join(root, "bzbackup", "bzdatacenter", "bz_done_20261101_0.dat")
    with open(path, "w", encoding="utf-8") as fh:
        for i, (stamp, size) in enumerate(rows):
            fh.write(f"5\tx\tx\t{stamp}\tx\tx\t{size:x}\tx\tC:\\data\\f{i}.bin\n")
    t = time.time() - age
    os.utime(path, (t, t))


def _days(con) -> dict:
    return {d: b for d, b in con.execute("SELECT day, bytes FROM backup_days")}


def test_a_rolled_back_upload_record_keeps_history_and_only_a_big_loss_is_reported(bz):
    con, root = bz
    _bz_done(root, [("20261101120000", 2 * GB), ("20261102120000", GB), ("20261102130000", 200_000_000)], 100)
    indexer._refresh_backup_days(con)
    before = _days(con)

    _bz_done(root, [("20261101120000", 2 * GB), ("20261102120000", GB)], 50)     # a routine trim: 0.2 GB
    indexer._refresh_backup_days(con)
    assert _days(con) == before
    assert checks.notices(con) == []

    _bz_done(root, [("20261101120000", 2 * GB)], 10)                              # a rollback: 1 GB more
    indexer._refresh_backup_days(con)
    assert _days(con) == before
    notes = checks.notices(con)
    assert len(notes) == 1 and notes[0]["level"] == "info" and "1.0 GB" in notes[0]["text"]


def test_backup_runs_survive_backblaze_locking_yesterdays_log(bz):
    # Backblaze zips each finished day's event log into a file KopiaLens can't open, so only today's
    # log is readable: a run must be kept once seen.
    from app.backblaze import queries
    con, root = bz
    logs = os.path.join(root, "bzlogs", "bzreports_eventlog")
    os.makedirs(logs)
    with open(os.path.join(logs, "17.log"), "w", encoding="utf-8") as fh:
        fh.write("2026-11-17 06:02:06 - STARTBACKUP\n2026-11-17 06:34:23 - END_BACKUP: Backed up 4706 FILES / 3761 MB\n")
    indexer._refresh_events(con)
    os.remove(os.path.join(logs, "17.log"))
    with open(os.path.join(logs, "18.log"), "w", encoding="utf-8") as fh:
        fh.write("2026-11-18 06:00:00 - STARTBACKUP\n")
    indexer._refresh_events(con)
    runs = queries.sessions()["runs"]
    assert runs == [{"started": "2026-11-17 06:02:06", "ended": "2026-11-17 06:34:23",
                     "files": 4706, "bytes": 3_761_000_000}]


def _utc(days_ago: float) -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(time.time() - days_ago * 86400))


def test_the_retransmit_report_counts_recent_wrong_size_lines(bz):
    # Line shape from a real Mac report (r/backblaze, 2026-11-18): UTC time, record file, reason.
    con, root = bz
    report = os.path.join(root, "bzreports", checks.RETRANSMIT_REPORT)
    assert checks.notices(con) == []
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(f"{_utc(90)} bz_done_20260801_1.bzff wrong_size_bz_comb_86677408\n")
    assert checks.notices(con) == []                                   # too old for Health
    with open(report, "a", encoding="utf-8") as fh:
        fh.write(f"{_utc(7)} bz_done_20261109_1.bzff wrong_size_bz_comb_86683235\n"
                 f"{_utc(2)} bz_done_20261111_1.bzff wrong_size_bz_comb_88103479\n")
    notes = checks.notices(con)
    last = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
    assert len(notes) == 1 and "2 times" in notes[0]["text"] and last in notes[0]["text"]


def test_purged_lines_count_files_and_skip_backblazes_padding(bz):
    # A purge comments a line out and tags its path; Backblaze's own padding lines also start with #.
    con, root = bz
    path = os.path.join(root, "bzbackup", "bzdatacenter", "bz_done_20261101_0.dat")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#\t+\tb--\t20261101101112\tr_h9\tu--\t0123456789abcdef\tk-_n-----\tg8\n")
        fh.write("#\t+\tx\t20261101120000\tx\tx\t10\tx\tC:\\data\\big.mkv_purge_jkGf\n")
        fh.write("#\t+\tx\t20261101120000\tx\tx\t10\tx\tC:\\data\\big.mkv_purge_jkGg\n")
        fh.write("5\t+\tx\t20261101120000\tx\tx\t10\tx\tC:\\data\\kept.txt\n")
    indexer._refresh_backup_days(con)
    notes = checks.notices(con)
    assert len(notes) == 1 and "upload 1 file again" in notes[0]["text"]
