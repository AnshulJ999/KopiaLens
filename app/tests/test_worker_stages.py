"""Which nightly stages actually run, and which are skipped when a prerequisite fails.

The two pre-existing tests for this read the source text and only prove that "dashboard" appears
before "analysis". Order is not the dependency: the dashboard stage has to SUCCEED, because it is
what rebuilds the global content refcount that the reclaimable analysis and the folder freeable
figures are priced against. These drive the real function instead.
"""

from __future__ import annotations

import pytest

from app.index import worker


class _StubReporter:
    """The real reporter writes progress files into the live data directory and runs a thread."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.stages: list[str] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_stage(self, name: str) -> None:
        self.stages.append(name)


@pytest.fixture
def ran(monkeypatch):
    """Run the worker with every stage stubbed. Returns the list of stages that really executed;
    the test decides which of them is made to fail."""
    executed: list[str] = []

    monkeypatch.setattr(worker, "_Reporter", _StubReporter)
    monkeypatch.setattr(worker, "clear_progress", lambda repo: None)
    monkeypatch.setattr("app.index.store.close_all", lambda: None)
    monkeypatch.setattr("app.index.worker.index_stage",
                        lambda repo, full=False: executed.append("index"))
    monkeypatch.setattr("app.index.snapanalysis.prewarm",
                        lambda repo, with_reclaim=False: executed.append("analysis"))
    monkeypatch.setattr("app.index.churnview.prewarm", lambda repo: executed.append("churn"))
    monkeypatch.setattr("app.index.foldercache.prewarm", lambda repo: executed.append("folders"))
    monkeypatch.setattr("app.core.policy.refresh_policies",
                        lambda repo, force=False: executed.append("policies"))
    monkeypatch.setattr("app.core.schedule.compute_next_snapshots",
                        lambda repo: executed.append("next_snapshots"))
    monkeypatch.setattr("app.core.metrics.table_rows",
                        lambda repo, refresh=False, mode="all": executed.append("dashboard"))
    return executed


def _fail(name: str, monkeypatch, target: str) -> None:
    def boom(*_a, **_kw):
        raise RuntimeError(f"{name} exploded")
    monkeypatch.setattr(target, boom)


def test_every_stage_runs_when_nothing_fails(ran):
    summary = worker.run("1")
    assert summary["ok"] is True
    assert ran == ["index", "dashboard", "analysis", "churn", "folders", "policies",
                   "next_snapshots"]
    assert summary["skipped"] == []


def test_a_failed_dashboard_skips_the_stages_that_price_against_the_refcount(ran, monkeypatch):
    """Analysis (reclaimable) and folders (freeable) both read the refcount the dashboard stage
    rebuilds. Running them against a stale one persists wrong delete-safety numbers as if current -
    the precise failure the stage order was introduced to prevent."""
    _fail("dashboard", monkeypatch, "app.core.metrics.table_rows")

    summary = worker.run("1")

    assert "analysis" not in ran
    assert "folders" not in ran
    assert set(summary["skipped"]) == {"analysis", "folders"}
    assert summary["ok"] is False


def test_a_failed_dashboard_does_not_block_independent_stages(ran, monkeypatch):
    """Churn, policies and next-snapshot times do not read the refcount, so losing the dashboard
    must not cost them too. Skipping everything would be its own kind of wrong."""
    _fail("dashboard", monkeypatch, "app.core.metrics.table_rows")

    worker.run("1")

    assert "churn" in ran
    assert "policies" in ran
    assert "next_snapshots" in ran


def test_a_failed_index_skips_everything_downstream(ran, monkeypatch):
    """A half-updated index makes every downstream number wrong, not just the refcount ones."""
    _fail("index", monkeypatch, "app.index.worker.index_stage")

    summary = worker.run("1")

    assert ran == []
    assert summary["ok"] is False
    assert set(summary["skipped"]) == {"dashboard", "analysis", "churn", "folders", "policies",
                                       "next_snapshots"}


def test_one_failing_independent_stage_does_not_stop_the_others(ran, monkeypatch):
    """A churn rollup that throws must not cost the night its policies or next-snapshot times."""
    _fail("churn", monkeypatch, "app.index.churnview.prewarm")

    summary = worker.run("1")

    assert "folders" in ran and "policies" in ran and "next_snapshots" in ran
    assert summary["ok"] is False
    assert "churn" in summary["errors"]
