"""Machine-written UI state (table columns, widths, sort) in ``ui_state.json``.

Kept out of settings.json: it is written on every column drag, which would bury real settings
changes in backups and churn the settings file. Plain at every encryption level, like settings.
"""
from __future__ import annotations

import json
import os
import threading

from config import CONFIG_DIR

from ..settings import raw_kopialens_settings
from .applog import get_logger
from .filelock import locked

log = get_logger("uistate")

UI_STATE_FILE = os.path.join(CONFIG_DIR, "ui_state.json")
_LOCK = threading.RLock()


def _path() -> str:
    return UI_STATE_FILE


def _read() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ui_state.json unreadable (%s); table layouts start fresh", exc)
        return {}


def _write(data: dict) -> None:
    from .dataupgrade import atomic_json
    os.makedirs(os.path.dirname(_path()) or ".", exist_ok=True)
    atomic_json(_path(), data)


def tables() -> dict:
    """Every table's saved layout. Layouts still in settings.json (before the data upgrade moves
    them) are read too; this file wins."""
    with _LOCK:
        legacy = raw_kopialens_settings().get("tables")
        return {**(legacy if isinstance(legacy, dict) else {}), **(_read().get("tables") or {})}


def set_table(table_id: str, config) -> None:
    with _LOCK, locked(_path()):
        data = _read()
        data.setdefault("tables", {})[table_id] = config
        _write(data)


def merge_tables(incoming: dict) -> int:
    """Add layouts that this file does not have yet (the data upgrade). Returns how many."""
    with _LOCK, locked(_path()):
        data = _read()
        current = data.setdefault("tables", {})
        added = 0
        for key, value in (incoming or {}).items():
            if key not in current:
                current[key] = value
                added += 1
        _write(data)
        return added
