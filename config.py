"""KopiaLens config.

Repositories come from a JSON record store (``repos.json``) rather than a literal in this file, and
the writable directories are relocatable. ``REPOS``/``repo_kind``/``kopia_repos`` keep their original
shape, so every caller that treats a repo id as an opaque key is unaffected.
"""
import json
import os
import sys

import appversion

HERE = os.path.dirname(os.path.abspath(__file__))
# The program that relaunches KopiaLens roles (server, workers). A Nuitka build's sys.executable
# names a python.exe that does not exist; its real binary is sys.argv[0].
COMPILED = "__compiled__" in globals()
SELF_EXE = os.path.abspath(sys.argv[0]) if COMPILED else sys.executable


APP_VERSION = appversion.version()   # git tag in a source checkout, else the VERSION file a build wrote
APP_COMMIT = appversion.commit()
# A testing build (release.py --testing): Experimental and Notes start switched on.
TESTING_BUILD = os.path.isfile(os.path.join(HERE, "devnotes", "TESTING.txt"))
API_VERSION = 1   # the stable API's version (docs/api.md); matches klens.API_VERSION

# Online addresses baked into every shipped copy. Final values before the first public build.
LICENCE_SERVICE_URL = os.environ.get("KOPIALENS_LICENCE_SERVICE") or "https://licence.kopialens.com"
PRICING_URL = os.environ.get("KOPIALENS_PRICING_URL") or "https://kopialens.com/#pricing"
# Daily update check + usage stats (docs/usage-stats.md).
CHECKIN_URL = os.environ.get("KOPIALENS_CHECKIN_URL") or "https://checkin.kopialens.com/v1/checkin"
FEEDBACK_URL = os.environ.get("KOPIALENS_FEEDBACK_URL") or "https://checkin.kopialens.com/v1/feedback"
# Help menu.
DOCS_URL = os.environ.get("KOPIALENS_DOCS_URL") or "https://kopialens.com/docs"
REPO_URL = os.environ.get("KOPIALENS_REPO_URL") or "https://github.com/AnshulJ999/KopiaLens"
SUPPORT_EMAIL = os.environ.get("KOPIALENS_SUPPORT_EMAIL") or "support@kopialens.com"
DISCORD_URL = os.environ.get("KOPIALENS_DISCORD_URL") or "https://discord.gg/22EU4cqEf6"
# Settings > About: ways to support development (same links as SyncLyrics).
SUPPORT_LINKS = [
    {"label": "GitHub Sponsors", "url": "https://github.com/sponsors/AnshulJ999", "icon": "github"},
    {"label": "Patreon", "url": "https://www.patreon.com/AnshulJain", "icon": "patreon", "note": "includes Pro"},
    {"label": "Ko-fi", "url": "https://ko-fi.com/anshul99", "icon": "coffee"},
    {"label": "PayPal", "url": "https://paypal.me/AnshulJain99", "icon": "paypal"},
]


def user_data_root() -> str:
    """The per-user data folder for an installed app."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "KopiaLens")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "KopiaLens")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "kopialens")


# A portable copy keeps its data in data\ beside the app, or in the folder a ``data=<path>`` line
# names (relative paths are beside the app): in PORTABLE_DATA_FILE, which the portable zip never
# contains so extracting a new version over the copy keeps it, else in the marker (older copies).
# The marker sits next to the desktop app, one level above the server folder; it beats
# KOPIALENS_DATA_DIR, and the desktop app passes the same folder on. shell/src-tauri/src/server.rs
# reads both files the same way.
PORTABLE_MARKER = "KopiaLens-portable.txt"
PORTABLE_DATA_FILE = "KopiaLens-data.txt"


def _data_line(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.partition("=")
        value = value.strip().strip('"')
        if sep and key.strip().lower() == "data" and value:
            return value
    return None


def portable_dir_of(app_dir: str) -> str | None:
    marker = os.path.join(app_dir, PORTABLE_MARKER)
    if not os.path.isfile(marker):
        return None
    value = _data_line(os.path.join(app_dir, PORTABLE_DATA_FILE)) or _data_line(marker)
    if not value:
        return os.path.join(app_dir, "data")
    return value if os.path.isabs(value) else os.path.join(app_dir, value)


def portable_data_dir() -> str | None:
    if not COMPILED:
        return None
    return portable_dir_of(os.path.dirname(HERE))


def _dev_env(name: str) -> str | None:
    """A setting from the git-ignored ``.env.dev`` of a source checkout (developer machines only;
    compiled builds never read it)."""
    try:
        with open(os.path.join(HERE, ".env.dev"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip().strip('"') or None
    return None


# A source checkout may name its data folder in .env.dev instead of a system-wide variable, so an
# installed or portable KopiaLens on the same computer never opens the developer's live data. Set in
# the environment so workers and other child processes inherit it; an explicit variable still wins.
if not COMPILED and os.environ.get("KOPIALENS_DATA_DIR") is None and _dev_env("KOPIALENS_DATA_DIR"):
    os.environ["KOPIALENS_DATA_DIR"] = _dev_env("KOPIALENS_DATA_DIR")


def _env_port(name: str, default: int) -> int:
    """A port from the environment; a bad value falls back to the default instead of crashing."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if 1 <= port <= 65535:
        return port
    print(f"KopiaLens: ignoring {name}={raw!r} (not a port number), using {default}", file=sys.stderr)
    return default


