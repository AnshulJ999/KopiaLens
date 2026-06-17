"""KopiaLens config."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
KOPIA_DIR = os.path.dirname(HERE)                                       # ..\Kopia
CACHEDIR = os.path.join(KOPIA_DIR, ".cache")                            # shared, already-warm cache
SETTINGS_FILE = os.path.join(HERE, "settings.json")                    # KopiaLens-local (migrated from the old shared CLI file)
DATA_DIR = os.path.join(HERE, "data")                                   # duckdb + logs (gitignored)

HOST, PORT = "127.0.0.1", 7123                                          # localhost-only

REPO2_CONFIG = r"C:\Users\me\AppData\Roaming\kopia\repository.config"
REPOS = {
    "1": {"name": "Main Kopia Backup (E:\\Kopia)", "args": []},
    "2": {"name": "Kopia Big Backup (I:\\Kopia Big Backup)", "args": ["--config-file", REPO2_CONFIG]},
}
