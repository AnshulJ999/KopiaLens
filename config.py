"""KopiaLens config."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
KOPIA_DIR = os.path.dirname(HERE)                                       # ..\Kopia
CACHEDIR = os.path.join(KOPIA_DIR, ".cache")                            # shared, already-warm cache
SETTINGS_FILE = os.path.join(HERE, "settings.json")                    # KopiaLens-local (migrated from the old shared CLI file)
DATA_DIR = os.path.join(HERE, "data")                                   # duckdb + logs (gitignored)

HOST, PORT = "127.0.0.1", 7123                                          # localhost-only

REPO2_CONFIG = r"C:\Users\me\AppData\Roaming\kopia\repository.config"
# Each repo carries a `kind`: "kopia" (DuckDB-backed, the original) or "backblaze" (read-only
# SQLite index of the local Backblaze bzdata - never touches kopia/DuckDB code). `args` is the
# kopia CLI suffix (empty for non-kopia repos). Use kopia_repos() in any kopia-only loop.
REPOS = {
    "1": {"name": "Main Kopia Backup (E:\\Kopia)", "args": [], "kind": "kopia"},
    "2": {"name": "Kopia Big Backup (I:\\Kopia Big Backup)", "args": ["--config-file", REPO2_CONFIG], "kind": "kopia"},
    "bz": {"name": "Backblaze Personal Backup", "args": [], "kind": "backblaze"},
}


def repo_kind(repo: str) -> str:
    return REPOS.get(repo, {}).get("kind", "kopia")


def kopia_repos() -> list:
    """Repo ids backed by kopia/DuckDB. Use this (NOT `REPOS`) in any loop that opens a
    DuckDB cursor or runs kopia - so the Backblaze repo is never dragged through kopia code."""
    return [r for r, c in REPOS.items() if c.get("kind", "kopia") == "kopia"]
