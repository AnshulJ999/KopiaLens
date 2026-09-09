"""The shared job runner: lifecycle, concurrency, stop, cursor status, command building."""

from __future__ import annotations

import re
import threading
import time

import pytest

from app.core import jobs, nightly
from app.core.kopia import maintenance_run_command


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No real data dir, no real audit writes, no console dance, a fresh registry per test."""
    monkeypatch.setattr(jobs, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_send_ctrl_break", lambda proc: False)
    calls = []
    monkeypatch.setattr(jobs.audit, "audit", lambda *a, **k: calls.append((a, k)))
    return calls


class FakeProc:
    """Minimal Popen stand-in: records termination, reports itself dead once terminated."""

    def __init__(self, release=None, rc=0):
        self.pid = 4242
        self.rc = rc
        self.terminated = False
        self._release = release
        self._alive = True

    def poll(self):
        return None if self._alive else self.rc

    def terminate(self):
        self.terminated = True
        self._alive = False
        if self._release:
            self._release.set()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.rc


def _fake_stream(lines, rc=0, release=None, proc=None):
    """A kopia_job_stream stand-in. With ``release`` it blocks like a long command until the
    (fake) process is terminated or the event is set - the path a real Stop takes."""
    def gen(repo, args, result):
        p = proc if proc is not None else FakeProc(release, rc)
        result["proc"] = p
        if release is not None:
            while not release.wait(timeout=0.02):
                if p.poll() is not None:
                    break
        for ln in lines:
            yield ("stderr", ln)
        result["returncode"] = 1 if p.terminated else rc
    return gen


def _wait_state(job, state, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if jobs._JOBS.get(job["job_id"], {}).get("state") == state:
            return
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {state}: {jobs._JOBS.get(job['job_id'])}")


def _wait_running(job, timeout=3.0):
    """Wait until the job thread has actually entered the command stream (its proc exists),
    so assertions about stop/refusal are not racing the thread start."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (job.get("_stream") or {}).get("proc") is not None:
            return
        time.sleep(0.02)
    raise AssertionError("job thread never started its command")


# ---- lifecycle ----

def test_a_job_runs_to_done_with_stamped_lines_and_one_audit_line(isolated, monkeypatch):
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["loading repository", "Fixed 3 snapshots"]))
    job = jobs.start_job("remove_files", "1", {"items": ["x"], "flag": "--object-id"})
    _wait_state(job, "done")
    s = jobs.job_status(job["job_id"], after=0)
    assert s["state"] == "done" and s["rc"] == 0 and s["total"] == 1 and s["done"] == 1
    assert all(re.match(r"^\d\d:\d\d:\d\d ", ln) for ln in s["lines"])
    assert s["lines"][-1].endswith("Fixed 3 snapshots")
    log = open(job["log_path"], encoding="utf-8").read()
    assert "Fixed 3 snapshots" in log
    assert len(isolated) == 1
    (args, kwargs) = isolated[0]
    assert args[0] == "executed" and args[1] == "remove-files" and kwargs["rc"] == 0


def test_a_failed_single_command_job_fails_with_the_stderr_tail(isolated, monkeypatch):
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["unable to lock repository"], rc=1))
    job = jobs.start_job("maintenance", "1", {"full": True})
    _wait_state(job, "failed")
    s = jobs.job_status(job["job_id"], after=0)
    assert "exit 1" in s["error"] and "unable to lock" in s["error"]


def test_a_failed_chunk_counts_as_an_error_but_the_job_completes(isolated, monkeypatch):
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["boom"], rc=1))
    job = jobs.start_job("remove_files", "1", {"items": ["x"], "flag": "--object-id"})
    _wait_state(job, "done")
    s = jobs.job_status(job["job_id"], after=0)
    assert s["errors"] == 1 and s["state"] == "done" and s["rc"] == 1


def test_a_dry_run_audits_as_generated(isolated, monkeypatch):
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["will remove file x"]))
    job = jobs.start_job("remove_files", "1", {"items": ["x"], "flag": "--object-id", "dry": True})
    _wait_state(job, "done")
    assert isolated[0][0][0] == "generated"


# ---- concurrency ----

