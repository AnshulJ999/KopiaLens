"""KopiaLens config.

Repositories come from a JSON record store (``repos.json``) rather than a literal in this file, and
the writable directories are relocatable. ``REPOS``/``repo_kind``/``kopia_repos`` keep their original
shape, so every caller that treats a repo id as an opaque key is unaffected.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
KOPIA_DIR = os.path.dirname(HERE)                                       # ..\Kopia

# Env beats the record store beats the default - Docker sets env, so env has to win.
CONFIG_DIR = os.environ.get("KOPIALENS_CONFIG_DIR") or HERE
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")              # KopiaLens-local (migrated from the old shared CLI file)
REPOS_FILE = os.environ.get("KOPIALENS_REPOS_FILE") or os.path.join(CONFIG_DIR, "repos.json")

_DEFAULT_DATA_DIR = os.path.join(HERE, "data")                          # duckdb + logs (gitignored)
_DEFAULT_CACHE_DIR = os.path.join(KOPIA_DIR, ".cache")                  # shared, already-warm cache

HOST, PORT = "0.0.0.0", 7123                                            # bind all interfaces (LAN-reachable for Caddy reverse-proxy at kopia.example.com)
GUARD_HOST = "127.0.0.1"                                                # single-instance guard probes loopback (the 0.0.0.0 bind includes it; connecting to 0.0.0.0 is unreliable on Windows)

# Tray control API (kopialens_tray.py). Restart/quit live in the TRAY, not the server: a process
# cannot kill itself and still answer the request that asked it to. Loopback ONLY and unauthenticated
# - fine for a personal tool on this machine, revisit before any public release.
TRAY_CONTROL_HOST, TRAY_CONTROL_PORT = "127.0.0.1", 7124

# Seed used only when repos.json is absent AND an index already exists (i.e. an install that predates
# the record store). A fresh install gets an EMPTY store and the "no repositories configured" state -
# guessing someone else's drive letters would be worse than asking.
_LEGACY_REPOS = [
    {"id": "1", "name": "Main Kopia Backup (E:\\Kopia)", "kind": "kopia", "config_file": None},
    {"id": "2", "name": "Kopia Big Backup (I:\\Kopia Big Backup)", "kind": "kopia",
     "config_file": r"C:\Users\me\AppData\Roaming\kopia\repository.config"},
    {"id": "bz", "name": "Backblaze Personal Backup", "kind": "backblaze", "config_file": None},
]


def _looks_like_existing_install() -> bool:
    try:
        return any(n.startswith("kopialens_repo") and n.endswith(".duckdb")
                   for n in os.listdir(_DEFAULT_DATA_DIR))
    except OSError:
        return False


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
    store = {"version": 1, "data_dir": None, "cache_dir": None,
             "repos": [dict(r) for r in _LEGACY_REPOS] if _looks_like_existing_install() else []}
    _write_records(store)
    return store


RECORDS = _load_records()

DATA_DIR = os.environ.get("KOPIALENS_DATA_DIR") or RECORDS.get("data_dir") or _DEFAULT_DATA_DIR
CACHEDIR = os.environ.get("KOPIALENS_CACHE_DIR") or RECORDS.get("cache_dir") or _DEFAULT_CACHE_DIR


def _to_entry(rec: dict) -> dict:
    """One record -> the legacy REPOS value. `args` is the kopia CLI suffix (empty for a repo that
    is not kopia-backed, or one using kopia's default config)."""
    cfg = rec.get("config_file")
    return {
        "name": rec.get("name") or rec.get("id"),
        "args": ["--config-file", cfg] if cfg else [],
        "kind": rec.get("kind", "kopia"),
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
