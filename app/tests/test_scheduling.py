"""Nightly scheduling invariants.

The rule that matters: in daily mode a pass is due when the most recent scheduled time has passed
and this repo has not run since. That one comparison is also what gives catch-up for free - if the
machine was off at 23:00, the scheduled time is still in the past when it next boots.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import main as m


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


def test_nightly_state_seeds_from_the_legacy_timestamp(monkeypatch, tmp_path):
    """Upgrading must not look like 'never indexed' and fire an immediate catch-up on restart."""
    legacy = tmp_path / "bg_worker_state.json"
    legacy.write_text('{"ts": 1787254084.0}', encoding="utf-8")
    monkeypatch.setattr(m, "_LAST_CYCLE_FILE", str(legacy))
    assert m.nightly_ts("1") == pytest.approx(1787254084.0)


def test_nightly_state_prefers_the_per_repo_timestamp(monkeypatch, tmp_path):
    f = tmp_path / "bg_worker_state.json"
    f.write_text('{"ts": 100.0, "nightly": {"1": 500.0}}', encoding="utf-8")
    monkeypatch.setattr(m, "_LAST_CYCLE_FILE", str(f))
    assert m.nightly_ts("1") == 500.0
    assert m.nightly_ts("2") == 100.0  # no per-repo entry yet -> legacy value