def test_a_second_mutating_job_on_the_same_repo_is_refused_but_other_repos_run(monkeypatch, isolated):
    release = threading.Event()
    proc = FakeProc(release)
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["working"], release=release, proc=proc))
    job = jobs.start_job("remove_files", "1", {"items": ["a"], "flag": "--object-id"})
    _wait_running(job)
    with pytest.raises(jobs.JobRefused):
        jobs.start_job("snapshot_delete", "1", {"snapshot_ids": ["s1"]})
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["ok"]))
    other = jobs.start_job("snapshot_delete", "2", {"snapshot_ids": ["s1"]})
    _wait_state(other, "done")
    jobs.stop_job(job["job_id"])
    _wait_state(job, "stopped")


def test_restore_runs_concurrently_with_a_mutating_job(monkeypatch, isolated):
    release = threading.Event()
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["working"], release=release))
    job = jobs.start_job("snapshot_delete", "1", {"snapshot_ids": ["s1"]})
    _wait_running(job)
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["restoring"]))
    rest = jobs.start_job("restore", "1", {"source_id": "abc", "target": "D:/out"})
    _wait_state(rest, "done")
    jobs.stop_job(job["job_id"])
    _wait_state(job, "stopped")


def test_a_mutating_job_is_refused_while_the_repo_is_being_indexed(monkeypatch, isolated):
    monkeypatch.setattr(nightly, "is_running", lambda repo: True)
    with pytest.raises(jobs.JobRefused):
        jobs.start_job("maintenance", "1", {"full": True})
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["restoring"]))
    job = jobs.start_job("restore", "1", {"source_id": "abc", "target": "D:/out"})  # read-only: allowed
    _wait_state(job, "done")


def test_dry_runs_do_not_take_the_write_lock(monkeypatch, isolated):
    release = threading.Event()
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["dry"], release=release))
    job = jobs.start_job("remove_files", "1", {"items": ["a"], "dry": True})
    _wait_running(job)
    real = jobs.start_job("snapshot_delete", "1", {"snapshot_ids": ["s1"]})  # not blocked by the dry run
    assert real["state"] == "running"
    jobs.stop_job(job["job_id"])
    jobs.stop_job(real["job_id"])
    _wait_state(job, "stopped")
    _wait_state(real, "stopped")


def test_unknown_kind_and_empty_params_are_refused():
    with pytest.raises(jobs.JobRefused):
        jobs.start_job("policy_write", "1", {})
    with pytest.raises(jobs.JobRefused):
        jobs.start_job("snapshot_delete", "1", {"snapshot_ids": []})


def test_a_non_kopia_repo_is_refused_cleanly():
    with pytest.raises(jobs.JobRefused):
        jobs.start_job("maintenance", "bz", {"full": True})  # kind=backblaze: no kopia args


# ---- stop ----

def test_stop_terminates_a_running_command_and_the_job_ends_stopped(monkeypatch, isolated):
    release = threading.Event()
    proc = FakeProc(release)
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["working"], release=release, proc=proc))
    job = jobs.start_job("maintenance", "1", {"full": True})
    _wait_running(job)
    r = jobs.stop_job(job["job_id"])
    assert r["ok"] and r["how"] == "terminated"
    assert proc.terminated
    _wait_state(job, "stopped")
    assert jobs.stop_job(job["job_id"])["ok"] is False  # already finished: a no-op


def test_stop_between_chunks_runs_no_further_chunk(monkeypatch, isolated):
    job = jobs._new_job("remove_files", "1", "t", {"dry": False},
                        [["kopia", "a"], ["kopia", "b"], ["kopia", "c"]], None)
    jobs._JOBS[job["job_id"]] = job
    seen = []

    def fake(repo, args, result):
        seen.append(args[-1])
        result["proc"] = FakeProc()
        yield ("stderr", "line under " + args[-1])
        result["returncode"] = 0
        job["stop_requested"] = True  # Stop landed while the chunk ran

    monkeypatch.setattr(jobs, "kopia_job_stream", fake)
    jobs._run(job, [["kopia", "a"], ["kopia", "b"], ["kopia", "c"]])
    assert seen == ["a"]
    assert job["state"] == "stopped" and job["done"] == 1


# ---- status cursor + ring ----

def test_the_status_cursor_returns_only_new_lines(monkeypatch, isolated):
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["l1", "l2", "l3"]))
    job = jobs.start_job("maintenance", "1", {"full": True})
    _wait_state(job, "done")
    full = jobs.job_status(job["job_id"], after=0)
    assert full["cursor"] == 3 and len(full["lines"]) == 3
    tail = jobs.job_status(job["job_id"], after=2)
    assert len(tail["lines"]) == 1 and tail["lines"][0].endswith("l3")
    assert jobs.job_status("nope") is None


