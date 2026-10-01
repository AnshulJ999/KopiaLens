"""A restart or quit mid-index must not lose the run.

Seen 2026-09-13: a tray Restart during repo 2's nightly killed the healthy worker at startup and
left the attempt "in progress" forever - no retry, no error, 33 h stale. Workers are now taken over,
their result file is read, and a run that truly died is recorded as interrupted.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from app import main as m
from app.core import nightly, runhistory

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(nightly, "REPOS", {"1": {"name": "Main"}, "2": {"name": "Big"}})
    monkeypatch.setattr(nightly, "worker_timeout", lambda: 3600)
    return tmp_path


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class _Worker:
    """A live kopia worker as psutil sees it: still running for one wait, then exited."""

    def __init__(self, pid, cmd="app.index.worker", on_exit=None):
        self.pid, self._cmd, self._waits, self._on_exit = pid, cmd, 0, on_exit
        self.terminated = False

    def cmdline(self):
        return ["pythonw.exe", "-m", self._cmd, "2"]

    def wait(self, timeout=None):
        self._waits += 1
        if self._waits == 1:
            raise psutil.TimeoutExpired(timeout)
        if self._on_exit:
            self._on_exit()
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def _join_resumed():
    for t in threading.enumerate():
        if t.name.startswith("resumed-worker-"):
            t.join(timeout=10)


def test_a_running_worker_is_taken_over_and_its_result_recorded(data, monkeypatch):
    progress = data / "index_progress_repo2.json"
    _write(progress, {"repo": "2", "pid": 5001, "started": time.time(), "trigger": "nightly", "full": False})
    result = data / "index_result_repo2.json"

    def finish():
        progress.unlink()
        _write(result, {"ok": True, "pid": 5001, "elapsed": 400.0, "trigger": "nightly", "stages": {"index": 1}})

    worker = _Worker(5001, on_exit=finish)
    monkeypatch.setattr("psutil.Process", lambda pid: worker)
    done = []

    out = nightly.resume_workers(on_done=lambda repo, ok, reason: done.append((repo, ok, reason)))
    assert out["adopted"] == ["2"]
    assert not worker.terminated, "a healthy worker must be left running"
    assert nightly.reserved_repo() == "2", "the adopted run holds the slot so nothing starts beside it"
    _join_resumed()

    assert done == [("2", True, None)]
    assert nightly.reserved_repo() is None
    assert not nightly.state("2")["running"]
    assert not result.exists(), "the result file is consumed once recorded"
    runs = runhistory.read_tail(repos=["2"])
    assert runs and runs[0]["ok"] and runs[0]["trigger"] == "nightly"


def test_a_worker_that_exits_without_a_result_is_interrupted(data, monkeypatch):
    _write(data / "index_progress_repo1.json", {"repo": "1", "pid": 5002, "started": time.time()})
    monkeypatch.setattr("psutil.Process", lambda pid: _Worker(5002))
    done = []
    nightly.resume_workers(on_done=lambda repo, ok, reason: done.append((repo, ok, reason)))
    _join_resumed()
    assert done == [("1", False, nightly.INTERRUPTED)]
    assert nightly.reserved_repo() is None


def test_a_result_written_while_the_app_was_closed_is_recorded(data, monkeypatch):
    _write(data / "index_result_repo1.json",
           {"ok": False, "pid": 77, "finished": 5000.0, "elapsed": 60.0, "errors": {"churn": "boom"}})
    done = []
    out = nightly.resume_workers(on_done=lambda repo, ok, reason: done.append((repo, ok, reason)))
    assert out["finished"] == ["1"]
    assert done == [("1", False, "churn: boom")], "a stage failure gets its reason from errors"
    assert not (data / "index_result_repo1.json").exists()


def test_a_dead_worker_with_its_result_is_finished_not_interrupted(data, monkeypatch):
    _write(data / "index_progress_repo1.json", {"repo": "1", "pid": 5003, "started": 1000.0})
    _write(data / "index_result_repo1.json", {"ok": True, "pid": 5003, "finished": 2000.0, "elapsed": 900.0})

    def gone(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr("psutil.Process", gone)
    out = nightly.resume_workers()
    assert out["finished"] == ["1"] and out["interrupted"] == []


def test_a_leftover_backblaze_worker_is_left_to_finish(data, monkeypatch):
    progress = data / "index_progress_repobz.json"
    _write(progress, {"repo": "bz", "pid": 5004})
    worker = _Worker(5004, cmd="app.backblaze.worker")
    monkeypatch.setattr("psutil.Process", lambda pid: worker)
    out = nightly.resume_workers()
    assert out["left_running"] == ["bz"] and not worker.terminated
    assert progress.exists(), "its progress file is how the BZ checks see it running"
    assert nightly.reserved_repo() is None


def test_backblaze_checks_see_a_leftover_worker(tmp_path, monkeypatch):
    from app.backblaze import indexer
    _write(tmp_path / "index_progress_repobz.json", {"repo": "bz", "pid": 5005})
    monkeypatch.setattr(indexer, "_progress_path", lambda: str(tmp_path / "index_progress_repobz.json"))
    monkeypatch.setattr(indexer, "_LEFTOVER_CHECK", [0.0, False])
    monkeypatch.setattr("psutil.Process", lambda pid: _Worker(5005, cmd="app.backblaze.worker"))
    assert indexer.state()["building"] is True
    assert indexer.refresh_via_worker()["reason"] == "already running"


def test_run_worker_consumes_only_its_own_result_file(data, monkeypatch):
    class _Done:
        pid, returncode = 4242, 0

        def communicate(self, timeout=None):
            _write(data / "index_result_repo1.json", {"ok": True, "pid": 4242})
            return json.dumps({"ok": True}), ""

    monkeypatch.setattr(nightly, "worker_log_path", lambda repo: str(data / "w.log"))
    seen_env = {}

    def popen(*a, **k):
        seen_env.update(k.get("env") or {})
        return _Done()

    monkeypatch.setattr(nightly.subprocess, "Popen", popen)
    assert nightly._run_worker("1", False, "nightly")["ok"]
    assert seen_env.get("KOPIALENS_INDEX_TRIGGER") == "nightly"
    assert not (data / "index_result_repo1.json").exists()


def test_stale_in_progress_attempts_are_closed_at_startup(tmp_path, monkeypatch):
    f = tmp_path / "bg_worker_state.json"
    _write(f, {"attempts": {"1": {"slot": 1.0, "at": 2.0, "retries": 0, "ok": None},
                            "2": {"slot": 1.0, "at": 2.0, "retries": 0, "ok": None},
                            "3": {"slot": 1.0, "at": 2.0, "retries": 0, "ok": True}}})
    monkeypatch.setattr(m, "_LAST_CYCLE_FILE", str(f))
    m._close_stale_attempts(adopted=["2"])
    attempts = json.loads(f.read_text(encoding="utf-8"))["attempts"]
    assert attempts["1"]["ok"] is False, "no pass behind it: the retry window must open"
    assert attempts["2"]["ok"] is None, "an adopted run is still in progress"
    assert attempts["3"]["ok"] is True
    assert m.nightly_error("1")["reason"] == nightly.INTERRUPTED


def test_worker_writes_its_result_even_when_stdout_is_gone(tmp_path, monkeypatch):
    from app.core import sizemap, sources
    from app.index import physical, worker

    monkeypatch.setattr(sources, "pin_for_run", lambda: None)
    monkeypatch.setattr(sizemap, "MEMO_ENABLED", sizemap.MEMO_ENABLED)  # main() flips these process-wide
    monkeypatch.setattr(physical, "REFCOUNT_MEMO_ENABLED", physical.REFCOUNT_MEMO_ENABLED)
    monkeypatch.setattr(worker, "result_path", lambda repo: str(tmp_path / f"index_result_repo{repo}.json"))
    monkeypatch.setattr(worker, "run", lambda repo, full=False: {"ok": True, "repo": repo, "elapsed": 1.0})
    monkeypatch.setenv("KOPIALENS_INDEX_TRIGGER", "nightly")

    class _Broken:
        def write(self, _s):
            raise OSError("broken pipe")

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", _Broken())
    assert worker.main(["3"]) == 0
    res = json.loads((tmp_path / "index_result_repo3.json").read_text(encoding="utf-8"))
    assert res["ok"] and res["trigger"] == "nightly" and res["pid"]


def test_reload_watch_does_not_poll_index_status():
    """The old watcher asked /api/index/status every 2 s; each ask could run kopia snapshot list."""
    src = (ROOT / "frontend" / "js" / "jobs.js").read_text(encoding="utf-8")
    start = src.index("_armReindexReload(repo, since)")
    end = src.index("};", start)
    assert "/api/index/status" not in src[start:end]
    assert "setInterval" not in src[start:end], "the watch rides the header chip poll"
    assert "_armReindexReload(p.repo, p.requested)" in src, "a queued update re-arms after a reload"
