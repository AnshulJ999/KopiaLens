"""Shared settings file support."""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from typing import Any

from appversion import prerelease
from config import APP_VERSION, COMPILED, SETTINGS_FILE, TESTING_BUILD
from .core import atomicfile


DEFAULT_SETTINGS: dict[str, Any] = {
    "last_repo": "1",
    "recent": "",
    "listing_backend": "subprocess",  # "subprocess" (default, no bg activity) | "server" (persistent kopia server)
    "kopialens": {
        "columns": [],
        "last_panel": "dashboard",
        "window": {},
        "time_format": "auto",
        "number_style": "system",    # system | en-US | de-DE | fr-FR | en-IN (shown by example)
        "byte_units": "decimal",     # decimal (GB, kopia's default) | binary (GiB)
        "date_style": "system",      # system | dmy (9 Oct 2026) | mdy (Oct 9, 2026) | iso (2026-10-09)
        "clock": "system",           # system (this computer's own clock) | 12 | 24
        # ---- appearance (themes + density) ---------------------------------------------------
        # data-theme/data-density are stamped into the rendered page so it paints correctly
        # before any JS runs. light_theme/dark_theme are the two slots the nav quick switch
        # flips between. Values are validated against the known lists before use.
        "theme": "system",           # daylight|parchment|slate|lilac|kopia|sky|teal|ember|midnight|obsidian|lens|graphite|ink|system
        "density": "normal",         # comfortable|normal|compact|dense|custom
        "density_pct": 100,          # density="custom": root text size in percent (80-115)
        "font": "inter",             # inter|geist|plex|system (bundled in frontend/vendor/fonts)
        "sort_arrows": "sorted",     # sorted|all - table header sort arrows
        "file_icons": "material",    # material|filled|outline|fluent|emoji - icons before file and folder names
        "table_align": "auto",       # auto|left|center|right - every table's starting alignment (a table or column can override)
        "dashboard_page_size": "all",   # all|25|50|100 - Dashboard rows per page
        "tab_order": [],             # tab ids in the order the user dragged them; [] = the default order
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
        "index_first_timeout": 86400,   # the same ceiling for a repo's first index (24h: big cloud repos)
        "folder_prewarm_budget": 900,  # max seconds the nightly spends pre-computing folder reclaim
        "log_cleanup_days": 90,      # days of job/dry-run/mount logs to keep; 0 = keep everything
        "task_history_days": 90,     # days of backup/maintenance task history (Tasks tab); 0 = keep everything
        "task_routine_seconds": 1,   # a task that finished without errors faster than this is routine
        "task_routine_hours": 48,    # how long routine tasks stay in the history
        "task_hide_routine": True,   # Tasks tab hides routine tasks
        "audit_hide_routine": True,  # Logs > Audit hides snapshots you started, previews and checks
        "audit_groups": ["changes"],  # Logs > Audit filter chips that are on (none = everything)
        "engine_takeback": "auto",
        "notify_webhook": {},        # KopiaLens's own notifications: {url, format text|json, headers [..], level info|warn|error}   # when another app stops running a repo's backups: auto = take over again | wait = stay paused
        "rank_count": 10000,         # exact top entries the nightly ranks for the Search columns
        "index_retry_delay": 900,    # after a FAILED pass, wait this long before the one retry
        "index_max_retries": 1,      # retries per scheduled slot; 0 = never retry until the next slot
        "quit_indexing": "finish",   # tray Quit: "finish" leaves a running index worker to complete, "stop" ends it
        "reindex_after_delete_delay": 300,  # quiet seconds after the last delete before the index updates
        "light_interval": 3600,      # light pass: next-snapshot times and the Search start page
        "light_mode": "in_use",      # when the light pass may run kopia: in_use (someone used KopiaLens within the interval) | always | off
        "dashboard_mode": "nightly",  # "nightly" (rebuild with the heavy pass) | "interval" (also during the day)
        "dashboard_interval": 14400,  # dashboard_mode="interval" only: seconds between rebuilds
        "startup_delay": 300,        # quiet period after every process start before any background work
        "dashboard_refresh": 1800,   # seconds: how often the open Dashboard view re-fetches the cache (0 = off)
        "churn_days": 30,            # Churn tab: default window (1 / 3 / 7 / 30 / 90 / 180 / 365)
        "churn_depth": 4,            # Churn tab: folder-rollup depth
        "churn_view": "folder",      # Churn tab: "folder" | "source" | "ext"
        "group_depth": 6,            # Search "By folder" rollup depth; folder prewarm follows it
        "group_min_depth": 3,        # Search "By folder": hide folders shallower than this (0 = none)
        "group_loose": True,         # Search folders: a row for the files lying directly in a shallower folder
        "group_depth_mode": "deepest",  # Search folders: deepest | nested | own (queries.DEPTH_MODES)
        "delete_top_n": 0,           # folder delete: 0 = everything matched, else only the N biggest by physical
        "churn_default": False,      # show churn column by default
        "tree_mode": "full",       # default tree view mode
        "tree_view": "list",       # Tree tab: "list" (drill in) | "tree" (expand folders in place)
        "search_sort": "stored",   # persisted search sort column (Physical (all))
        "search_dir": "desc",      # persisted search sort direction
        "search_dedupe": False,    # persisted dedupe toggle (search)
        "search_kind": "files",    # Search lists "files" | "folders" (rolled up by depth) | "both"
        "search_dedupe_by": "path",  # what Dedupe merges: "path" (one row per file path) | "content" (identical bytes)
        "header_search_only": False,  # Search tab uses the header search box instead of its own
        "snap_source_by_repo": {},  # Snapshots tab source per repo id ("*ALL*" = all sources)
        "tree_source_by_repo": {},  # Tree tab source per repo id
        "owner_filter_by_repo": {},  # Dashboard + Snapshots owner filter per repo id: "all" | "me" | "user@host"
        "sim_policy_by_repo": {},   # Simulate's last policy per repo id
        "recent_backup": "",       # default "Backed up" filter in days ("" = Any); user changes persist
        "tree_dedupe": False,      # persisted dedupe toggle (tree)
        "docs_view": "popup",       # the docs browser: "popup" | "side"
        "panel_layout": "tabbed",  # detail-panel layout: "classic" | "tabbed"
        "hide_overdue": False,      # Dashboard: no "overdue" badge on Next snapshot
        "policy_layout": "tabs",    # policy editor: "tabs" (one section at a time) | "scroll" (stacked, jump strip)
        "policy_leave": "ask",      # leaving a policy with unsaved changes: "ask" | "keep" (drafts stay until saved)
        # ---- external tools -----------------------------------------------------------------
        # The kopia program. Empty = auto: the BUNDLED copy first (a release pins the kopia it was
        # tested with), then PATH, the usual install locations and KopiaUI's bundle - see
        # core/kopiabin.py, the ONLY place that resolves it. KOPIALENS_KOPIA overrides it.
        "kopia_path": "",
        # The default place for a connection KopiaLens CREATES (the connect form can pick per repo):
        #   "" / "data" = KopiaLens's data folder, used only by this copy
        #   "kopia" = kopia's own config folder, so KopiaUI and the kopia CLI see it too
        # KOPIALENS_KOPIA_CONFIG_LOCATION overrides it. See core/repoconnect.config_location().
        "kopia_config_location": "",
        "es_path": "",               # Everything CLI for the Excluded browser; "" = find it (core/everything.es_path)
        # What answers disk questions: everything | auto (Everything, else the scanner) | scan | off.
        # The KopiaLens scanner reads the disk, so it is opt-in (auto or scan).
        "disk_lookup": "everything",
        "disk_scan_budget": 15,      # seconds a folder count may take while a screen waits
        "disk_scan_threads": 0,      # scanner threads; 0 = automatic by disk type (SSD up to 8, hard disk and network 2)
        "disk_scan_background": False,  # a folder that ran out of time keeps counting after you leave it
        "disk_count_drives": False,  # the Excluded tab counts each drive and place's total size by itself
        "disk_ignore_search": False,  # the nightly index looks for ignore files on disk with the scanner
        "disk_follow_links": False,  # the scanner enters linked folders (kopia stores the link, not its target)
        "disk_sizes_minutes": 0,     # how long a counted size is used; 0 = until Recount
        "disk_show_local": True,     # the Excluded tab lists this computer's own drives (off for a server that only checks shares)
        "disk_paths": [],            # extra places to scan: [{path, backup_path, computer}] (core/disklookup.places)
        "folder_picker": "auto",     # Browse... in a browser: auto (= in-app list) | native (Windows dialog, this PC only) | browser; see core/host.py
        "mount_dir": "",             # Linux/macOS snapshot mounts go under this folder; "" = <data>/mounts (env KOPIALENS_MOUNT_DIR wins)
        "mount_host_path": "",       # Docker: the host folder mapped to mount_dir, shown instead of the container path
        "excluded_tolerance": 0.05,  # folder counts as "backed" when excluded fraction is below this (cuts partial noise)
        "repo_labels": "short",      # repo chips/columns: "short" (Repo 1) | "full" (the name from repos.json)
        "index_wait_unreachable": 1800,  # seconds an unreachable repository is waited for before the run gives up (progress kept)
        "index_stall_timeout": 3600,     # seconds the tree read may make no progress before it is stopped; 0 = off
        "index_keep_versions": 1,        # older index versions kept on disk (0-3); 0 = off
        "index_backup_dir": "",          # a folder (ideally another drive) holding a copy of each index; "" = off
        "show_pro_badge": True,      # the small Pro mark next to the logo while a licence is active
        "logo": "k3g",               # header logo: k3g | k6s | k3a | tile | none
        "tray_icon": "k3g",          # desktop app tray icon (api/shell.PREFS lists the choices)
        "ask_snapshot_now": True,    # confirm before Snapshot now
        "task_view": "popup",        # Tasks: details as a full-size popup or a small panel
        "retention_chips": "kopiaui",  # Snapshots retention chips: kopiaui | uniform | custom
        "retention_chip_color": "#2563eb",  # the custom chip colour
        "update_check": True,        # daily check for a newer version (docs/usage-stats.md)
        "usage_stats": True,         # anonymous usage stats sent with that check
        # stable | beta (beta also offers beta versions); a beta build starts on beta
        "update_channel": "beta" if prerelease(APP_VERSION) else "stable",
        "debug_log": False,          # also write data/logs/kopialens-debug.log at DEBUG (env KOPIALENS_DEBUG_LOG overrides)
        # Settings > Experimental (shown when "experimental" is on); a testing build starts with both on.
        "experimental": TESTING_BUILD,
        "exp_notes": TESTING_BUILD,  # the Notes widget (app/core/devnotes.py); notes kept in <data>/notes
        # The desktop app's local control port (restart/quit/status for scripts and agents), read
        # at its start: on in a source checkout, off in a released copy until turned on.
        "control_api": not COMPILED,
        "port": 7123,                # the app's port, read at start by config.py and the desktop app (env KOPIALENS_PORT wins)
        "control_port": 7124,        # the tray's control port (env KOPIALENS_TRAY_PORT wins)
        "tray_click": "window",      # desktop app tray icon click: window | menu (right-click always shows the menu)
        "tray_double_click": "window",  # tray icon double-click (Windows): window | none
        "desktop_signin": False,     # the desktop app's window opens signed in; browsers still sign in
        "notifications": "warnings",  # desktop app toasts: all | warnings | off
        # ---- unified store ---------------------------------------------------------
        # Encryption at rest. Three levels, decided in onboarding:
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
        "base_path": "",               # sub-path behind a reverse proxy ("/kopialens"); file-only
                                       # (env KOPIALENS_BASE_PATH), core/basepath.py
        # ---- misc ceilings ------------------------------------------------------------------
        "kopia_timeout": 3600,         # seconds; a hung kopia call fails instead of hanging forever
        "remove_plan_cap": 200000,     # max files one chunked-delete plan may enumerate
    },
    "backblaze": {
        "bzdata_path": "",           # read-only source root (never written); "" = this OS's default
        "computer": "auto",          # whose Backblaze it is: auto (compare names) | this | other (no Kopia cross-check)
        "updates": "auto",          # auto = refresh when Backblaze's file lists change | off = only when started by hand
        # Where the `files` index lives: "parquet" (data/bz/ per-drive Parquet generations
        # read via DuckDB - the DEFAULT) or "sqlite" (kopialens_bz.sqlite + FTS5, the LEGACY path
        # kept for rollback). Switching stores triggers a full rebuild of the missing store on the
        # next refresh; the small history/state tables always stay in the SQLite file.
        "store": "parquet",
        "history_days": 90,          # how many days of the daily per-folder rollup to retain (growth/churn trend)
        # minimum seconds between real BZ reindexes: the cheap detect runs on the light
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

# kopialens settings with their own route and checks: the general settings save refuses them.
OWN_ROUTE_SETTINGS = {
    "auth_enabled": "Settings > Account",
    **{k: "Settings > Remote access" for k in ("access", "access_addresses", "allowed_hosts", "https",
                                                "https_cert", "https_key", "trusted_proxies")},
    "bind_host": "settings.json",
    "base_path": "settings.json",
    "encryption": "Settings > Encryption",
    "encryption_level": "Settings > Encryption",
    "kopia_path": "Settings > Repositories",
    "write_mode": "Settings > Permissions",
}

# A saved value equal to its default is not kept, so a changed default reaches every install that
# never chose otherwise. These keep an explicit value: they have their own route, code reads the
# file's own value on purpose, or their default depends on the build.
KEEP_EXPLICIT = frozenset(OWN_ROUTE_SETTINGS) | {
    "auth_enabled", "bind_host", "base_path", "encryption", "encryption_level", "kopia_path",
    "experimental", "exp_notes", "control_api", "update_channel"}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def drop_defaults(settings: dict[str, Any], keys: set[tuple[str, str]] | None = None) -> list[str]:
    """Remove saved values equal to their default, in place. ``keys`` limits it to (block, key)
    pairs ("" = the top level); None checks every key. Returns the keys removed."""
    removed: list[str] = []
    for block, defaults in (("", DEFAULT_SETTINGS), ("kopialens", DEFAULT_SETTINGS["kopialens"]),
                            ("backblaze", DEFAULT_SETTINGS["backblaze"])):
        target = settings.get(block) if block else settings
        if not isinstance(target, dict):
            continue
        for key in list(target):
            if keys is not None and (block, key) not in keys:
                continue
            if key in KEEP_EXPLICIT or key not in defaults or (not block and isinstance(defaults[key], dict)):
                continue
            if _same(target[key], defaults[key]):
                del target[key]
                removed.append(f"{block}.{key}" if block else key)
    return removed


# Readers use the last parsed file while its stat is unchanged, so they never wait on a save that
# is retrying a held file. A write from any process changes the stat.
_CACHE: tuple[tuple, dict[str, Any]] | None = None
_READ_LOCK = threading.RLock()


def _file_key() -> tuple:
    st = os.stat(SETTINGS_FILE)
    return (SETTINGS_FILE, st.st_ino, st.st_mtime_ns, st.st_size)


def _read_disk(strict: bool) -> tuple[dict[str, Any] | None, tuple | None]:
    """(parsed file, its stat key). None = the file exists but could not be opened."""
    from .core import configbackup
    for attempt in range(2):
        try:
            key = _file_key()
        except FileNotFoundError:
            return {}, None
        except OSError:
            key = None
        for _ in range(3):
            try:
                with open(SETTINGS_FILE, encoding="utf-8") as handle:
                    raw = handle.read()
                break
            except FileNotFoundError:
                return {}, None
            except OSError:
                time.sleep(0.1)
        else:
            if strict:
                raise OSError(f"settings file could not be read: {SETTINGS_FILE}")
            return None, None
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                return loaded, key
        except ValueError:
            pass
        if attempt == 0 and not configbackup.recover("settings.json"):
            return {}, None
    return {}, None


def _read_settings_file(strict: bool = False) -> dict[str, Any]:
    """The file exactly as written - defaults do NOT fill absent keys. That distinction is the
    only way to tell an explicit user choice from an absent one, which both the profile-aware
    bind default and the save path need.

    A damaged file is moved aside and the newest good backup put back (core/configbackup). A file
    that cannot be OPENED (briefly held by another program) is not damaged: reads keep the last
    copy read (defaults when there is none), and ``strict`` (the save path) always reads the file
    and raises instead, so a save never writes a near-empty file over settings it could not read."""
    global _CACHE
    if strict:
        with _SETTINGS_LOCK:
            return _read_disk(True)[0] or {}
    try:
        key = _file_key()
    except FileNotFoundError:
        return {}
    except OSError:
        key = None
    cached = _CACHE
    if key is not None and cached is not None and cached[0] == key:
        return deepcopy(cached[1])
    with _READ_LOCK:
        cached = _CACHE
        if key is not None and cached is not None and cached[0] == key:
            return deepcopy(cached[1])
        loaded, read_key = _read_disk(False)
        if loaded is None:
            return deepcopy(cached[1]) if cached is not None else {}
        if read_key is not None:
            _CACHE = (read_key, deepcopy(loaded))
        return loaded


def raw_kopialens_settings() -> dict[str, Any]:
    """The kopialens dict exactly as the FILE carries it - no DEFAULTS filling absent keys."""
    return _read_settings_file().get("kopialens") or {}


def load_settings() -> dict[str, Any]:
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
    global _CACHE
    with _SETTINGS_LOCK:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        atomicfile.replace(tmp, SETTINGS_FILE)
        try:
            _CACHE = (_file_key(), json.loads(json.dumps(settings)))
        except OSError:
            _CACHE = None
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