def test_lines_that_fell_out_of_the_ring_are_reported_as_skipped(monkeypatch, isolated):
    monkeypatch.setattr(jobs, "BUFFER_LINES", 4)
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream([f"l{i}" for i in range(10)]))
    job = jobs.start_job("maintenance", "1", {"full": True})
    _wait_state(job, "done")
    s = jobs.job_status(job["job_id"], after=0)
    assert len(s["lines"]) == 4 and s["cursor"] == 10 and s["skipped"] == 6


def test_list_jobs_returns_every_job_newest_first(monkeypatch, isolated):
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["x"]))
    first = jobs.start_job("maintenance", "1", {"full": True})
    _wait_state(first, "done")
    second = jobs.start_job("restore", "1", {"source_id": "abc", "target": "D:/out"})
    _wait_state(second, "done")
    listed = jobs.list_jobs()
    assert [j["job_id"] for j in listed] == [second["job_id"], first["job_id"]]
    assert all("buffer" not in j and "proc" not in j and "on_done" not in j for j in listed)


# ---- command building ----

def test_commands_come_from_the_real_builders():
    full = maintenance_run_command("1")
    cmds, _ = jobs._build_commands("maintenance", "1", {"full": True})
    assert cmds[0] == full[1:] and cmds[0][-3:] == ["maintenance", "run", "--full"]
    cmds, _ = jobs._build_commands("maintenance", "1", {"full": False})
    assert cmds[0][-2:] == ["maintenance", "run"]
    ids = ["a" * 64, "b" * 64]
    cmds, _ = jobs._build_commands("snapshot_delete", "1", {"snapshot_ids": ids})
    assert all(c[-1] == "--delete" for c in cmds)
    dry, _ = jobs._build_commands("snapshot_delete", "1", {"snapshot_ids": ids, "dry": True})
    assert all("--delete" not in c for c in dry)
    dryrf, _ = jobs._build_commands("remove_files", "1", {"items": ["x", "y"], "dry": True})
    assert all("--commit" not in c for c in dryrf)
    commitrf, _ = jobs._build_commands("remove_files", "1", {"items": ["x", "y"]})
    assert all("--commit" in c for c in commitrf)


def test_the_streamer_never_receives_a_doubled_kopia_prefix(monkeypatch, isolated):
    """Builders emit pasteable commands (leading "kopia"); the streamer consumes bare args and
    prepends its own. Skipping the strip produced "kopia kopia ..." - caught live, so pinned."""
    seen = []

    def fake(repo, args, result):
        seen.append(list(args))
        result["proc"] = FakeProc()
        yield ("stderr", "ok")
        result["returncode"] = 0

    monkeypatch.setattr(jobs, "kopia_job_stream", fake)
    for kind, params in (("maintenance", {"full": True}),
                         ("remove_files", {"items": ["x"], "flag": "--object-id"}),
                         ("snapshot_delete", {"snapshot_ids": ["a" * 64]}),
                         ("restore", {"source_id": "abc", "target": "D:/out"})):
        job = jobs.start_job(kind, "1", params)
        _wait_state(job, "done")
    assert seen and all(c[0] != "kopia" for c in seen)


def test_maintenance_running_is_scoped_and_state_gated(monkeypatch, isolated):
    monkeypatch.setattr(jobs, "kopia_job_stream", _fake_stream(["gc found 0 unused contents"]))
    assert jobs.maintenance_running() is False
    job = jobs.start_job("maintenance", "1", {"full": True})
    assert jobs.maintenance_running() is True and jobs.maintenance_running("1") is True
    assert jobs.maintenance_running("2") is False
    _wait_state(job, "done")
    assert jobs.maintenance_running() is False


# ---- wiring ----

def test_the_job_routes_are_registered():
    from app.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert {"/api/jobs/start", "/api/jobs/status", "/api/jobs/stop", "/api/jobs/list"} <= paths


def test_the_nightly_waits_while_a_maintenance_job_runs(monkeypatch):
    from app import main as m
    started = []
    monkeypatch.setattr(m.nightly, "any_running", lambda: False)
    monkeypatch.setattr(m.jobrunner, "maintenance_running", lambda repo=None: True)
    monkeypatch.setattr(m.nightly, "start_index_pass_async",
                        lambda repo, on_done=None: started.append(repo) or {"started": True})
    monkeypatch.setattr(m, "_index_due", lambda repo, now_dt, now_ts: True)
    monkeypatch.setattr(m, "_light_interval", lambda: 10 ** 9)
    m._last_light = time.time()
    m._tick()
    assert started == []
