""" run history + job history, and the never-indexed status shape ().

The Logs tab reads these records; the invariants are: append-only, best-effort (a history
failure never breaks the pass or job it describes), newest-first reads, and a run record that
exists even when the worker never survived to report one.
"""

from __future__ import annotations

import json

import pytest

from app.core import jobs as jobs_mod
from app.core import runhistory


# ---- run history -----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def redirect_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(runhistory, "_runs_dir", lambda: str(tmp_path / "runs"))


def test_run_record_round_trips_the_worker_summary():
    runhistory.record("1", {"ok": True, "stages": {"index": 6.7, "analysis": 4.8},
                            "errors": {}, "skipped": []},
                      trigger="nightly", full=False, elapsed=25.4)
    rec = runhistory.read_tail(5)[0]
    assert rec["repo"] == "1" and rec["trigger"] == "nightly" and rec["ok"] is True
    assert rec["stages"]["index"] == 6.7
    assert rec["elapsed"] == 25.4
    assert rec["ts"]


def test_a_killed_run_still_gets_a_record():
    """The app writes the record, so a worker that died (timeout) is on the list too -
    the exact gap a worker-written summary would have."""
    runhistory.record("2", {"ok": False, "reason": "timeout"}, trigger="nightly",
                      full=False, elapsed=14400.0)
    rec = runhistory.read_tail(5)[0]
    assert rec["ok"] is False and rec["reason"] == "timeout"


def test_read_tail_merges_repos_newest_first_and_caps():
    runhistory.record("1", {"ok": True}, trigger="manual", full=False, elapsed=1.0)
    runhistory.record("3", {"ok": True}, trigger="nightly", full=False, elapsed=2.0)
    runhistory.record("1", {"ok": False, "reason": "x"}, trigger="manual", full=False, elapsed=3.0)
    got = runhistory.read_tail(10)
    assert [r["elapsed"] for r in got] == [3.0, 2.0, 1.0]   # newest first, all repos merged
    assert len(runhistory.read_tail(2)) == 2


def test_read_tail_is_empty_when_nothing_was_recorded():
    assert runhistory.read_tail(10) == []


# ---- job history -----------------------------------------------------------------------

@pytest.fixture
def job_env(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "_history_path", lambda: str(tmp_path / "jobs_history.jsonl"))
    return tmp_path


def _finished_job(**over):
    job = {"job_id": "abc123", "kind": "remove_files", "kind_label": "Removing files",
           "repo": "3", "label": "2 object(s)", "dry": False, "mutating": True,
           "state": "done", "rc": 0, "error": None, "detail": {"items": 2},
           "created": 1000.0, "started": 1001.0, "finished": 1005.0,
           "total": 1, "done": 1, "errors": 0, "log_path": "x.log"}
    job.update(over)
    return job


def test_job_history_round_trip(job_env):
    jobs_mod._record_history(_finished_job())
    got = jobs_mod.read_job_history(10)
    assert got[0]["job_id"] == "abc123"
    assert got[0]["elapsed"] == 4.0
    assert got[0]["detail"] == {"items": 2}
    assert "commands_text" not in got[0]  # compact: the log file holds the command text


def test_job_history_newest_first(job_env):
    jobs_mod._record_history(_finished_job(job_id="old", created=10.0))
    jobs_mod._record_history(_finished_job(job_id="new", created=20.0))
    got = jobs_mod.read_job_history(10)
    assert [j["job_id"] for j in got] == ["new", "old"]


def test_job_history_read_is_empty_without_a_file():
    assert jobs_mod.read_job_history(10) == []


# ---- never-indexed repo status ---------------------------------------------------------

def test_index_status_reports_a_never_indexed_repo(monkeypatch):
    """A repo with no published generation is a normal state, not a 500: /api/index/status
    must answer zeros + never_indexed so the UI can offer the Index button (observed live on a
    fresh repo 3, 2026-09-13 - the button was greyed out and every page errored)."""
    from app.index import builder
    monkeypatch.setattr(builder, "sources_index", lambda repo: {"u@h:C:": []})
    monkeypatch.setattr(builder, "_has_index", lambda repo: False)
    out = builder.index_status("3")
    assert out["never_indexed"] is True
    assert out["building"] is False
    assert out["indexed_snapshots"] == 0 and out["files"] == 0
    assert out["error"] is None


def test_status_with_summary_skips_the_summary_for_a_never_indexed_repo(monkeypatch):
    """/api/index/status goes through index_status_with_summary; a repo with no index must not
    reach index_summary (no generation to open - it answered 500 on a fresh install)."""
    from app.index import builder
    monkeypatch.setattr(builder, "sources_index", lambda repo: {"u@h:C:": []})
    monkeypatch.setattr(builder, "_has_index", lambda repo: False)
    monkeypatch.setattr(builder, "_read_status_cache", lambda repo: None)

    def _no_generation(repo):
        raise FileNotFoundError(f"repo {repo!r} has no index (no manifest.json)")
    monkeypatch.setattr("app.index.queries.index_summary", _no_generation)
    out = builder.index_status_with_summary("3")
    assert out["never_indexed"] is True
    assert "summary" not in out


def test_index_status_normal_repo_is_not_flagged(monkeypatch):
    from app.index import builder

    class _FakeCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            return None

        def execute(self, q):
            class R:
                def __init__(self, v):
                    self.v = v

                def fetchone(self):
                    return (self.v,)

                def fetchall(self):
                    return []
            if "COUNT(*) FROM isnaps" in q:
                return R(4)
            if "COUNT(*) FROM filev" in q:
                return R(952)
            return R(None)

    monkeypatch.setattr(builder, "sources_index", lambda repo: {"u@h:C:": []})
    monkeypatch.setattr(builder, "_has_index", lambda repo: True)
    monkeypatch.setattr(builder, "read_cursor", lambda repo: _FakeCur())
    monkeypatch.setattr(builder, "_JOBS", {})
    out = builder.index_status("3")
    assert out["never_indexed"] is False
    assert out["indexed_snapshots"] == 4 and out["files"] == 952
