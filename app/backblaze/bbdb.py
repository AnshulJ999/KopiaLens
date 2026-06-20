"""SQLite store for the Backblaze index (read-only on bzdata; this DB lives under KopiaLens/data).

Chosen over DuckDB deliberately: BZ is a read-only source rebuilt-on-change, so SQLite avoids
every DuckDB pain (grow-only file, ART index-delete crash, compaction machinery). Substring
("Everything-style") search uses an FTS5 **trigram** index over the lowercased path, which is the
right tool for arbitrary substring matching (falls back to a LIKE scan if trigram is unavailable).
"""

from __future__ import annotations

import os
import sqlite3
import threading

try:
    from config import DATA_DIR  # KopiaLens/data
except Exception:  # standalone / tests
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
_FTS_OK = True  # set False if the trigram FTS5 table can't be created (old SQLite)


def db_path() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "kopialens_bz.sqlite")


def fts_available() -> bool:
    return _FTS_OK


def connect() -> sqlite3.Connection:
    """Process-wide connection (created + schema-initialised once). WAL for concurrent reads."""
    global _CONN
    with _LOCK:
        if _CONN is None:
            con = sqlite3.connect(db_path(), check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA temp_store=MEMORY")
            _init_schema(con)
            _CONN = con
        return _CONN


def close() -> None:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            finally:
                _CONN = None


def _init_schema(con: sqlite3.Connection) -> None:
    global _FTS_OK
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            drive TEXT, full_path TEXT, path_lc TEXT, name TEXT, ext TEXT,
            size INTEGER, mtime_hex TEXT, excluded INTEGER, excl_rule TEXT
        );
        CREATE INDEX IF NOT EXISTS files_drive   ON files(drive);
        CREATE INDEX IF NOT EXISTS files_pathlc  ON files(path_lc);
        CREATE INDEX IF NOT EXISTS files_ext     ON files(ext);
        CREATE INDEX IF NOT EXISTS files_excl    ON files(excluded);

        CREATE TABLE IF NOT EXISTS queue (drive TEXT, full_path TEXT, size INTEGER);
        CREATE INDEX IF NOT EXISTS queue_drive ON queue(drive);

        CREATE TABLE IF NOT EXISTS events (
            ts TEXT, kind TEXT, drive TEXT, files INTEGER, bytes INTEGER, detail TEXT, src TEXT
        );
        CREATE INDEX IF NOT EXISTS events_ts   ON events(ts);
        CREATE INDEX IF NOT EXISTS events_kind ON events(kind);

        CREATE TABLE IF NOT EXISTS churn (folder TEXT, cnt INTEGER);

        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS ingest (source TEXT PRIMARY KEY, mtime REAL, size INTEGER, extra TEXT);
        """
    )
    # FTS5 trigram over path_lc (external content = mirrors `files` by rowid; rebuilt after a batch).
    try:
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5("
            "path_lc, content='files', content_rowid='rowid', tokenize='trigram')"
        )
        _FTS_OK = True
    except sqlite3.OperationalError:
        _FTS_OK = False
    con.commit()


def rebuild_fts(con: sqlite3.Connection) -> None:
    """Re-sync the external-content FTS index from `files` (call once after an index batch)."""
    if not _FTS_OK:
        return
    try:
        con.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
        con.commit()
    except sqlite3.OperationalError:
        pass


# ---- small helpers ----------------------------------------------------------------------
def kv_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                [key, value])


def kv_get(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT v FROM kv WHERE k=?", [key]).fetchone()
    return row[0] if row else default


def ingest_get(con: sqlite3.Connection, source: str) -> tuple[float, int, str] | None:
    row = con.execute("SELECT mtime, size, extra FROM ingest WHERE source=?", [source]).fetchone()
    return (row[0], row[1], row[2]) if row else None


def ingest_set(con: sqlite3.Connection, source: str, mtime: float, size: int, extra: str = "") -> None:
    con.execute(
        "INSERT INTO ingest(source, mtime, size, extra) VALUES(?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, extra=excluded.extra",
        [source, mtime, size, extra],
    )
