"""Pause background indexing for a while (tray or Settings).

Held in memory on purpose: a pause ends when KopiaLens restarts. While paused, scheduled kopia
index runs, the daytime dashboard rebuild, the index update after deletes and the Backblaze
reindex all wait; anything the user starts by hand still runs. A run that was due during the
pause starts at the next tick after it ends (the scheduler's normal catch-up).
"""
from __future__ import annotations

import threading
import time

from .applog import get_logger

log = get_logger("pause")

CHOICES_HOURS = (4, 8, 12)
_LOCK = threading.Lock()
_STATE: dict = {"since": None, "until": None}


def state(now: float | None = None) -> dict:
    """``until`` is None for "until I resume". An expired pause reads as not paused."""
    now = time.time() if now is None else now
    with _LOCK:
        since, until = _STATE["since"], _STATE["until"]
        if since is not None and until is not None and now >= until:
            _STATE.update(since=None, until=None)
            since = until = None
            log.info("indexing pause ended")
    return {"paused": since is not None, "since": since, "until": until}


def active(now: float | None = None) -> bool:
    return state(now)["paused"]


def pause(hours: int | None, now: float | None = None) -> dict:
    """``hours`` from CHOICES_HOURS, or None/0 for until resumed."""
    if hours and hours not in CHOICES_HOURS:
        raise ValueError(f"hours must be one of {CHOICES_HOURS} or 0")
    now = time.time() if now is None else now
    with _LOCK:
        _STATE.update(since=now, until=now + hours * 3600 if hours else None)
    log.info("indexing paused %s", f"for {hours} h" if hours else "until resumed")
    return state(now)


def resume() -> dict:
    with _LOCK:
        was = _STATE["since"] is not None
        _STATE.update(since=None, until=None)
    if was:
        log.info("indexing resumed")
    return state()
