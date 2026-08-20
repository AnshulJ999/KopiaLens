"""Regression tests for the issues the commit-1 review turned up.

Each test names the failure it prevents, because several of these were real and would have hit on
the first live night.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest

from app import main as m
from app.api.insights import classify_folder_sources
from app.index.churnview import _slice

SEP = chr(92)


# ---- the blocker: worker launched from the wrong directory ------------------------------------
def test_worker_cwd_is_the_kopialens_root_not_the_app_package():
    """`python -m app.index.worker` only resolves from the KopiaLens root. Deriving the root by
    counting os.path.dirname calls got this one level short, so every nightly run would have died
    with 'No module named app'."""
    from app.core import nightly
    assert os.path.isdir(os.path.join(nightly._KL_ROOT, "app")), nightly._KL_ROOT
    assert os.path.isfile(os.path.join(nightly._KL_ROOT, "config.py")), nightly._KL_ROOT


# ---- stage order: reclaimable must not be priced off a stale refcount --------------------------
def test_worker_rebuilds_the_dashboard_before_analysis():
    """Reclaimable reads content_refs; the dashboard stage is what rebuilds it. Analysis first
    would price tonight's delete-safety numbers against yesterday's refcount."""
    import inspect
    from app.index import worker
    src = inspect.getsource(worker.run)
    body = src[src.index('if stage("index"'):]
    assert body.index('"dashboard"') < body.index('"analysis"'), "analysis must not precede dashboard"


def test_worker_skips_dependent_stages_when_the_index_fails():
    import inspect
    from app.index import worker
    src = inspect.getsource(worker.run)
    assert "skipping every stage that reads the index" in src


# ---- retry policy -----------------------------------------------------------------------------
@pytest.fixture
def sched(monkeypatch, tmp_path):
    f = tmp_path / "bg_worker_state.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(m, "_LAST_CYCLE_FILE", str(f))
    monkeypatch.setattr(m, "_index_mode", lambda: "daily")
    monkeypatch.setattr(m, "_index_time", lambda: (23, 0))
    monkeypatch.setattr(m, "_retry_delay", lambda: 900.0)
    monkeypatch.setattr(m, "_max_retries", lambda: 1)
    return f


def _write(f, data):
    f.write_text(json.dumps(data), encoding="utf-8")


def _at(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi)


def _due(now):
    return m._index_due("1", now, now.timestamp())


def test_failed_run_is_retried_once_after_the_delay(sched):
    now = _at(2026, 8, 21, 23, 40)
    slot = m._current_slot(now, now.timestamp())
    _write(sched, {"nightly": {"1": slot + 60},
                   "attempts": {"1": {"slot": slot, "at": slot + 60, "finished": slot + 120,
                                      "retries": 0, "ok": False}}})
    just_after = dt.datetime.fromtimestamp(slot + 300)
    assert _due(just_after) is False        # inside the retry delay
    later = dt.datetime.fromtimestamp(slot + 1200)
    assert _due(later) is True              # delay elapsed -> one retry


def test_retry_happens_only_once(sched):
    now = _at(2026, 8, 21, 23, 40)
    slot = m._current_slot(now, now.timestamp())
    _write(sched, {"nightly": {"1": slot + 60},
                   "attempts": {"1": {"slot": slot, "at": slot + 60, "finished": slot + 120,
                                      "retries": 1, "ok": False}}})
    assert _due(dt.datetime.fromtimestamp(slot + 5000)) is False


def test_successful_run_is_never_retried(sched):
    now = _at(2026, 8, 21, 23, 40)
    slot = m._current_slot(now, now.timestamp())
    _write(sched, {"nightly": {"1": slot + 60},
                   "attempts": {"1": {"slot": slot, "at": slot + 60, "finished": slot + 120,
                                      "retries": 0, "ok": True}}})
    assert _due(dt.datetime.fromtimestamp(slot + 5000)) is False


def test_a_run_still_in_flight_is_not_restarted(sched):
    """ok is None while the pass is running - that must not read as a failure to retry."""
    now = _at(2026, 8, 21, 23, 40)
    slot = m._current_slot(now, now.timestamp())
    _write(sched, {"nightly": {"1": slot + 60},
                   "attempts": {"1": {"slot": slot, "at": slot + 60, "retries": 0, "ok": None}}})
    assert _due(dt.datetime.fromtimestamp(slot + 5000)) is False


def test_freshness_reports_success_not_attempt(sched):
    """A crashed run must never make the UI say 'indexed just now'."""
    _write(sched, {"nightly": {"1": 5000.0}, "success": {"1": 1000.0}})
    assert m.nightly_ts("1") == 5000.0
    assert m.nightly_success_ts("1") == 1000.0


def test_mark_result_advances_success_only_on_success(sched):
    _write(sched, {"nightly": {"1": 100.0}, "success": {"1": 100.0},
                   "attempts": {"1": {"slot": 0, "at": 100.0, "retries": 0, "ok": None}}})
    m.mark_nightly_result("1", False)
    assert m.nightly_success_ts("1") == 100.0        # unchanged after a failure
    m.mark_nightly_result("1", True)
    assert m.nightly_success_ts("1") > 100.0


# ---- churn cache must not let one request's limit constrain another ---------------------------
def test_slice_does_not_mutate_the_cached_payload():
    payload = {"by_source": [{"bytes": i} for i in range(50)],
               "by_folder": [{"bytes": i} for i in range(50)],
               "by_ext": [{"bytes": i} for i in range(50)], "total_bytes": 1}
    small = _slice(payload, 10)
    assert len(small["by_source"]) == 10
    assert len(payload["by_source"]) == 50, "the cached rollup was truncated in place"
    assert len(_slice(payload, 400)["by_source"]) == 50


# ---- folder source classification -------------------------------------------------------------
def test_sub_policies_are_not_counted_as_covering_the_folder():
    """'AppData is covered by 24 sources' was wrong: one source covers it, 23 sit inside it."""
    prefix = "C:" + SEP + "Users" + SEP + "A" + SEP + "AppData"
    all_src = ["u@h:C:" + SEP,
               "u@h:" + prefix + SEP + "Local" + SEP + "Google",
               "u@h:" + prefix + SEP + "Roaming"]
    rows = [{"source": s} for s in all_src]
    classify_folder_sources(prefix, rows, all_src)
    assert [r["relation"] for r in rows] == ["covers", "inside", "inside"]
    assert sum(1 for r in rows if r["relation"] == "covers") == 1


def test_overlap_counts_nested_parents():
    prefix = "C:" + SEP + "Users"
    deep = "u@h:C:" + SEP + "Users" + SEP + "A" + SEP + "AppData" + SEP + "Local" + SEP + "Google"
    all_src = ["u@h:C:" + SEP, "u@h:C:" + SEP + "Users" + SEP + "A" + SEP + "AppData", deep]
    rows = [{"source": deep}]
    classify_folder_sources(prefix, rows, all_src)
    assert rows[0]["overlap"] == 3      # itself + AppData + C:
    assert rows[0]["relation"] == "inside"


def test_a_folder_equal_to_its_source_root_counts_as_covered():
    prefix = "G:" + SEP + "Apps"
    all_src = ["u@h:G:" + SEP + "Apps"]
    rows = [{"source": all_src[0]}]
    classify_folder_sources(prefix, rows, all_src)
    assert rows[0]["relation"] == "covers"


def test_prefix_lookalikes_are_not_treated_as_parents():
    """C:\\Apps must not be read as the parent of C:\\AppsData."""
    prefix = "C:" + SEP + "AppsData"
    all_src = ["u@h:C:" + SEP + "Apps", "u@h:C:" + SEP + "AppsData"]
    rows = [{"source": "u@h:C:" + SEP + "AppsData"}]
    classify_folder_sources(prefix, rows, all_src)
    assert rows[0]["overlap"] == 1
