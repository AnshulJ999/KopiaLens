"""Logging levels, the debug log file, polling summaries and the Logs tab's record parser."""

from __future__ import annotations

import logging

import pytest

from app.api import logs as logs_api
from app.core import applog


# ---- debug log toggle --------------------------------------------------------------------

@pytest.fixture
def debug_env(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "DEBUG_LOG_FILE", str(tmp_path / "kopialens-debug.log"))
    monkeypatch.setattr(applog, "LOG_DIR", str(tmp_path))
    monkeypatch.delenv("KOPIALENS_DEBUG_LOG", raising=False)
    root = logging.getLogger("kopialens")
    level = root.level
    yield tmp_path
    applog.apply_logging_settings({"kopialens": {"debug_log": False}})
    root.setLevel(level)


def test_debug_log_attaches_and_detaches(debug_env):
    root = logging.getLogger("kopialens")
    applog.apply_logging_settings({"kopialens": {"debug_log": True}})
    assert applog._debug_handler is not None and root.level == logging.DEBUG
    applog.get_logger("test").debug("a debug line")
    applog._debug_handler.flush()
    assert "a debug line" in (debug_env / "kopialens-debug.log").read_text(encoding="utf-8")
    applog.apply_logging_settings({"kopialens": {"debug_log": False}})
    assert applog._debug_handler is None and root.level == logging.INFO


def test_env_overrides_the_setting(debug_env, monkeypatch):
    monkeypatch.setenv("KOPIALENS_DEBUG_LOG", "0")
    assert applog.debug_log_wanted({"kopialens": {"debug_log": True}}) is False
    monkeypatch.setenv("KOPIALENS_DEBUG_LOG", "1")
    assert applog.debug_log_wanted({"kopialens": {"debug_log": False}}) is True


def test_debug_log_is_off_by_default(debug_env):
    assert applog.debug_log_wanted({}) is False


# ---- polling summaries -----------------------------------------------------------------

def test_request_stats_summarises_once_per_interval():
    stats = applog.RequestStats(interval=60)
    t0 = stats._since
    assert stats.add("/api/jobs/list", 2, now=t0 + 1) is None
    assert stats.add("/api/jobs/list", 6, now=t0 + 2) is None
    line = stats.add("/api/index/freshness", 4, now=t0 + 61)
    assert line and "3 total" in line
    assert "/api/jobs/list 2x avg 4 ms max 6 ms" in line
    assert stats.add("/api/jobs/list", 1, now=t0 + 62) is None   # a new interval started


# ---- Logs tab record parser ---------------------------------------------------------------

_SAMPLE = """half a line from the seek
2026-09-13 06:49:58,295 INFO  kopialens.app | KopiaLens starting up
2026-09-13 06:50:01,000 DEBUG kopialens.sources | sources_index built
2026-09-13 06:50:02,500 ERROR kopialens.app | bg tick failed: boom
Traceback (most recent call last):
  File "x.py", line 1
2026-09-13 06:50:03 INFO  kopialens.tray | tray control: "POST /restart HTTP/1.1" 200 -
"""


def test_parse_records_groups_tracebacks_and_drops_the_partial_head():
    recs = logs_api.parse_records(_SAMPLE)
    assert [r["level"] for r in recs] == ["INFO", "DEBUG", "ERROR", "INFO"]
    assert "Traceback" in recs[2]["message"] and 'File "x.py"' in recs[2]["message"]
    assert recs[3]["logger"] == "kopialens.tray" and recs[3]["ts"]


def test_app_log_endpoint_is_newest_first_and_level_filtered(tmp_path, monkeypatch):
    p = tmp_path / "kopialens.log"
    p.write_text(_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(logs_api, "LOG_FILE", str(p))
    monkeypatch.setattr(logs_api, "DEBUG_LOG_FILE", str(tmp_path / "missing-debug.log"))
    out = logs_api.app_log_tail(limit=10, level="info")
    assert [r["level"] for r in out["records"]] == ["INFO", "ERROR", "INFO"]   # newest first, no DEBUG
    assert out["records"][0]["logger"] == "kopialens.tray"
    assert out["debug_available"] is False
    errs = logs_api.app_log_tail(limit=10, level="error")
    assert [r["level"] for r in errs["records"]] == ["ERROR"]
