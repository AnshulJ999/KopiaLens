"""Pausing background indexing: timed pauses end on their own, and a pause holds the automatic
index update after deletes while anything the user starts still runs."""
from __future__ import annotations

import pytest

from app.api import actions
from app.core import jobs, nightly, pause, reindexq


@pytest.fixture(autouse=True)
def _clean():
    pause.resume()
    yield
    pause.resume()


def test_a_timed_pause_ends_by_itself():
    pause.pause(4, now=1000.0)
    assert pause.active(now=1000.0 + 3 * 3600)
    assert not pause.active(now=1000.0 + 4 * 3600)


def test_until_resumed_has_no_end():
    pause.pause(0, now=1000.0)
    assert pause.state(now=1e12) == {"paused": True, "since": 1000.0, "until": None}


@pytest.mark.parametrize("hours", [-1, 24 * 400])
def test_lengths_outside_zero_to_a_year_are_refused(hours):
    with pytest.raises(ValueError):
        pause.pause(hours)


def test_a_pause_survives_a_restart(monkeypatch):
    pause.pause(0, now=1000.0)
    monkeypatch.setattr(pause, "_loaded", False)
    monkeypatch.setattr(pause, "_STATE", {"since": None, "until": None})
    assert pause.state(now=2000.0)["paused"]


@pytest.fixture
def queue(monkeypatch):
    started = []
    monkeypatch.setattr(nightly, "state", lambda repo: {})
    monkeypatch.setattr(jobs, "list_jobs", lambda: [])
    monkeypatch.setattr(actions, "_start_index_run", lambda repo: started.append(repo) or {"started": True})
    monkeypatch.setattr(reindexq, "_save", lambda: None)
    monkeypatch.setattr(reindexq, "_PENDING", {})
    return started


@pytest.mark.parametrize("reason, runs", [("delete", False), ("first", True)])
def test_a_pause_holds_the_update_after_deletes_but_not_a_first_index(queue, reason, runs):
    reindexq._PENDING["1"] = {"requested": 0.0, "due": 0.0, "reason": reason}
    pause.pause(0)
    out = reindexq._try_start("1")
    assert bool(queue) is runs
    assert out.get("started") is runs or out.get("reason") == "indexing is paused"


def test_start_now_runs_even_while_paused(queue):
    reindexq._PENDING["1"] = {"requested": 0.0, "due": 1e12, "reason": "delete"}
    pause.pause(0)
    reindexq.run_now("1")
    assert queue == ["1"]