# Everything KopiaLens writes lives under one data folder: index, cache (cache/), logs, audit,
# backups, settings.json, repos.json. KOPIALENS_DATA_DIR relocates all of it; the finer env
# overrides below still win for their own part.
#
# Old layout: settings.json + repos.json next to the code, data/ beside them, the walk cache in
# ../.cache. An install that still has repos.json here keeps that layout until it is moved.
LEGACY_LAYOUT = not os.environ.get("KOPIALENS_DATA_DIR") and os.path.exists(os.path.join(HERE, "repos.json"))
_LEGACY = LEGACY_LAYOUT
if _LEGACY:
    _DEFAULT_DATA_DIR = os.path.join(HERE, "data")
    _DEFAULT_CONFIG_DIR = HERE
    _DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(HERE), ".cache")
else:
    _DEFAULT_DATA_DIR = portable_data_dir() or os.environ.get("KOPIALENS_DATA_DIR") or user_data_root()
    _DEFAULT_CONFIG_DIR = _DEFAULT_DATA_DIR
    _DEFAULT_CACHE_DIR = os.path.join(_DEFAULT_DATA_DIR, "cache")

CONFIG_DIR = os.environ.get("KOPIALENS_CONFIG_DIR") or _DEFAULT_CONFIG_DIR
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
REPOS_FILE = os.environ.get("KOPIALENS_REPOS_FILE") or os.path.join(CONFIG_DIR, "repos.json")

def _setting_port(key: str, default: int) -> int:
    """A port saved in settings.json (Settings > Advanced), read before anything else loads."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            value = int((json.load(fh).get("kopialens") or {}).get(key) or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return default
    return value if 1 <= value <= 65535 else default


# Loopback by DEFAULT: reaching KopiaLens from another machine is a deliberate choice, not the
# out-of-the-box state (it browses and can delete from a backup repository). The actual bind comes
# from settings.kopialens.bind_host - see app.core.auth.bind_host - with this as the fallback.
# Ports: environment, then Settings > Advanced, then the default.
HOST, PORT = "127.0.0.1", _env_port("KOPIALENS_PORT", _setting_port("port", 7123))
GUARD_HOST = "127.0.0.1"                                                # single-instance guard probes loopback (a 0.0.0.0 bind includes it; connecting to 0.0.0.0 is unreliable on Windows)

def save_records(store: dict, strict: bool = False) -> None:
    """Persist the record store (see app.core.repos). ``strict`` raises OSError instead of
    swallowing it - a user's add/edit/remove must not report success when nothing was saved."""
    _write_records(store, strict)


def _write_records(store: dict, strict: bool = False) -> None:
    """Atomic, cross-process locked, and never fatal at startup: a read-only config dir must not
    stop the app."""
    from app.core import configbackup
    from app.core.filelock import locked
    try:
        os.makedirs(os.path.dirname(REPOS_FILE), exist_ok=True)
        with locked(REPOS_FILE):
            configbackup.before_write()
            tmp = REPOS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(store, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, REPOS_FILE)
        configbackup.remember("repos.json")
    except OSError:
        if strict:
            raise


