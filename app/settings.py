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
        # ---- heavy-pass scheduling -------------------------------------------------------------
        # Everything expensive (index build, snapshot analysis, reclaimable, churn rollup, dashboard
        # rebuild, BZ refresh) runs in a worker SUBPROCESS once a day, so this process never grows.
        "index_mode": "daily",       # "daily" (index_time) | "interval" (index_interval) - both use the worker
        "index_time": "23:00",       # daily mode: local time. A missed run catches up at the next opportunity.
        "index_interval": 86400,     # interval mode only: seconds between heavy passes
        "index_worker_timeout": 14400,  # hard ceiling on one worker run (4h) so a hang can't block tomorrow
        "index_drain_timeout": 300,  # how long the handover waits for in-flight queries to finish
        "folder_prewarm_budget": 900,  # max seconds the nightly spends pre-computing folder reclaim
        "index_retry_delay": 900,    # after a FAILED pass, wait this long before the one retry
        "index_max_retries": 1,      # retries per scheduled slot; 0 = never retry until the next slot
        "light_interval": 1800,      # cheap in-process warm pass: next-snapshot times, stats, search landing
        "dashboard_mode": "nightly",  # "nightly" (rebuild with the heavy pass) | "interval" (also during the day)
        "dashboard_interval": 14400,  # dashboard_mode="interval" only: seconds between rebuilds
        "reindex_interval": 1800,    # legacy interval, still used for the restart-quiet-period calculation
        "reclaim_interval": 10800,   # seconds between background reclaimable pre-warm passes (heavy: refcount + double walk)
        "startup_delay": 300,        # quiet period after every process start before any background work
        "dashboard_refresh": 900,    # seconds: how often the open Dashboard view re-fetches the cache (0 = off)
        "churn_days": 30,            # Churn tab: default window (1 / 3 / 7 / 30 / 90 / 180 / 365)
        "churn_depth": 4,            # Churn tab: folder-rollup depth
        "churn_view": "source",      # Churn tab: "source" | "folder" | "ext"
        "group_depth": 3,            # Search "By folder" rollup depth; folder prewarm follows it
        "delete_top_n": 0,           # folder delete: 0 = everything matched, else only the N biggest by physical
        "churn_default": False,      # show churn column by default
        "tree_mode": "full",       # default tree view mode
        "search_sort": "size",     # persisted search sort column
        "search_dir": "desc",      # persisted search sort direction
        "search_dedupe": False,    # persisted dedupe toggle (search)
        "recent_backup": "30",     # default "Backed up" filter in days ("" = Any); user changes persist
        "tree_dedupe": False,      # persisted dedupe toggle (tree)
        "panel_layout": "classic",  # detail-panel layout (): "classic" | "tabbed"
        "es_path": r"G:\Apps\CLI Tools\es.exe",  # Everything CLI for the Excluded browser (local disk side)
        "excluded_tolerance": 0.05,  # folder counts as "backed" when excluded fraction is below this (cuts partial noise)
        "auto_compact": True,        # auto-compact the DuckDB when it bloats (DuckDB has no GC of its own)
        "compact_ratio": 1.8,        # compact when the file grows past this x its last-compacted size
        "compact_min_interval": 86400,  # min seconds between auto-compactions (1 day)
        "refcount_write_interval": 21600,  # min seconds between content_refs rewrites - a large churn
                                           # on the DuckDB file, so deliberately infrequent (see metrics.py)
        "dir_memo_max_mb": 1500,     # RAM budget for the in-process directory-listing LRU (byte-budgeted, not item-count)
        # Walk-cache storage format for NEWLY written rows: "none" | "zstd-1" | "zstd-3". Both
        # formats are readable side by side, so changing this is safe at any time; converting the
        # rows already on disk is a separate one-off (`python compress_cache.py`).
        # zstd-1 measured on real rows (2026-09-05): 24.0% of original at 1.53x read cost, against
        # zstd-3's 24.2% at 1.76x - level 3 is both bigger and slower here (small independent blobs),
        # so it is offered but never the default.
        "walk_cache_compression": "zstd-1",
        # ---- access control ---------------------------------------------------------
        # Auth is on BECAUSE a password exists, not the other way round: enabled() requires a
        # hash, so a fresh install (no password) is never locked out. The bind default is PER
        # INSTALL PROFILE (tray = desktop = loopback; anything else = server = all interfaces) -
        # resolved in core/auth.py, which also honours KOPIALENS_BIND_HOST / _ALLOWED_HOSTS /
        # _PASSWORD env overrides (an auth OFF switch does not exist yet - see). Choosing
        # a password belongs to onboarding (); Settings carries the control until then.
        "auth_enabled": True,
        "auth_password_hash": "",      # pbkdf2_sha256$iters$salt$digest - never the password
        "bind_host": "127.0.0.1",      # desktop-profile default; a server install resolves 0.0.0.0
        "allowed_hosts": [],           # extra Host header names (a reverse proxy's domain).
                                       # localhost and bare IP addresses are always accepted.
        # ---- misc ceilings ------------------------------------------------------------------
        "kopia_timeout": 3600,         # seconds; a hung kopia call fails instead of hanging forever
        "remove_plan_cap": 200000,     # max files one chunked-delete plan may enumerate
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


def raw_kopialens_settings() -> dict[str, Any]:
    """The kopialens dict exactly as the FILE carries it - no DEFAULTS filling absent keys. The
    only way to tell an explicit user choice from an absent one, which the profile-aware bind
    default needs. Empty dict on a missing or unreadable file."""
    with _SETTINGS_LOCK:
        try:
            if not os.path.exists(SETTINGS_FILE):
                return {}
            with open(SETTINGS_FILE, encoding="utf-8") as handle:
                loaded = json.load(handle)
            return loaded.get("kopialens", {}) or {}
        except Exception:
            return {}


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
