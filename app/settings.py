"""Shared settings file support."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any

from config import KOPIA_DIR, SETTINGS_FILE

_OLD_SETTINGS_FILE = os.path.join(KOPIA_DIR, "kopiatoolkit-settings.json")  # legacy shared-with-CLI file

DEFAULT_SETTINGS: dict[str, Any] = {
    "last_repo": "1",
    "laptop_mode": "all",
    "verbose": False,
    "recent": "",
    "listing_backend": "subprocess",  # "subprocess" (default, no bg activity) | "server" (persistent kopia server)
    "kopialens": {
        "columns": [],
        "last_panel": "dashboard",
        "window": {},
        "time_format": "absolute",
        "tables": {},          # per-table Tabulator persistence (columns + sort), keyed by table id
        "page_size": 500,      # search default page size (may be "Infinite")
        "reindex_interval": 1800,    # seconds between background incremental reindex + dashboard-recompute passes
        "reclaim_interval": 10800,   # seconds between background reclaimable pre-warm passes (heavy: refcount + double walk)
        "dashboard_refresh": 900,    # seconds: how often the open Dashboard view re-fetches the cache (0 = off)
        "churn_default": False,      # show churn column by default
        "tree_mode": "full",       # default tree view mode
        "search_sort": "size",     # persisted search sort column
        "search_dir": "desc",      # persisted search sort direction
        "search_dedupe": False,    # persisted dedupe toggle (search)
        "tree_dedupe": False,      # persisted dedupe toggle (tree)
        "es_path": r"G:\Apps\CLI Tools\es.exe",  # Everything CLI for the Excluded browser (local disk side)
        "excluded_tolerance": 0.05,  # folder counts as "backed" when excluded fraction is below this (cuts partial noise)
        "auto_compact": True,        # auto-compact the DuckDB when it bloats (DuckDB has no GC of its own)
        "compact_ratio": 1.8,        # compact when the file grows past this x its last-compacted size
        "compact_min_interval": 86400,  # min seconds between auto-compactions (1 day)
    },
    "backblaze": {
        "bzdata_path": r"C:\ProgramData\Backblaze\bzdata",  # read-only source root (never written)
        "history_days": 90,          # how many days of the daily per-folder rollup to retain (growth/churn trend)
        # folder-rollup views (Queue/Churn/by-folder) roll these prefixes up to a single row instead
        # of splitting their (chunk-named) subfolders. Toggle per-request from the UI.
        "collapse_prefixes": [r"E:\Kopia", r"I:\Kopia Big Backup"],
        # copied (readable) rotated eventlog folder for the one-time session-history import
        "eventlog_archive_path": r"H:\Excluded\BackblazeData\bzlogs\bzreports_eventlog",
    },
}


def _migrate_settings() -> None:
    """One-time: seed the new KopiaLens-local settings file from the old shared CLI file."""
    if os.path.exists(SETTINGS_FILE) or not os.path.exists(_OLD_SETTINGS_FILE):
        return
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(_OLD_SETTINGS_FILE, encoding="utf-8") as src:
            data = src.read()
        with open(SETTINGS_FILE, "w", encoding="utf-8") as dst:
            dst.write(data)
    except Exception:
        pass


_migrate_settings()


# All settings reads/writes go through this lock so a read-modify-write (e.g. the many Tabulator
# table-persistence POSTs fired across several browser tabs at once) can never interleave - the
# bug that silently reset settings.json to defaults.
_SETTINGS_LOCK = threading.RLock()


def load_settings() -> dict[str, Any]:
    with _SETTINGS_LOCK:
        settings = deepcopy(DEFAULT_SETTINGS)
        if not os.path.exists(SETTINGS_FILE):
            return settings
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except Exception:
            # File exists but is unreadable/corrupt. Do NOT silently return defaults that a
            # following save could persist over the real file (that wipes the user's settings).
            # Preserve the bad copy for recovery, then fall back to defaults.
            try:
                os.replace(SETTINGS_FILE, SETTINGS_FILE + ".corrupt")
            except Exception:
                pass
            return settings
        settings.update(loaded)
        settings["kopialens"] = {**DEFAULT_SETTINGS["kopialens"], **loaded.get("kopialens", {})}
        settings["backblaze"] = {**DEFAULT_SETTINGS["backblaze"], **loaded.get("backblaze", {})}
        return settings


def save_settings(settings: dict[str, Any]) -> None:
    """Atomic write under the lock: write a temp file, fsync, then os.replace() (atomic on Windows
    + POSIX). A concurrent reader can never catch the file mid-truncate, so load_settings can't fall
    back to defaults from a partial read."""
    with _SETTINGS_LOCK:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, SETTINGS_FILE)


def mutate_settings(mutator) -> dict[str, Any]:
    """Atomic read-modify-write: hold the lock across load + mutate + save so concurrent writers
    cannot interleave and clobber each other's changes. `mutator(settings)` edits in place."""
    with _SETTINGS_LOCK:
        settings = load_settings()
        mutator(settings)
        save_settings(settings)
        return settings
