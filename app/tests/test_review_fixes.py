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


def test_freshness_is_never_when_no_success_is_recorded(sched):
    """The strip must not borrow a timestamp from anywhere else.

    It used to fall through to the attempt time and then to the global cycle `ts`, so three repos
    all displayed the same "8m ago" - the age of the background cycle, not of any index. The
    existing tests never caught it because they always populated `success`."""
    _write(sched, {"ts": 9999.0, "nightly": {}})
    assert m.nightly_success_ts("1") == 0.0
    _write(sched, {"ts": 9999.0, "nightly": {"1": 5000.0}})
    assert m.nightly_success_ts("1") == 0.0, "an ATTEMPT is not a success"


def test_a_repo_that_only_ever_fails_never_looks_fresh(sched):
    """The case that actually matters. `ts` is rewritten on every app start, so with the old
    fallback a repo whose index had been failing for a week read as minutes old, and got YOUNGER
    each time the app restarted."""
    _write(sched, {"ts": 1000.0, "nightly": {"1": 1000.0},
                   "attempts": {"1": {"slot": 0, "at": 1000.0, "retries": 1, "ok": False}}})
    assert m.nightly_success_ts("1") == 0.0
    _write(sched, {"ts": 8000.0, "nightly": {"1": 1000.0},          # ...app restarted, ts moved
                   "attempts": {"1": {"slot": 0, "at": 1000.0, "retries": 1, "ok": False}}})
    assert m.nightly_success_ts("1") == 0.0, "a restart must not refresh the displayed age"


def test_the_due_check_has_no_seeding_fallback(sched):
    """`nightly_ts` (2026-08-22 fix) ignores the shared `ts` entirely - a repo with no attempt of
    its own reads as 0 ('never'/due), never the shared cycle timestamp. The old fallback let
    repo1 starting first each night falsely mark repo2/bz as already attempted, starving them
    permanently - not just a display quirk, a real scheduling bug."""
    _write(sched, {"ts": 5000.0, "nightly": {}})
    assert m.nightly_ts("1") == 0.0


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


