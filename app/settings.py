"""Shared settings file support."""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from typing import Any

from config import SETTINGS_FILE


DEFAULT_SETTINGS: dict[str, Any] = {
    "last_repo": "1",
    "recent": "",
    "listing_backend": "subprocess",  # "subprocess" (default, no bg activity) | "server" (persistent kopia server)
    "kopialens": {
        "columns": [],
        "last_panel": "dashboard",
        "window": {},
        "time_format": "absolute",
        "number_style": "system",    # system | en-US | de-DE | fr-FR | en-IN (shown by example)
        "date_style": "system",      # system | dmy (9 Oct 2026) | mdy (Oct 9, 2026) | iso (2026-10-09)
        "clock": "12",               # system | 12 | 24
        # ---- appearance (themes + density) ---------------------------------------------------
        # data-theme/data-density are stamped into the rendered page so it paints correctly
        # before any JS runs. light_theme/dark_theme are the two slots the nav quick switch
        # flips between. Values are validated against the known lists before use.
        "theme": "system",           # daylight|parchment|slate|lilac|ember|midnight|pine|obsidian|lens|graphite|ink|system
        "density": "normal",         # comfortable|normal|compact|dense|custom
        "density_pct": 100,          # density="custom": root text size in percent (80-115)
        "font": "inter",             # inter|geist|plex|system (bundled in frontend/vendor/fonts)
        "sort_arrows": "sorted",     # sorted|all - table header sort arrows
        "light_theme": "daylight",   # remembered light slot for the quick switch
        "dark_theme": "lens",        # remembered dark slot for the quick switch
        "page_size": 500,      # search default page size (may be "Infinite")
        # ---- heavy-pass scheduling -------------------------------------------------------------
        # Everything expensive (index build, snapshot analysis, reclaimable, churn rollup, dashboard
        # rebuild, BZ refresh) runs in a worker SUBPROCESS once a day, so this process never grows.
        "index_mode": "daily",       # "daily" (index_time) | "interval" (index_interval) - both use the worker
        "index_time": "23:00",       # daily mode: local time. A missed run catches up at the next opportunity.
        "index_interval": 86400,     # interval mode only: seconds between heavy passes
        "index_worker_timeout": 14400,  # hard ceiling on one worker run (4h) so a hang can't block tomorrow
        "folder_prewarm_budget": 900,  # max seconds the nightly spends pre-computing folder reclaim
        "log_cleanup_days": 90,      # days of job/dry-run/mount logs to keep; 0 = keep everything
        "rank_count": 10000,         # exact top entries the nightly ranks for the Search columns
        "index_retry_delay": 900,    # after a FAILED pass, wait this long before the one retry
        "index_max_retries": 1,      # retries per scheduled slot; 0 = never retry until the next slot
        "quit_indexing": "finish",   # tray Quit: "finish" leaves a running index worker to complete, "stop" ends it
        "reindex_after_delete_delay": 300,  # quiet seconds after the last delete before the index updates
        "light_interval": 1800,      # cheap in-process warm pass: next-snapshot times, stats, search landing
        "dashboard_mode": "nightly",  # "nightly" (rebuild with the heavy pass) | "interval" (also during the day)
        "dashboard_interval": 14400,  # dashboard_mode="interval" only: seconds between rebuilds
        "reindex_interval": 1800,    # legacy interval, still used for the restart-quiet-period calculation
        "startup_delay": 300,        # quiet period after every process start before any background work
        "dashboard_refresh": 900,    # seconds: how often the open Dashboard view re-fetches the cache (0 = off)
        "churn_days": 30,            # Churn tab: default window (1 / 3 / 7 / 30 / 90 / 180 / 365)
        "churn_depth": 4,            # Churn tab: folder-rollup depth
        "churn_view": "source",      # Churn tab: "source" | "folder" | "ext"
        "group_depth": 3,            # Search "By folder" rollup depth; folder prewarm follows it
        "group_min_depth": 0,        # Search "By folder": hide folders shallower than this (0 = none)
        "delete_top_n": 0,           # folder delete: 0 = everything matched, else only the N biggest by physical
        "churn_default": False,      # show churn column by default
        "tree_mode": "full",       # default tree view mode
        "tree_view": "list",       # Tree tab: "list" (drill in) | "tree" (expand folders in place)
        "search_sort": "size",     # persisted search sort column
        "search_dir": "desc",      # persisted search sort direction
        "search_dedupe": False,    # persisted dedupe toggle (search)
        "search_kind": "files",    # Search lists "files" | "folders" (rolled up by depth) | "both"
        "search_dedupe_by": "path",  # what Dedupe merges: "path" (one row per file path) | "content" (identical bytes)
        "header_search_only": False,  # Search tab uses the header search box instead of its own
        "snap_source_by_repo": {},  # Snapshots tab source per repo id ("*ALL*" = all sources)
        "tree_source_by_repo": {},  # Tree tab source per repo id
        "sim_policy_by_repo": {},   # Simulate's last policy per repo id
        "recent_backup": "30",     # default "Backed up" filter in days ("" = Any); user changes persist
        "tree_dedupe": False,      # persisted dedupe toggle (tree)
        "panel_layout": "classic",  # detail-panel layout (): "classic" | "tabbed"
        "policy_layout": "tabs",    # policy editor: "tabs" (one section at a time) | "scroll" (stacked, jump strip)
        "policy_leave": "ask",      # leaving a policy with unsaved changes: "ask" | "keep" (drafts stay until saved)
        # ---- external tools -----------------------------------------------------------------
        # The kopia program. Empty = auto: the BUNDLED copy first (a release pins the kopia it was
        # tested with), then PATH, the usual install locations and KopiaUI's bundle - see
        # core/kopiabin.py, the ONLY place that resolves it. KOPIALENS_KOPIA overrides it.
        "kopia_path": "",
        # Where a connection KopiaLens CREATES keeps its kopia config file:
        #   "" = profile default (tray/desktop = "kopia", server/Docker = "data")
        #   "kopia" = kopia's own config folder, so KopiaUI and the kopia CLI see it too
        #   "data"  = KopiaLens's data folder (a container's data folder is the mounted volume)
        # KOPIALENS_KOPIA_CONFIG_LOCATION overrides it. See core/repoconnect.config_location().
        "kopia_config_location": "",
        "es_path": "",               # Everything CLI for the Excluded browser; "" = find it (core/everything.es_path)
        "disk_lookup": "auto",       # what answers disk questions: auto | everything | scan | off
        "disk_scan_budget": 15,      # seconds one folder scan may take before it answers "not measured"
        "folder_picker": "auto",     # Browse...: auto | native (Windows dialog) | browser (in-app list); see core/host.py
        "mount_dir": "",             # Linux/macOS snapshot mounts go under this folder; "" = <data>/mounts (env KOPIALENS_MOUNT_DIR wins)
        "mount_host_path": "",       # Docker: the host folder mapped to mount_dir, shown instead of the container path
        "excluded_tolerance": 0.05,  # folder counts as "backed" when excluded fraction is below this (cuts partial noise)
        "repo_labels": "short",      # repo chips/columns: "short" (Repo 1) | "full" (the name from repos.json)
        "show_pro_badge": True,      # the small Pro mark next to the logo while a licence is active
        "update_check": True,        # daily check for a newer version (docs/usage-stats.md)
        "usage_stats": True,         # anonymous usage stats sent with that check
        "debug_log": False,          # also write data/logs/kopialens-debug.log at DEBUG (env KOPIALENS_DEBUG_LOG overrides)
        "control_api": True,         # the tray's local control port (restart/quit/status for scripts); read at tray start
        "tray_click": "window",      # desktop app tray icon click: window | menu (right-click always shows the menu)
        "tray_double_click": "window",  # tray icon double-click (Windows): window | none
        "notifications": "warnings",  # desktop app toasts: all | warnings | off
        # ---- unified store () ---------------------------------------------------------
        # Encryption at rest (). Three levels, decided in onboarding ():
        #   "off"        nothing
        #   "index"      the index generations + side caches + policy caches (the full map) -
        #                kopia's own posture: its repository and cache encrypted, config and logs not
        #   "full"       the above + the small JSON files, audit/history, logs, policy backups
        #                and the Backblaze store - stricter than kopia
        # This is the INTENT: new files are written in this state, while anything already on disk
        # keeps its own state (a generation records it in its manifest, a file in its own bytes)
        # until the switch rewrites it. Never touches the kopia repository.
        "encryption_level": "off",
        # The old boolean, read as index-level when on. Kept so an install upgrading from part 1
        # keeps meaning something until `filecrypto.upgrade_step` folds it into the level.
        "encryption": False,
        # Seconds without a USER request before every open generation is closed and its memory
        # returned. 0 disables. Polled status endpoints deliberately do not count as activity.
        "store_idle_close": 1800,
        # DuckDB limits. Empty/0 auto-detects from total RAM (see index/store.resolve_limits);
        # these are the Advanced overrides. Threads are never left at the DuckDB default - a high
        # count inflates hash-aggregate memory enough to OOM the small tier.
        "duckdb_memory": "",
        "duckdb_threads": 0,
        # ---- access control ---------------------------------------------------------
        # Sign-in guards once an account exists (accounts live in users.json, core/users.py) and
        # auth_enabled is on; switching off keeps the account. The bind default is PER INSTALL
        # PROFILE (tray = desktop = loopback; anything else = server = all interfaces) - resolved
        # in core/auth.py, which also honours KOPIALENS_BIND_HOST / _ALLOWED_HOSTS / _PASSWORD /
        # _USERNAME / _AUTH_ENABLED env overrides.
        "auth_enabled": True,
        "session_days": 30,            # "stay signed in for": one of auth.SESSION_CHOICES
        "bind_host": "127.0.0.1",      # desktop-profile default; a server install resolves 0.0.0.0
        "allowed_hosts": [],           # extra Host header names (a reverse proxy's domain).
                                       # localhost and bare IP addresses are always accepted.
        # ---- misc ceilings ------------------------------------------------------------------
        "kopia_timeout": 3600,         # seconds; a hung kopia call fails instead of hanging forever
        "remove_plan_cap": 200000,     # max files one chunked-delete plan may enumerate
    },
    "backblaze": {
        "bzdata_path": r"C:\ProgramData\Backblaze\bzdata",  # read-only source root (never written)
        # where the `files` index lives: "parquet" (data/bz/ per-drive Parquet generations
        # read via DuckDB - the DEFAULT) or "sqlite" (kopialens_bz.sqlite + FTS5, the LEGACY path
        # kept for rollback). Switching stores triggers a full rebuild of the missing store on the
        # next refresh; the small history/state tables always stay in the SQLite file.
        "store": "parquet",
        "history_days": 90,          # how many days of the daily per-folder rollup to retain (growth/churn trend)
        # minimum seconds between real BZ reindexes (): the cheap detect runs on the light
        # interval, but a change it finds waits for this gap before the expensive reindex starts
        "bz_reindex_min_gap": 21600,
        # folder-rollup views (Queue/Churn/by-folder) roll these prefixes up to a single row instead
        # of splitting their (chunk-named) subfolders. Toggle per-request from the UI.
        "collapse_prefixes": [],
        # copied (readable) rotated eventlog folder for the one-time session-history import
        "eventlog_archive_path": "",
    },
}


