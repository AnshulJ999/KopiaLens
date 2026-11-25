"""Policy-write jobs (steps, the held delete) and the post-delete index update queue."""

from __future__ import annotations

import time

import pytest

from app.core import jobs, nightly, reindexq


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs.audit, "audit", lambda *a, **k: None)
    monkeypatch.setattr(nightly, "is_running", lambda repo: False)
    monkeypatch.setattr(reindexq, "_PENDING", {})
    monkeypatch.setattr(reindexq, "_path", lambda: str(tmp_path / "reindex_pending.json"))
    monkeypatch.setattr(reindexq, "_ensure_thread", lambda: None)
    monkeypatch.setattr(reindexq, "delay", lambda: 300)


def _wait_done(job, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job["state"] != "running":
            return
        time.sleep(0.02)
    raise AssertionError(job)


def _step(rc, results):
    def run(job, lf):
        jobs.say(job, lf, "step")
        job["results"].append({"rc": rc})
        results.append(rc)
        return rc, "boom" if rc else ""
    return run


def test_failed_rule_write_holds_the_delete(monkeypatch):
    ran = []
    monkeypatch.setattr(jobs, "_stream_one", lambda job, cmd, lf: (ran.append(cmd), (0, ""))[1])
    params = {"steps": [_step(0, []), _step(1, [])],
              "then": {"kind": "snapshot_delete", "params": {"snapshot_ids": ["s1"]}}}
    job = jobs.start_job("policy_write", "1", params)
    _wait_done(job)
    assert job["held"] is True and job["state"] == "failed"
    assert ran == []                                  # the delete never started
    assert job["mutating"] is True and job["kind_label"] == "Excluding and deleting"


def test_rule_writes_then_delete_run_in_order(monkeypatch):
    ran = []
    monkeypatch.setattr(jobs, "_stream_one", lambda job, cmd, lf: (ran.append(cmd), (0, ""))[1])
    order = []
    params = {"steps": [_step(0, order)],
              "then": {"kind": "snapshot_delete", "params": {"snapshot_ids": ["s1"]}}}
    job = jobs.start_job("policy_write", "1", params)
    _wait_done(job)
    assert job["state"] == "done" and not job["held"]
    assert order == [0] and len(ran) == 1 and "--delete" in ran[0]
    assert "snapshot" in job["commands_text"]


def test_plain_rule_write_is_not_mutating():
    job = jobs.start_job("policy_write", "1", {"steps": [_step(0, [])], "then": None})
    _wait_done(job)
    assert job["mutating"] is False and job["state"] == "done"


def test_queue_waits_and_new_deletes_push_it_out(monkeypatch):
    reindexq.request("1", now=1000)
    assert reindexq.pending()[0]["due"] == 1300
    reindexq.touch("1", now=1200)
    assert reindexq.pending()[0]["due"] == 1500
    reindexq.touch("2", now=1200)                    # nothing queued for repo 2
    assert [p["repo"] for p in reindexq.pending()] == ["1"]


def test_queue_survives_a_restart(monkeypatch):
    reindexq.request("3", now=1000)
    monkeypatch.setattr(reindexq, "_PENDING", {})
    reindexq.load()
    assert reindexq.pending() == [{"repo": "3", "requested": 1000.0, "due": 1300.0, "reason": "delete"}]


def test_queue_drops_when_a_later_pass_already_ran(monkeypatch):
    reindexq.request("1", now=time.time() - 10)
    monkeypatch.setattr(nightly, "state", lambda repo: {"started": time.time(), "running": False})
    assert reindexq._try_start("1")["reason"] == "already up to date"
    assert reindexq.pending() == []


def test_queue_keeps_the_request_when_the_slot_is_busy(monkeypatch):
    from app.api import actions
    reindexq.request("1", now=time.time() - 1000)
    monkeypatch.setattr(nightly, "state", lambda repo: {})
    monkeypatch.setattr(actions, "_start_index_run", lambda repo: {"started": False, "reason": "repo 2 is indexing"})
    assert reindexq._try_start("1")["started"] is False
    assert [p["repo"] for p in reindexq.pending()] == ["1"]
