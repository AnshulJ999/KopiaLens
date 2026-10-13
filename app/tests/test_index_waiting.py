"""An index run never waits forever: an unreachable repository is given up on after the wait limit
(progress kept, the run retried), and a tree read whose counters stop moving is stopped as stuck,
while a slow one that keeps moving is left alone."""

from __future__ import annotations

import subprocess

import pytest

from app.core import nightly
from app.index import treesync


def test_an_unreachable_repository_is_given_up_on_after_the_wait_limit(monkeypatch):
    monkeypatch.setattr(treesync, "kopia", lambda repo, args: subprocess.CompletedProcess(args, 1, "", "unreachable"))
    monkeypatch.setattr(treesync, "_wait_limit", lambda: 60.0)
    monkeypatch.setattr(treesync, "_WAIT_START", 0)
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(treesync.time, "time", lambda: next(clock))
    monkeypatch.setattr(treesync.time, "sleep", lambda s: None)
    monkeypatch.setitem(treesync._CTX, "should_stop", lambda: False)
    with pytest.raises(treesync.GaveUpWaiting):
        treesync._wait_until_reachable("3")


def _clock_with(monkeypatch, progress):
    monkeypatch.setattr(nightly, "worker_progress", lambda repo: progress())
    asked = []
    monkeypatch.setattr("app.index.resume.request_stop", lambda repo: asked.append(repo))
    return nightly._WorkClock(), asked


def test_a_read_that_stops_moving_is_stopped_as_stuck(monkeypatch):
    clock, asked = _clock_with(monkeypatch, lambda: {"phase": "tree sync", "sync": {"level": 3, "level_read": 10, "read": 500}})
    for minute in range(70):
        clock.worked = minute * 60.0
        clock.stuck("3", 3600)
    assert asked == ["3"] and clock.cause == "stuck"


def test_a_slow_read_that_keeps_moving_is_left_alone(monkeypatch):
    n = iter(range(10_000))
    clock, asked = _clock_with(monkeypatch, lambda: {"phase": "tree sync", "sync": {"level": 3, "level_read": 10, "read": next(n)}})
    for minute in range(200):
        clock.worked = minute * 60.0
        clock.stuck("3", 3600)
    assert asked == []


def test_waiting_for_the_repository_and_later_stages_are_not_watched(monkeypatch):
    states = iter([{"phase": "tree sync", "sync": {"read": 5, "waiting_since": 1}}] * 50 + [{"phase": "maps"}] * 100)
    clock, asked = _clock_with(monkeypatch, lambda: next(states))
    for minute in range(150):
        clock.worked = minute * 60.0
        clock.stuck("3", 3600)
    assert asked == []