# All settings reads/writes go through this lock so a read-modify-write (e.g. the many Tabulator
# table-persistence POSTs fired across several browser tabs at once) can never interleave - the
# bug that silently reset settings.json to defaults.
_SETTINGS_LOCK = threading.RLock()


def _read_settings_file(strict: bool = False) -> dict[str, Any]:
    """The file exactly as written - defaults do NOT fill absent keys. That distinction is the
    only way to tell an explicit user choice from an absent one, which both the profile-aware
    bind default and the save path need.

    A damaged file is moved aside and the newest good backup put back (core/configbackup). A file
    that cannot be OPENED (briefly held by another program) is not damaged: reads fall back to
    defaults, and ``strict`` (the save path) raises instead, so a save never writes a near-empty
    file over settings it could not read."""
    from .core import configbackup
    with _SETTINGS_LOCK:
        for attempt in range(2):
            if not os.path.exists(SETTINGS_FILE):
                return {}
            for _ in range(3):
                try:
                    with open(SETTINGS_FILE, encoding="utf-8") as handle:
                        raw = handle.read()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                if strict:
                    raise OSError(f"settings file could not be read: {SETTINGS_FILE}")
                return {}
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    return loaded
            except ValueError:
                pass
            if attempt == 0 and not configbackup.recover("settings.json"):
                return {}
        return {}


