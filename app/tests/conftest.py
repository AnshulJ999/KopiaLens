"""Shared test isolation.

Every on-disk side-channel the app appends to (audit trail, run history, job history) is
redirected to a per-test scratch dir. Without this, a test that exercises a code path which
WRITES one of those records (e.g. run_index_pass, a policy backup/restore, a finished job)
silently appends fake entries to the LIVE data/ files - observed 2026-09-13: pytest runs left
"policy-restore repo 1" audit lines and two 0.2s run records in data/runs/repo2.jsonl.

A test that needs a specific path still redirects it itself (monkeypatch runs after this
fixture); this is only the floor.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_disk_side_channels(tmp_path, monkeypatch):
    from app.core import applog, audit, jobs, runhistory

    monkeypatch.setattr(audit, "_path", lambda: str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(runhistory, "_runs_dir", lambda: str(tmp_path / "runs"))
    monkeypatch.setattr(jobs, "_history_path", lambda: str(tmp_path / "jobs_history.jsonl"))
    #: every log writer too. The dry-run tests used to drop `dry_run_*.log` files into
    # the developer's real data folder on every suite run (the paths are read from applog at call
    # time by kopia/jobs/nightly, so patching here covers all of them).
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(applog, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(applog, "APP_LOG_DIR", str(log_dir / "app"))
    monkeypatch.setattr(applog, "INDEX_LOG_DIR", str(log_dir / "indexing"))
    monkeypatch.setattr(applog, "JOB_LOG_DIR", str(log_dir / "jobs"))
    monkeypatch.setattr(applog, "LOG_FILE", str(log_dir / "app" / "kopialens.log"))
    monkeypatch.setattr(applog, "DEBUG_LOG_FILE", str(log_dir / "app" / "kopialens-debug.log"))