# ---- one heavy pass at a time, across every repo ----------------------------------------------
def test_only_one_heavy_pass_can_run_even_under_a_race(monkeypatch):
    """The gate is a single atomic reservation. Two mechanisms enforcing this (a reserved-repo
    marker AND a separate lock) was one too many; this proves the remaining one is sufficient."""
    import threading
    import time as _t
    from app.core import nightly

    monkeypatch.setattr(nightly, "_run_kopia", lambda repo, full: (_t.sleep(0.2), {"ok": True})[1])
    ran = []

    def go(repo):
        r = nightly.run_index_pass(repo)
        if not r.get("skipped"):
            ran.append(repo)

    threads = [threading.Thread(target=go, args=("1" if i % 2 else "2",)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ran) == 1, f"expected exactly one pass, got {ran}"
    assert nightly.reserved_repo() is None, "the slot was not released"


def test_a_second_repo_cannot_start_while_one_is_indexing(monkeypatch):
    """The manual Index button used to check only its own repo, so clicking it on repo 2 during
    repo 1's nightly started a second worker."""
    from app.core import nightly

    assert nightly._reserve("1") is True
    try:
        assert nightly.start_index_pass_async("2")["started"] is False
        assert nightly.run_index_pass("2").get("skipped") is True
    finally:
        nightly._release("1")


# ---- the offline handover itself -------------------------------------------------------------
# Nothing exercised exclusive_offline, which is how an UnboundLocalError on its FAST path (drain
# completes immediately - the normal case) survived py_compile and a green suite.
def test_exclusive_offline_fast_path_hands_over_and_reopens(tmp_path, monkeypatch):
    import duckdb
    from app.index import schema

    dbfile = tmp_path / "t.duckdb"
    con = duckdb.connect(str(dbfile))
    con.execute("CREATE TABLE t(x INTEGER)")
    monkeypatch.setitem(schema._POOL, "1", con)
    monkeypatch.setattr(schema, "get_db", lambda repo: schema._POOL.get(repo))
    monkeypatch.setattr(schema, "_drain_timeout_default", lambda: 5.0)

    class _NoLock:
        def acquire(self, *a, **k):
            return True

        def release(self):
            pass

    monkeypatch.setattr("app.core.metrics._build_lock", lambda repo, mode: _NoLock())

    inside = {}
    with schema.exclusive_offline("1", reason="indexing"):
        inside["offline"] = schema.offline_status("1") is not None
        inside["pool_empty"] = "1" not in schema._POOL
    assert inside["offline"] is True, "repo was not marked offline"
    assert inside["pool_empty"] is True, "the pooled connection was not released to the worker"
    assert schema.offline_status("1") is None, "repo stayed offline after the block"


def test_exclusive_offline_reports_what_is_blocking_on_timeout(monkeypatch):
    from app.index import schema

    class _NoLock:
        def acquire(self, *a, **k):
            return True

        def release(self):
            pass

    monkeypatch.setattr("app.core.metrics._build_lock", lambda repo, mode: _NoLock())
    monkeypatch.setitem(schema._active_reads, "9", 3)   # pretend three queries are stuck
    try:
        with pytest.raises(TimeoutError) as err:
            with schema.exclusive_offline("9", drain_timeout=0.2):
                pass
        assert "3 operation(s) still in flight" in str(err.value)
        assert schema.offline_status("9") is None, "a failed drain must not leave the repo offline"
    finally:
        schema._active_reads.pop("9", None)


# ---- orphaned worker cleanup ------------------------------------------------------------------
# A worker can outlive the app (tray quit runs no graceful shutdown). Observed live: it kept the
# repo's DuckDB exclusively and the next app start returned 500 for every request to that repo.
def _progress(tmp_path, repo, pid):
    f = tmp_path / f"index_progress_repo{repo}.json"
    f.write_text(json.dumps({"repo": repo, "pid": pid, "stage": "folders"}), encoding="utf-8")
    return f


def test_reaper_kills_a_real_orphan_and_clears_its_file(tmp_path, monkeypatch):
    from app.core import nightly

    monkeypatch.setattr(nightly, "DATA_DIR", str(tmp_path))
    f = _progress(tmp_path, "1", 4242)
    killed = []

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return ["pythonw.exe", "-m", "app.index.worker", "1"]

        def terminate(self):
            killed.append(self.pid)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("psutil.Process", _Proc)
    assert nightly.reap_orphan_workers() == ["1"]
    assert killed == [4242]
    assert not f.exists(), "the stale progress file must be removed"


def test_reaper_never_kills_an_unrelated_process(tmp_path, monkeypatch):
    """PIDs get recycled. Killing whatever now holds that number would be catastrophic, so the
    command line must be confirmed first."""
    from app.core import nightly

    monkeypatch.setattr(nightly, "DATA_DIR", str(tmp_path))
    _progress(tmp_path, "2", 4243)
    killed = []

    class _Innocent:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return ["chrome.exe", "--type=renderer"]

        def terminate(self):
            killed.append(self.pid)

    monkeypatch.setattr("psutil.Process", _Innocent)
    assert nightly.reap_orphan_workers() == []
    assert killed == [], "reaper killed a process that was not one of our workers"


def test_reaper_handles_an_already_dead_pid(tmp_path, monkeypatch):
    import psutil
    from app.core import nightly

    monkeypatch.setattr(nightly, "DATA_DIR", str(tmp_path))
    f = _progress(tmp_path, "1", 999999)

    def _boom(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr("psutil.Process", _boom)
    assert nightly.reap_orphan_workers() == []
    assert not f.exists()


# ---- an unknown repo id must never strand the global slot -------------------------------------
# Reproduced live: /api/index/build?repo=bad answered "started: true", the thread bailed at the
# unknown-repo check ABOVE the try/finally that releases, and every later index request was refused
# with "repo bad is indexing" until a restart. repo_kind() answering "kopia" for a nonexistent id
# is why the endpoint guard did not stop it.
def test_unknown_repo_does_not_jam_the_heavy_pass_slot():
    from app.core import nightly

    assert nightly.reserved_repo() is None, "slot dirty before the test"
    r = nightly.start_index_pass_async("definitely-not-a-repo")
    assert r["started"] is False
    assert nightly.reserved_repo() is None, "an unknown repo left the slot reserved"
    # and a valid request must still work afterwards
    assert nightly._reserve("1") is True
    nightly._release("1")


def test_owned_bail_on_unknown_repo_releases_the_slot():
    """Defence in depth: even if a caller reserves and then hands over an unknown repo."""
    from app.core import nightly

    assert nightly._reserve("bogus") is True
    out = nightly.run_index_pass("bogus", _owned=True)
    assert out.get("skipped") is True
    assert nightly.reserved_repo() is None, "the _owned bail path stranded the slot"


def test_unknown_repo_is_rejected_by_the_api_guard():
    """repo_kind() defaults to 'kopia' for ids that do not exist, so membership must be checked."""
    from fastapi import HTTPException
    from app.api.dashboard import _require_kopia

    for bad in ("bad", "", "99"):
        try:
            _require_kopia(bad)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"_require_kopia accepted unknown repo {bad!r}")
