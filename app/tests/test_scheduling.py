"""Nightly scheduling invariants.

The rule that matters: in daily mode a pass is due when the most recent scheduled time has passed
and this repo has not run since. That one comparison is also what gives catch-up for free - if the
machine was off at 23:00, the scheduled time is still in the past when it next boots.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from app import main as m


@pytest.fixture(autouse=True)
def _no_repo_record(monkeypatch):
    """A repo's creation time counts as its last run; these tests date runs before any real record."""
    monkeypatch.setattr(m, "_repo_created", lambda repo: 0.0)


@pytest.fixture
def daily(monkeypatch):
    """Daily mode at 23:00, with a settable 'last run' timestamp."""
    monkeypatch.setattr(m, "_index_mode", lambda: "daily")
    monkeypatch.setattr(m, "_index_time", lambda: (23, 0))
    state = {"last": 0.0}
    monkeypatch.setattr(m, "nightly_ts", lambda repo: state["last"])
    return state


def _at(y, mo, d, h, mi=0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi)


def _due(now: dt.datetime) -> bool:
    return m._index_due("1", now, now.timestamp())


def test_due_after_the_scheduled_time_when_never_run(daily):
    assert _due(_at(2026, 8, 21, 23, 5)) is True


def test_not_due_again_the_same_night(daily):
    daily["last"] = _at(2026, 8, 21, 23, 1).timestamp()
    assert _due(_at(2026, 8, 21, 23, 30)) is False
    assert _due(_at(2026, 8, 22, 9, 0)) is False   # still before the next 23:00


def test_due_again_the_next_night(daily):
    daily["last"] = _at(2026, 8, 21, 23, 1).timestamp()
    assert _due(_at(2026, 8, 22, 23, 1)) is True


def test_catch_up_after_the_pc_was_off_overnight(daily):
    """Last ran two days ago, booting at 09:00 - 23:00 yesterday was missed, so run now."""
    daily["last"] = _at(2026, 8, 19, 23, 0).timestamp()
    assert _due(_at(2026, 8, 21, 9, 0)) is True


def test_not_due_before_the_first_scheduled_time_of_the_day(daily):
    """Ran at 23:00 last night; at 09:00 the most recent due time is still last night's."""
    daily["last"] = _at(2026, 8, 20, 23, 5).timestamp()
    assert _due(_at(2026, 8, 21, 9, 0)) is False


def test_midnight_schedule_does_not_double_fire(daily, monkeypatch):
    monkeypatch.setattr(m, "_index_time", lambda: (0, 0))
    daily["last"] = _at(2026, 8, 21, 0, 1).timestamp()
    assert _due(_at(2026, 8, 21, 0, 30)) is False
    assert _due(_at(2026, 8, 21, 23, 59)) is False
    assert _due(_at(2026, 8, 22, 0, 1)) is True


def test_interval_mode_uses_elapsed_time(monkeypatch):
    monkeypatch.setattr(m, "_index_mode", lambda: "interval")
    monkeypatch.setattr(m, "_index_interval", lambda: 3600.0)
    now = _at(2026, 8, 21, 12, 0)
    monkeypatch.setattr(m, "nightly_ts", lambda repo: (now - dt.timedelta(minutes=30)).timestamp())
    assert _due(now) is False
    monkeypatch.setattr(m, "nightly_ts", lambda repo: (now - dt.timedelta(minutes=90)).timestamp())
    assert _due(now) is True


def test_interval_mode_retries_a_failed_pass_under_its_own_slot(monkeypatch):
    monkeypatch.setattr(m, "_index_mode", lambda: "interval")
    monkeypatch.setattr(m, "_index_interval", lambda: 3600.0)
    monkeypatch.setattr(m, "_retry_delay", lambda: 600.0)
    monkeypatch.setattr(m, "_max_retries", lambda: 2)
    now = _at(2026, 8, 21, 12, 0)
    started = (now - dt.timedelta(minutes=20)).timestamp()
    monkeypatch.setattr(m, "nightly_ts", lambda repo: started)
    rec = {"slot": started, "at": started, "finished": started + 60, "retries": 0, "ok": False}
    monkeypatch.setattr(m, "_attempt_record", lambda repo: rec)
    assert m._due_slot("1", now, now.timestamp()) == started
    rec["ok"] = True
    assert m._due_slot("1", now, now.timestamp()) is None


@pytest.mark.parametrize("raw,expected", [
    ("23:00", (23, 0)),
    ("07:05", (7, 5)),
    ("9", (9, 0)),          # no minutes given
    ("nonsense", (23, 0)),  # unparseable -> the default, never an exception
    ("99:99", (23, 59)),    # out of range -> clamped
])
def test_index_time_parsing_is_forgiving(monkeypatch, raw, expected):
    """A junk or out-of-range setting must fall back, never crash the scheduler thread."""
    monkeypatch.setattr(m, "_kl", lambda: {"index_time": raw})
    assert m._index_time() == expected


def test_nightly_ts_has_no_legacy_fallback(monkeypatch, tmp_path):
    """The shared `ts` field must NOT seed a repo's due-check (2026-08-22 fix): it used to make
    repo1 starting first each night falsely mark repo2/bz as 'already attempted', starving them
    permanently. A repo with no attempt of its own reads as 0 ('never'), which is already the
    correct 'due' answer - no fallback needed."""
    legacy = tmp_path / "bg_worker_state.json"
    legacy.write_text('{"ts": 1787254084.0}', encoding="utf-8")
    monkeypatch.setattr(m, "_LAST_CYCLE_FILE", str(legacy))
    assert m.nightly_ts("1") == 0.0


