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


def test_the_retransmit_report_is_reported_only_with_wrong_size_lines(bz):
    con, root = bz
    report = os.path.join(root, "bzreports", checks.RETRANSMIT_REPORT)
    assert checks.notices(con) == []
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("header\n")
    assert checks.notices(con) == []
    with open(report, "a", encoding="utf-8") as fh:
        fh.write("20261101 bz_done_20261101_0.dat wrong_size 1 2\n20261105 bz_done_20261105_0.dat wrong_size 3 4\n")
    notes = checks.notices(con)
    assert len(notes) == 1 and "2 times" in notes[0]["text"]
