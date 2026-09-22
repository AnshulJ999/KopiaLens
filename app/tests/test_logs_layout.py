"""The log layout: app/ + indexing/ + jobs/, the one-time move, the cleanup setting, and the
readable index-pass line ( covers the test-side isolation in conftest).
"""

from __future__ import annotations

import os
import time

from app.core import applog
from app.core import nightly


def _wire(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(applog, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(applog, "APP_LOG_DIR", str(log_dir / "app"))
    monkeypatch.setattr(applog, "INDEX_LOG_DIR", str(log_dir / "indexing"))
    monkeypatch.setattr(applog, "JOB_LOG_DIR", str(log_dir / "jobs"))
    monkeypatch.setattr(applog, "LOG_FILE", str(log_dir / "app" / "kopialens.log"))
    monkeypatch.setattr(applog, "DEBUG_LOG_FILE", str(log_dir / "app" / "kopialens-debug.log"))
    return log_dir


def test_migrate_moves_flat_files_into_the_three_groups(tmp_path, monkeypatch):
    log_dir = _wire(tmp_path, monkeypatch)
    log_dir.mkdir(parents=True)
    for name in ("kopialens.log", "kopialens.log.3", "kopialens-debug.log",
                 "kopia-server-repo1.log", "index_worker_repo1.log", "index_worker_repo1.log.2",
                 "job_snapshot_delete_repo1_20260923_010101_abc.log",
                 "dry_run_remove_files_repo1_20260923_010101.log",
                 "mount_20260923_010101.log"):
        (log_dir / name).write_text("x", encoding="utf-8")
    (log_dir / "notes.txt").write_text("keep me", encoding="utf-8")

    assert applog.migrate() == 9
    assert (log_dir / "app" / "kopialens.log").exists()
    assert (log_dir / "app" / "kopialens.log.3").exists()
    assert (log_dir / "app" / "kopia-server-repo1.log").exists()
    assert (log_dir / "indexing" / "index_worker_repo1.log.2").exists()
    assert (log_dir / "jobs" / "mount_20260923_010101.log").exists()
    assert (log_dir / "jobs" / "dry_run_remove_files_repo1_20260923_010101.log").exists()
    assert (log_dir / "notes.txt").exists()          # not a log: untouched
    assert applog.migrate() == 0                     # idempotent


def test_migrate_never_clobbers_an_existing_destination(tmp_path, monkeypatch):
    log_dir = _wire(tmp_path, monkeypatch)
    log_dir.mkdir(parents=True)
    (log_dir / "kopialens.log").write_text("legacy", encoding="utf-8")
    (log_dir / "app").mkdir()
    (log_dir / "app" / "kopialens.log").write_text("current", encoding="utf-8")

    assert applog.migrate() == 0
    assert (log_dir / "app" / "kopialens.log").read_text(encoding="utf-8") == "current"
    assert (log_dir / "kopialens.log").read_text(encoding="utf-8") == "legacy"


def test_cleanup_off_keeps_everything_and_the_newest_hundred_survive_otherwise(tmp_path,
                                                                               monkeypatch):
    log_dir = _wire(tmp_path, monkeypatch)
    jobs = log_dir / "jobs"
    jobs.mkdir(parents=True)
    now = time.time()
    for i in range(105):
        p = jobs / f"job_x_{i:03d}.log"
        p.write_text("x", encoding="utf-8")
        os.utime(p, (now - (105 - i) * 86400, now - (105 - i) * 86400))

    assert applog.cleanup_jobs(days=0, now=now) == 0          # OFF
    assert len(list(jobs.iterdir())) == 105
    assert applog.cleanup_jobs(days=90, now=now) == 5         # 105 - the newest 100
    left = sorted(p.name for p in jobs.iterdir())
    assert len(left) == 100 and left[0] == "job_x_005.log"


def test_cleanup_reads_the_setting(tmp_path, monkeypatch):
    """The setting decides: 0 keeps everything; N days removes what is old AND outside the newest
    hundred."""
    log_dir = _wire(tmp_path, monkeypatch)
    jobs = log_dir / "jobs"
    jobs.mkdir(parents=True)
    now = time.time()
    for i in range(100):
        p = jobs / f"job_new_{i:03d}.log"
        p.write_text("x", encoding="utf-8")
        os.utime(p, (now - i * 60, now - i * 60))
    old = jobs / "job_old.log"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (now - 200 * 86400,) * 2)

    import app.settings as settings_mod
    monkeypatch.setattr(settings_mod, "load_settings",
                        lambda: {"kopialens": {"log_cleanup_days": 0}})
    assert applog.cleanup_jobs() == 0
    monkeypatch.setattr(settings_mod, "load_settings",
                        lambda: {"kopialens": {"log_cleanup_days": 30}})
    assert applog.cleanup_jobs() == 1
    assert not old.exists()


def test_index_pass_line_is_a_readable_sentence():
    line = nightly.summary_line({"ok": True, "stages": {"index": 245.2, "dashboard": 9.4},
                                 "skipped": ["churn"], "errors": {}}, 500.0)
    assert line == "OK in 500s - index 245s, dashboard 9s - skipped: churn"
    failed = nightly.summary_line({"ok": False, "stages": {}, "errors": {"folders": "boom"}})
    assert failed == "FAILED - errors: folders"
    assert "{" not in line and "{" not in failed


def test_every_index_log_writer_uses_the_new_folders(tmp_path, monkeypatch):
    """The writers read their directory from applog at call time, so the patch above is the
    whole isolation - assert none of them rebuilds a flat path."""
    _wire(tmp_path, monkeypatch)
    assert applog.INDEX_LOG_DIR in nightly.worker_log_path("1")
    from app.core.kopia import remove_log_path
    assert applog.JOB_LOG_DIR in remove_log_path("dry_run_x.log")


def test_the_cleanup_setting_has_a_control_and_a_field_row():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    html = (root / "frontend" / "partials" / "settings.html").read_text(encoding="utf-8")
    js = (root / "frontend" / "js" / "settings.js").read_text(encoding="utf-8")
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "saveKl('log_cleanup_days')" in html
    assert "log_cleanup_days: { s: \"logCleanupDays\"" in js
    assert "logCleanupDays:" in app_js
    from app.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["kopialens"]["log_cleanup_days"] == 90
