"""Pause background indexing for a while (tray or Settings).

Saved in the data folder, so a pause survives a restart like the backups pause does. While
paused, scheduled kopia index runs, the daytime dashboard rebuild, the index update after deletes
and the Backblaze reindex all wait; anything the user starts by hand still runs. A run that was
due during the pause starts at the next tick after it ends (the scheduler's normal catch-up).
"""
from __future__ import annotations

import os
import threading
import time

from config import DATA_DIR

from . import filecrypto
from .applog import get_logger

log = get_logger("pause")

STATE_FILE = os.path.join(DATA_DIR, "index_pause.json")
MAX_HOURS = 24 * 366
_LOCK = threading.Lock()
_STATE: dict = {"since": None, "until": None}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    data = filecrypto.read_json(STATE_FILE, default={}) or {}
    if isinstance(data, dict) and data.get("since"):
        _STATE.update(since=data.get("since"), until=data.get("until"))


def _save() -> None:
    try:
        filecrypto.write_json(STATE_FILE, dict(_STATE))
    except Exception as exc:  # noqa: BLE001 - the pause still holds for this run
        log.warning("couldn't save the indexing pause: %s", exc)


def check_hours(hours: float | None) -> float:
    """0/None = until resumed; otherwise any length up to a year (the Custom choice)."""
    h = float(hours or 0)
    if h < 0 or h > MAX_HOURS:
        raise ValueError(f"hours must be between 0 and {MAX_HOURS}")
    return h


def state(now: float | None = None) -> dict:
    """``until`` is None for "until I resume". An expired pause reads as not paused."""
    now = time.time() if now is None else now
    with _LOCK:
        _load()
        since, until = _STATE["since"], _STATE["until"]
        if since is not None and until is not None and now >= until:
            _STATE.update(since=None, until=None)
            _save()
            since = until = None
            log.info("indexing pause ended")
    return {"paused": since is not None, "since": since, "until": until}


def active(now: float | None = None) -> bool:
    return state(now)["paused"]


def pause(hours: float | None, now: float | None = None) -> dict:
    h = check_hours(hours)
    now = time.time() if now is None else now
    with _LOCK:
        _load()
        _STATE.update(since=now, until=now + h * 3600 if h else None)
        _save()
    log.info("indexing paused %s", f"for {h:g} h" if h else "until resumed")
    return state(now)


def resume() -> dict:
    with _LOCK:
        _load()
        was = _STATE["since"] is not None
        _STATE.update(since=None, until=None)
        _save()
    if was:
        log.info("indexing resumed")
    return state()
