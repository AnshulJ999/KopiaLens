"""`blob stats` runs only as a user-started job: one per repo, stoppable, output kept for Stats."""

from __future__ import annotations

from collections import deque

import pytest

from app.api import jobs as jobs_api
from app.api import stats
from app.core import jobs


def test_builds_only_blob_stats(monkeypatch):
    monkeypatch.setattr(jobs, "_kopia_exe", lambda: "kopia")
    monkeypatch.setattr(jobs, "_repo_args", lambda repo: ["--config-file", "r.cfg"])
    cmds, meta = jobs._build_commands("blob_stats", "1", {})
    assert cmds == [["blob", "stats"]]
    assert jobs.KINDS["blob_stats"]["mutating"] is False


def test_a_second_run_on_the_same_repo_is_refused(monkeypatch):
    monkeypatch.setattr(jobs, "list_jobs", lambda: [{"kind": "blob_stats", "repo": "1", "state": "running"}])
    with pytest.raises(jobs.JobRefused):
        jobs_api._resolve_params("blob_stats", "1", {})
    assert jobs_api._resolve_params("blob_stats", "2", {}) == {}


def test_finished_output_fills_the_stats_card(monkeypatch):
    monkeypatch.setattr(stats, "_blob_cache", {})
    job = {"kind": "blob_stats", "repo": "1", "state": "done",
           "buffer": deque(["12:00:01 Count: 41210", "12:00:02 Total: 1.2 TB"])}
    jobs_api._on_done(job)
    assert stats._blob_cache["1"] == "Count: 41210\nTotal: 1.2 TB"
    jobs_api._on_done(dict(job, repo="2", state="stopped"))
    assert "2" not in stats._blob_cache
