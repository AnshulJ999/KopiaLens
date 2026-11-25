"""Logs tab routes - the pieces that are not audit, jobs or index runs.

The app log (data/logs/kopialens.log) and, when enabled, the debug log
(data/logs/kopialens-debug.log) are surfaced here as parsed records, newest first, so "what has
the app been doing" is answerable inside the UI. Read-only, tail of the current file only.
"""

from __future__ import annotations

import datetime
import os
import re

from fastapi import APIRouter, HTTPException

from ..core.applog import DEBUG_LOG_FILE, LOG_FILE

router = APIRouter(tags=["logs"])

_TAIL_BYTES = 524_288
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
# "2026-09-13 06:49:58,295 INFO  kopialens.app | msg" (the tray writes the same shape without ms)
_RECORD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,(\d{3}))? +([A-Z]+) +(\S+) \| ?(.*)$")


def parse_records(text: str) -> list[dict]:
    """Group raw log text into records (a traceback's lines stay with the line that raised),
    oldest first. Lines before the first recognised record are dropped (a partial tail)."""
    out: list[dict] = []
    for line in text.splitlines():
        m = _RECORD.match(line)
        if m:
            stamp, ms, level, name, msg = m.groups()
            try:
                ts = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp() + int(ms or 0) / 1000
            except ValueError:
                ts = None
            out.append({"ts": ts, "level": level, "logger": name, "message": msg})
        elif out:
            out[-1]["message"] += "\n" + line
    return out


@router.get("/logs/app")
def app_log_tail(limit: int = 500, file: str = "app", level: str = "info") -> dict:
    """The newest ``limit`` records of the app or debug log at ``level`` and above."""
    limit = max(1, min(limit, 2000))
    path = DEBUG_LOG_FILE if file == "debug" else LOG_FILE
    min_level = _LEVELS.get(level.upper(), 20)
    from ..core import filecrypto
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"log not found: {exc}") from exc
    tail = filecrypto.read_tail_text(path, _TAIL_BYTES)
    records = [r for r in parse_records(tail) if _LEVELS.get(r["level"], 20) >= min_level]
    records.reverse()
    return {"records": records[:limit], "path": path, "file": "debug" if file == "debug" else "app",
            "debug_available": os.path.exists(DEBUG_LOG_FILE),
            "truncated": len(records) > limit or size > _TAIL_BYTES}
