"""KopiaLens config.

Repositories come from a JSON record store (``repos.json``) rather than a literal in this file, and
the writable directories are relocatable. ``REPOS``/``repo_kind``/``kopia_repos`` keep their original
shape, so every caller that treats a repo id as an opaque key is unaffected.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def user_data_root() -> str:
    """The per-user data folder for an installed app."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "KopiaLens")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "KopiaLens")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "kopialens")


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
    _DEFAULT_DATA_DIR = os.environ.get("KOPIALENS_DATA_DIR") or user_data_root()
    _DEFAULT_CONFIG_DIR = _DEFAULT_DATA_DIR
    _DEFAULT_CACHE_DIR = os.path.join(_DEFAULT_DATA_DIR, "cache")

CONFIG_DIR = os.environ.get("KOPIALENS_CONFIG_DIR") or _DEFAULT_CONFIG_DIR
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
REPOS_FILE = os.environ.get("KOPIALENS_REPOS_FILE") or os.path.join(CONFIG_DIR, "repos.json")

# Loopback by DEFAULT: reaching KopiaLens from another machine is a deliberate choice, not the
# out-of-the-box state (it browses and can delete from a backup repository). The actual bind comes
# from settings.kopialens.bind_host - see app.core.auth.bind_host - with this as the fallback.
HOST, PORT = "127.0.0.1", 7123
GUARD_HOST = "127.0.0.1"                                                # single-instance guard probes loopback (a 0.0.0.0 bind includes it; connecting to 0.0.0.0 is unreliable on Windows)

# Tray control API (kopialens_tray.py). Restart/quit live in the TRAY, not the server: a process
# cannot kill itself and still answer the request that asked it to. Loopback ONLY and unauthenticated
# - fine for a personal tool on this machine, revisit before any public release.
TRAY_CONTROL_HOST, TRAY_CONTROL_PORT = "127.0.0.1", 7124

def save_records(store: dict) -> None:
    """Persist the record store (see app.core.repos)."""
    _write_records(store)


def _write_records(store: dict) -> None:
    """Atomic, and never fatal: a read-only config dir must not stop the app from starting."""
    try:
        os.makedirs(os.path.dirname(REPOS_FILE), exist_ok=True)
        tmp = REPOS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, REPOS_FILE)
    except OSError:
        pass


def _load_records() -> dict:
    try:
        with open(REPOS_FILE, encoding="utf-8") as fh:
            store = json.load(fh)
        if isinstance(store, dict) and isinstance(store.get("repos"), list):
            return store
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        # Present but unreadable: keep the bad copy rather than overwrite it with a seed.
        try:
            os.replace(REPOS_FILE, REPOS_FILE + ".corrupt")
        except OSError:
            pass
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


def kopia_repos() -> list:
    """Repo ids backed by kopia/DuckDB. Use this (NOT `REPOS`) in any loop that opens a
    DuckDB cursor or runs kopia - so the Backblaze repo is never dragged through kopia code."""
    return [r for r, c in REPOS.items() if c.get("kind", "kopia") == "kopia"]
