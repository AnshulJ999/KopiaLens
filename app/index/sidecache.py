"""Side caches as one-row Parquet.

The small derived caches - churn rollups, folder prewarm, policy prices, the dashboard table,
simulate results, next-snapshot times - are kopia-derived metadata (paths, sizes, dates). As plain
JSON they would sit in cleartext beside an encrypted store, so they go through DuckDB too: one
key, one format, one reader, and the same `crypto.copy_options` / `crypto.parquet_read` the store
itself uses. No second crypto library.

Format: `<name>.parquet` under CACHEDIR, one row ``(payload VARCHAR, written_at TIMESTAMP)``. The
payload is exactly the JSON the callers already produced and consumed, so their shapes do not
change - only where the bytes live.

Encryption is off until onboarding, so these are plaintext Parquet today; when it is switched on
they are written encrypted and read with the key, with no caller change.

A cache file carries its own state - the Parquet footer magic (PARE/PAR1) says whether it is
encrypted - so a read never has to trust the setting. That matters while a switch is running:
the setting can already be flipped while old files are still in the previous state, and each
file stays readable until the switch rewrites it.

Two rules:
  * A cache is never written in cleartext while encryption is on. If the key is locked, ``write``
    returns False and the caller keeps its data - a stale cache is fine, a leaked one is not.
  * A missing, unreadable or undecryptable file reads as None. Every caller already handles
    "no cache"; a cache is never load-bearing for correctness.
"""

from __future__ import annotations

import json
import os
import threading

import duckdb

from config import CACHEDIR

from ..core.applog import get_logger
from . import crypto
from ..core import atomicfile

log = get_logger("sidecache")


def path(name: str, base: str | None = None) -> str:
    return os.path.join(base or CACHEDIR, f"{name}.parquet")


def _sql(p: str) -> str:
    return p.replace("\\", "/").replace("'", "''")


def write(name: str, payload: dict, base: str | None = None) -> bool:
    """Persist one cache payload. False when encryption is on but locked (never write cleartext)."""
    if crypto.writes_encrypted() and not crypto.unlocked():
        log.warning("sidecache %s not written: encryption is on and locked", name)
        return False
    try:
        os.makedirs(base or CACHEDIR, exist_ok=True)
        target = path(name, base)
        # Per-writer temp name: two background refreshes writing the same cache must not interleave
        # into one file (the folder cache hit exactly that with a single fixed `.tmp`).
        tmp = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
        for p in (tmp, tmp + ".wal"):
            if os.path.exists(p):
                os.remove(p)
        try:
            con = duckdb.connect()
            try:
                # copy_options NAMES the key; only this puts it on the connection. Without both,
                # an encrypted write fails outright and every cache silently stops persisting.
                crypto.register_key(con)
                con.execute("CREATE TABLE _p AS SELECT ?::VARCHAR AS payload, now() AS written_at",
                            [json.dumps(payload)])
                con.execute("COPY (SELECT * FROM _p) TO '%s' (%s)"
                            % (_sql(tmp), crypto.copy_options()))
            finally:
                con.close()
            atomicfile.replace(tmp, target)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:  # noqa: BLE001 - a cache write must never break the caller
        log.warning("sidecache %s write failed: %s", name, exc)
        return False


def read(name: str, base: str | None = None) -> dict | None:
    """The cached payload, or None when absent, unreadable, or encrypted and locked."""
    p = path(name, base)
    if not os.path.exists(p):
        return None
    encrypted = crypto.file_is_encrypted(p)   # the file's own state, never the setting
    con = duckdb.connect()
    try:
        crypto.register_key(con, encrypted=encrypted)   # parquet_read names the key; this registers it
        row = con.execute(
            f"SELECT payload FROM read_parquet({crypto.parquet_read(_sql(p), encrypted=encrypted)})"
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - a cache miss, never an error
        log.debug("sidecache %s unreadable: %s", name, exc)
        return None
    finally:
        con.close()
    if not row or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        log.warning("sidecache %s payload is not valid JSON", name)
        return None


def drop(name: str, base: str | None = None) -> None:
    """Remove a cache file (best effort)."""
    try:
        os.remove(path(name, base))
    except OSError:
        pass