def _load_records() -> dict:
    """The record store. A damaged file is moved aside and the newest good backup put back; a file
    that cannot be opened right now is left alone (nothing is written over it)."""
    from app.core import configbackup
    for attempt in range(2):
        try:
            with open(REPOS_FILE, encoding="utf-8") as fh:
                store = json.load(fh)
            if isinstance(store, dict) and isinstance(store.get("repos"), list):
                return store
        except FileNotFoundError:
            break
        except ValueError:
            pass
        except OSError:
            return {"version": 1, "cache_dir": None, "repos": []}
        if attempt == 0 and not configbackup.recover("repos.json"):
            break
    # A fresh install starts with no repositories; guessing someone's drive letters is worse.
    store = {"version": 1, "cache_dir": None, "repos": []}
    _write_records(store)
    return store


RECORDS = _load_records()

# The record store lives inside the data folder, so it can only relocate the cache.
DATA_DIR = (os.environ.get("KOPIALENS_DATA_DIR")
            or (RECORDS.get("data_dir") if _LEGACY else None) or _DEFAULT_DATA_DIR)
CACHEDIR = os.environ.get("KOPIALENS_CACHE_DIR") or RECORDS.get("cache_dir") or _DEFAULT_CACHE_DIR


def _to_entry(rec: dict) -> dict:
    """One record -> the legacy REPOS value. `args` is the kopia CLI suffix (empty for a repo that
    is not kopia-backed, or one using kopia's default config)."""
    cfg = rec.get("config_file")
    return {
        "name": rec.get("name") or rec.get("id"),
        "args": ["--config-file", cfg] if cfg else [],
        "kind": rec.get("kind", "kopia"),
        # display + policy fields the legacy callers may not need but must not drop:
        "short_name": rec.get("short_name") or "",
        "counts_as_backup": rec.get("counts_as_backup", True),
        # A demo copy: an index with faked names and no kopia behind it (app.core.demo).
        "demo": bool(rec.get("demo")),
    }


# Each repo carries a `kind`: "kopia" (DuckDB-backed, the original) or "backblaze" (read-only
# SQLite index of the local Backblaze bzdata - never touches kopia/DuckDB code). Use kopia_repos()
# in any kopia-only loop.
REPOS = {str(r["id"]): _to_entry(r) for r in RECORDS.get("repos", []) if r.get("id")}

# Per-source separator / case-sensitivity overrides, merged across records (a source key identifies
# the same machine+path in any repo). Read by app.core.paths.
PATH_OVERRIDES = {}
for _rec in RECORDS.get("repos", []):
    PATH_OVERRIDES.update(_rec.get("path_overrides") or {})


def reload_repos() -> None:
    """Rebuild REPOS / PATH_OVERRIDES from the record store, IN PLACE.

    In place is the point: `from config import REPOS` binds the dict object, so rebinding the
    name would leave every importer reading the pre-edit store - a repo added at runtime would
    stay invisible to `_repo_args` until a restart. Called by core.repos after every store write.
    """
    REPOS.clear()
    REPOS.update({str(r["id"]): _to_entry(r) for r in RECORDS.get("repos", []) if r.get("id")})
    PATH_OVERRIDES.clear()
    for rec in RECORDS.get("repos", []):
        PATH_OVERRIDES.update(rec.get("path_overrides") or {})


def repo_record(repo: str) -> dict:
    """The full record for a repo id (kopia Unique ID, connection type, config path, ...).
    ``REPOS`` carries only what the legacy callers need."""
    for rec in RECORDS.get("repos", []):
        if str(rec.get("id")) == str(repo):
            return dict(rec)
    return {}


def repo_kind(repo: str) -> str:
    return REPOS.get(repo, {}).get("kind", "kopia")


def is_demo(repo: str) -> bool:
    return bool(REPOS.get(repo, {}).get("demo"))


# A data folder written by app.core.demo: every repo in it is a demo copy and the local disk is
# never read (a realistic fake path can exist on this machine).
DEMO_INSTANCE = os.path.isfile(os.path.join(DATA_DIR, "demo.json"))


def _demo_meta() -> dict:
    try:
        with open(os.path.join(DATA_DIR, "demo.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# A demo folder with its own generated bzdata shows the Backblaze screens (app.core.demobz).
DEMO_BACKBLAZE = DEMO_INSTANCE and bool(_demo_meta().get("backblaze"))


def kopia_repos() -> list:
    """Repo ids backed by kopia/DuckDB. Use this (NOT `REPOS`) in any loop that opens a
    DuckDB cursor or runs kopia - so the Backblaze repo is never dragged through kopia code."""
    return [r for r, c in REPOS.items() if c.get("kind", "kopia") == "kopia"]