def test_nightly_state_prefers_the_per_repo_timestamp(monkeypatch, tmp_path):
    f = tmp_path / "bg_worker_state.json"
    f.write_text('{"ts": 100.0, "nightly": {"1": 500.0}}', encoding="utf-8")
    monkeypatch.setattr(m, "_LAST_CYCLE_FILE", str(f))
    assert m.nightly_ts("1") == 500.0
    assert m.nightly_ts("2") == 0.0  # no per-repo entry -> never, NOT the shared ts


@pytest.mark.parametrize("mode", ["off", "interval"])
def test_first_background_pass_waits_only_the_startup_delay(monkeypatch, tmp_path, mode):
    """A restart soon after the last cycle once held all background work for the rest of the
    24-hour index interval (Backblaze queue and stats included)."""
    recent = time.time() - 60
    f = tmp_path / "bg_worker_state.json"
    f.write_text(f'{{"ts": {recent}, "nightly": {{"1": {recent}}}}}', encoding="utf-8")
    monkeypatch.setattr(m, "_LAST_CYCLE_FILE", str(f))
    monkeypatch.setattr(m, "_index_mode", lambda: mode)
    monkeypatch.setattr(m, "_index_interval", lambda: 86400.0)
    monkeypatch.setattr(m, "_startup_delay", lambda: 300.0)
    monkeypatch.setattr(m, "_bz_reconcile", lambda: None, raising=False)
    waits = []
    monkeypatch.setattr(m._bg_stop, "wait", lambda t: waits.append(t) or True)   # True = stop
    m._bg_worker()
    assert waits == [300.0]


# ----: BZ's own detect-driven due check --------------------------------------------

def _bz_due(monkeypatch, *, pending, anchor, gap=21600.0, last_attempt=0.0, now=1_000_000.0):
    kv = {"reindex_pending": 1.0 if pending else 0.0, "last_reindex_ts": anchor}
    monkeypatch.setattr(m, "_bz_present", lambda: True)
    monkeypatch.setattr(m, "_bz_kv_float", lambda key: kv[key])
    monkeypatch.setattr(m, "_bz_min_gap", lambda: gap)
    monkeypatch.setattr(m, "_retry_delay", lambda: 900.0)
    monkeypatch.setattr(m, "_BZ_LAST_ATTEMPT", last_attempt)
    return m._bz_reindex_due(now)


def test_bz_not_due_without_a_detected_change(monkeypatch):
    assert _bz_due(monkeypatch, pending=False, anchor=0.0) is False


def test_bz_not_due_while_the_min_gap_is_open(monkeypatch):
    assert _bz_due(monkeypatch, pending=True, anchor=1_000_000.0 - 3600) is False


def test_bz_due_when_the_gap_has_opened(monkeypatch):
    assert _bz_due(monkeypatch, pending=True, anchor=1_000_000.0 - 30000) is True


def test_bz_waits_out_a_recent_failed_attempt(monkeypatch):
    assert _bz_due(monkeypatch, pending=True, anchor=0.0, last_attempt=1_000_000.0 - 60) is False


def test_bz_retries_after_the_retry_window(monkeypatch):
    assert _bz_due(monkeypatch, pending=True, anchor=0.0, last_attempt=1_000_000.0 - 1000) is True


def test_bz_min_gap_never_collapses_to_zero(monkeypatch):
    """A junk setting must fall back to the default, and the floor (300s) always holds."""
    monkeypatch.setattr(m, "_DEFAULT_BZ_REINDEX_GAP", 21600)
    monkeypatch.setattr(m, "load_settings", lambda: {"backblaze": {"bz_reindex_min_gap": "nonsense"}})
    assert m._bz_min_gap() == 21600.0
    monkeypatch.setattr(m, "load_settings", lambda: {"backblaze": {"bz_reindex_min_gap": 5}})
    assert m._bz_min_gap() == 300.0


def _tick_with(monkeypatch, *, bz_due):
    """Run one _tick with everything idle; record what the scheduler tried to start."""
    started = []
    monkeypatch.setattr(m.nightly, "any_running", lambda: False)
    monkeypatch.setattr(m.jobrunner, "maintenance_running", lambda: False)
    monkeypatch.setattr(m, "_due_slot", lambda repo, nd, ts: None)
    monkeypatch.setattr(m, "_bz_reindex_due", lambda ts: bz_due)
    monkeypatch.setattr(m.nightly, "start_index_pass_async",
                        lambda repo, on_done=None, full=False: started.append(repo) or {"started": True})
    monkeypatch.setattr(m, "_last_light", time.time())
    m._tick()
    return started


def test_tick_never_gives_bz_the_daily_slot(monkeypatch):
    seen = []
    monkeypatch.setattr(m.nightly, "any_running", lambda: False)
    monkeypatch.setattr(m.jobrunner, "maintenance_running", lambda: False)
    monkeypatch.setattr(m, "_due_slot", lambda repo, nd, ts: seen.append(repo) or None)
    monkeypatch.setattr(m, "_bz_reindex_due", lambda ts: False)
    monkeypatch.setattr(m, "_last_light", time.time())
    m._tick()
    assert "bz" not in seen  # BZ left the daily slot () - detect drives it instead


def test_tick_starts_the_bz_reindex_through_the_global_slot(monkeypatch):
    assert _tick_with(monkeypatch, bz_due=True) == ["bz"]


def test_tick_skips_bz_when_not_due(monkeypatch):
    assert _tick_with(monkeypatch, bz_due=False) == []


def test_a_pause_holds_every_scheduled_run(monkeypatch):
    from app.core import pause
    pause.pause(0)
    try:
        assert _tick_with(monkeypatch, bz_due=True) == []
    finally:
        pause.resume()
    assert _tick_with(monkeypatch, bz_due=True) == ["bz"]   # due work starts once it ends