def raw_kopialens_settings() -> dict[str, Any]:
    """The kopialens dict exactly as the FILE carries it - no DEFAULTS filling absent keys."""
    return _read_settings_file().get("kopialens") or {}


def load_settings() -> dict[str, Any]:
    with _SETTINGS_LOCK:
        settings = deepcopy(DEFAULT_SETTINGS)
        loaded = _read_settings_file()
        if not loaded:
            return settings
        settings.update(loaded)
        kl = loaded.get("kopialens") if isinstance(loaded.get("kopialens"), dict) else {}
        bz = loaded.get("backblaze") if isinstance(loaded.get("backblaze"), dict) else {}
        settings["kopialens"] = {**DEFAULT_SETTINGS["kopialens"], **kl}
        settings["backblaze"] = {**DEFAULT_SETTINGS["backblaze"], **bz}
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
    from .core import configbackup
    configbackup.remember("settings.json")


def mutate_settings(mutator) -> dict[str, Any]:
    """Atomic read-modify-write: hold the lock across read + mutate + save so concurrent writers
    cannot interleave and clobber each other's changes. `mutator(settings)` edits in place.

    The write is based on the FILE's own content, never the defaults-merged view: persisting
    merged defaults is what stamped values the user never chose - a server install that saved
    anything wrote the desktop loopback bind into settings.json and lost its address on restart."""
    from .core import configbackup
    from .core.filelock import locked
    with _SETTINGS_LOCK, locked(SETTINGS_FILE):
        settings = _read_settings_file(strict=True)
        configbackup.before_write()
        mutator(settings)
        save_settings(settings)
        return load_settings()
