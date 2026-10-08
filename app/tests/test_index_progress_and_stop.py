""" the whole-run estimate + stop means stop."""

from __future__ import annotations

import datetime as dt
import time

FAKE_RUNS = [
    {"ok": True, "full": False, "stages": {"index": 300.0, "dashboard": 60.0, "analysis": 40.0}},
    {"ok": True, "full": False, "stages": {"index": 200.0, "dashboard": 60.0, "analysis": 40.0}},
    {"ok": True, "full": True, "stages": {"index": 4000.0}},        # a different kind: ignored
]


def _weights_from(monkeypatch, runs=FAKE_RUNS):
    from app.core import nightly, runhistory

    monkeypatch.setattr(runhistory, "read_tail", lambda limit=0, repos=None: list(runs))


def test_run_progress_is_one_overall_number_with_an_eta(monkeypatch):
    from app.core import nightly

    _weights_from(monkeypatch)
    out = nightly.run_progress("3", {"stage": "dashboard", "full": False,
                                     "stage_started": time.time() - 30})
    assert out["estimated"] is True
    assert [s["state"] for s in out["stages"]][:3] == ["done", "current", "pending"]
    assert 0 < out["pct"] < 100 and out["eta_seconds"] is not None
    # index (median 300) done + half of dashboard (60) over the total of the known stages
    assert out["pct"] == int((300 + 30) / (300 + 60 + 40) * 100) == 82


def test_run_progress_prefers_a_real_fraction_over_elapsed_time(monkeypatch):
    from app.core import nightly

    _weights_from(monkeypatch)
    out = nightly.run_progress("3", {"stage": "index", "full": False, "pct": 50,
                                     "stage_started": time.time()})
    assert out["pct"] == 37          # half of the index stage (150 of 400 seconds)
    assert out["stages"][0]["state"] == "current"


def test_run_progress_without_history_claims_nothing(monkeypatch):
    from app.core import nightly

    _weights_from(monkeypatch, runs=[])
    out = nightly.run_progress("3", {"stage": "index"})
    assert out["estimated"] is False and out["pct"] is None and out["eta_seconds"] is None


def test_worker_log_tail_reads_the_last_lines(monkeypatch, tmp_path):
    from app.core import nightly

    log = tmp_path / "worker.log"
    log.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
    monkeypatch.setattr(nightly, "worker_log_path", lambda repo: str(log))
    assert nightly.worker_log_tail("3", limit=3) == ["line 7", "line 8", "line 9"]
    monkeypatch.setattr(nightly, "worker_log_path", lambda repo: str(tmp_path / "nope.log"))
    assert nightly.worker_log_tail("3") == []


# ---- KL103 ---------------------------------------------------------------------------------------

def _due(monkeypatch, record):
    from app import main

    now_ts = time.time()
    now_dt = dt.datetime.now()
    slot = main._current_slot(now_dt)
    state = {"attempts": {"3": {"slot": slot, "finished": now_ts - 10_000, **record}},
             "nightly": {"3": slot}}
    monkeypatch.setattr(main, "_read_state", lambda: state)
    return main._index_due("3", now_dt, now_ts)


def test_a_stopped_run_stays_stopped(monkeypatch):
    assert _due(monkeypatch, {"ok": False, "retries": 0, "stopped": True}) is False


def test_a_genuine_failure_still_retries(monkeypatch):
    assert _due(monkeypatch, {"ok": False, "retries": 0}) is True


def test_a_finished_run_is_not_due(monkeypatch):
    assert _due(monkeypatch, {"ok": True, "retries": 0}) is False


def test_worker_log_lines_pages_with_a_cursor(monkeypatch, tmp_path):
    from app.core import nightly

    log = tmp_path / "w.log"
    log.write_text(chr(10).join(f"l{i}" for i in range(10)), encoding="utf-8")
    monkeypatch.setattr(nightly, "worker_log_path", lambda repo: str(log))
    lines, cursor = nightly.worker_log_lines("3", after=0, limit=4)
    assert lines == ["l0", "l1", "l2", "l3"] and cursor == 4
    lines, cursor = nightly.worker_log_lines("3", after=cursor, limit=4)
    assert lines == ["l4", "l5", "l6", "l7"] and cursor == 8
    log.write_text("fresh", encoding="utf-8")              # rolled: the cursor no longer exists
    lines, cursor = nightly.worker_log_lines("3", after=99)
    assert lines == ["fresh"] and cursor == 1


def test_worker_log_lines_first_call_starts_at_the_latest_run(monkeypatch, tmp_path):
    """The file holds many nights; the panel's first call must open on the current run, not page
    through old ones."""
    from app.core import nightly

    log = tmp_path / "w.log"
    log.write_text(chr(10).join(["===== run started 2026-09-21 23:00:00 repo=2 full=False =====",
                                 "old a", "old b",
                                 "===== run started 2026-09-23 03:06:36 repo=2 full=False =====",
                                 "new a", "new b"]), encoding="utf-8")
    monkeypatch.setattr(nightly, "worker_log_path", lambda repo: str(log))
    lines, cursor = nightly.worker_log_lines("2", after=0)
    assert lines[0].startswith("===== run started 2026-09-23") and lines[1:] == ["new a", "new b"]
    assert cursor == 6


def test_queued_repo_reads_the_scheduler_state(monkeypatch, tmp_path):
    import json

    from app.core import nightly
    from config import REPOS

    (tmp_path / "bg_worker_state.json").write_text(json.dumps(
        {"attempts": {"1": {"slot": 100}}, "nightly": {"1": 100, "2": 50}}), encoding="utf-8")
    monkeypatch.setattr(nightly, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr("config.REPOS", {"1": {}, "2": {}})   # REPOS is a dict of ids
    assert nightly.queued_repo("1") == "2"       # repo 2 has not run in this slot
    assert nightly.queued_repo("2") is None      # nothing after the last repo
    monkeypatch.setattr("config.REPOS", REPOS)
