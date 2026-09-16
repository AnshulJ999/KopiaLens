"""Encryption at rest for the unified store ( section 7).

One install-wide data key protects every local Parquet file (index, tree, maps, side caches).
The data key is random and never derived: a password change re-wraps it, it does not re-encrypt
anything. Wrapping uses DuckDB's own authenticated database encryption rather than a second
crypto library - the data key lives as one row inside a tiny encrypted DuckDB file whose key is
derived from the password with Argon2id.

  password --Argon2id--> KEK --wraps--> data key --encrypts--> every Parquet file
  recovery key          --wraps--> data key        (optional, shown once at setup)
  OS keychain           holds     data key         ("remember on this machine")

Encryption is OFF until onboarding () turns it on; every entry point here is a no-op in
that state, so callers never branch.

Two traps this module exists to contain:

  * ``PRAGMA add_parquet_key`` REFUSES a bound parameter ("this type of statement can't be
    prepared"), so the key is inlined into SQL. Keys are therefore kept base64 - the charset is
    quote-safe - and ``_check_key`` enforces that before any string ever reaches a statement.
  * A DuckDB session spills PLAINTEXT to ``temp_directory`` even when every input is encrypted
    Parquet (P6). Only an attached encrypted database plus ``temp_file_encryption`` prevents it,
    which is why ``apply`` attaches a throwaway encrypted session database to every connection.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import threading

import duckdb

from config import CONFIG_DIR

from ..core.applog import get_logger

log = get_logger("crypto")

KEY_NAME = "kl"                       # the parquet key name every statement refers to
_SERVICE = "KopiaLens"                # OS keychain service
_ACCOUNT = "data-key"
_KEY_BYTES = 32                       # AES-256
_B64 = re.compile(r"\A[A-Za-z0-9+/]+={0,2}\Z")

# Argon2id parameters. Deliberately modest: this runs once per unlock on a desktop, and the
# thing it protects is a local metadata store, not a password database.
_KDF = {"time_cost": 3, "memory_cost": 65536, "parallelism": 4}

_LOCK = threading.RLock()
_DATA_KEY: str | None = None          # base64, memory only, never logged or persisted in clear


# ---------------------------------------------------------------------------- key material

def _check_key(key: str) -> str:
    """Guard the inlining trap: a key reaches SQL as a literal, so it must be quote-safe."""
    if not key or not _B64.match(key):
        raise ValueError("key is not base64 - refusing to inline it into SQL")
    return key


def _new_key() -> str:
    return base64.b64encode(secrets.token_bytes(_KEY_BYTES)).decode()


def keystore_dir() -> str:
    return CONFIG_DIR


def _wrap_path(kind: str) -> str:
    return os.path.join(keystore_dir(), f"keystore-{kind}.kdb")


def _meta_path() -> str:
    return os.path.join(keystore_dir(), "keystore.json")


def _derive(password: str, salt: bytes) -> str:
    from argon2.low_level import Type, hash_secret_raw
    raw = hash_secret_raw(secret=password.encode("utf-8"), salt=salt, hash_len=_KEY_BYTES,
                          type=Type.ID, **_KDF)
    return base64.b64encode(raw).decode()


def _read_meta() -> dict:
    import json
    try:
        with open(_meta_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 - absent or unreadable keystore = not set up
        return {}


def _write_meta(meta: dict) -> None:
    import json
    os.makedirs(keystore_dir(), exist_ok=True)
    tmp = _meta_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _meta_path())


def _store_wrapped(kind: str, kek: str, data_key: str) -> None:
    """Write the data key into a tiny encrypted DuckDB file opened with ``kek``."""
    os.makedirs(keystore_dir(), exist_ok=True)   # first setup on a fresh install gets here first
    path = _wrap_path(kind)
    tmp = path + ".tmp"
    for p in (tmp, tmp + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    con = duckdb.connect()
    try:
        con.execute("ATTACH '%s' AS ks (ENCRYPTION_KEY '%s')" % (_sql(tmp), _check_key(kek)))
        con.execute("CREATE TABLE ks.k (name VARCHAR, val VARCHAR)")
        con.execute("INSERT INTO ks.k VALUES ('data_key', ?)", [_check_key(data_key)])
        con.execute("DETACH ks")
    finally:
        con.close()
    os.replace(tmp, path)


def _load_wrapped(kind: str, kek: str) -> str | None:
    """The data key from a wrap file, or None when ``kek`` is wrong or the file is absent.

    DuckDB's database encryption is authenticated, so a wrong key raises "Wrong encryption key
    used to open the database file" rather than returning garbage - which is what makes this a
    usable password check.
    """
    path = _wrap_path(kind)
    if not os.path.exists(path):
        return None
    con = duckdb.connect()
    try:
        con.execute("ATTACH '%s' AS ks (READ_ONLY, ENCRYPTION_KEY '%s')" % (_sql(path), _check_key(kek)))
        row = con.execute("SELECT val FROM ks.k WHERE name = 'data_key'").fetchone()
        con.execute("DETACH ks")
        return _check_key(row[0]) if row else None
    except duckdb.Error:
        return None                    # wrong password, or a corrupt wrap file
    finally:
        con.close()


def _sql(path: str) -> str:
    return path.replace("\\", "/").replace("'", "''")


# ---------------------------------------------------------------------------- public state

def configured() -> bool:
    """True once a password has been set up (a wrap file exists)."""
    return os.path.exists(_wrap_path("password"))


def enabled() -> bool:
    """Encryption is on for this install. Off until onboarding turns it on."""
    from ..settings import load_settings
    return bool(load_settings()["kopialens"].get("encryption")) and configured()


def unlocked() -> bool:
    with _LOCK:
        return _DATA_KEY is not None


def data_key() -> str | None:
    """The unlocked data key (base64), or None. Memory only - never write this anywhere."""
    with _LOCK:
        return _DATA_KEY


def lock() -> None:
    global _DATA_KEY
    with _LOCK:
        _DATA_KEY = None


def ready() -> bool:
    """Encryption is either off, or on AND unlocked - i.e. the store can be read or written.

    The nightly checks this and skips with a reason when it is False (locked and not
    remembered), rather than publishing a half-encrypted generation.
    """
    return not enabled() or unlocked()


# ---------------------------------------------------------------------------- setup / unlock

def setup(password: str, remember: bool = False, with_recovery: bool = True) -> str | None:
    """Create the keystore. Returns the recovery key to show ONCE, or None if not requested.

    Generates a fresh data key; nothing is encrypted by this call. Turning encryption on for
    existing data is a separate rewrite (see ``switch`` in the storage layer).
    """
    global _DATA_KEY
    with _LOCK:
        salt = secrets.token_bytes(16)
        key = _new_key()
        _store_wrapped("password", _derive(password, salt), key)
        recovery = None
        if with_recovery:
            recovery = _new_key()
            _store_wrapped("recovery", recovery, key)
        _write_meta({"version": 1, "kdf": {"name": "argon2id", "salt": base64.b64encode(salt).decode(), **_KDF},
                     "has_recovery": bool(recovery)})
        _DATA_KEY = key
        if remember:
            remember_on_this_machine(True)
        log.info("keystore created (recovery key %s)", "issued" if recovery else "not issued")
        return recovery


def unlock(password: str) -> bool:
    global _DATA_KEY
    with _LOCK:
        meta = _read_meta()
        salt = meta.get("kdf", {}).get("salt")
        if not salt:
            return False
        key = _load_wrapped("password", _derive(password, base64.b64decode(salt)))
        if key is None:
            return False
        _DATA_KEY = key
        return True


def unlock_with_recovery(recovery_key: str) -> bool:
    global _DATA_KEY
    with _LOCK:
        try:
            key = _load_wrapped("recovery", recovery_key.strip())
        except ValueError:
            return False               # not even base64: a mistyped key, not a crash
        if key is None:
            return False
        _DATA_KEY = key
        return True


def unlock_from_keychain() -> bool:
    """Unlock from the OS keychain ("remember on this machine"). False when nothing is stored."""
    global _DATA_KEY
    with _LOCK:
        try:
            import keyring
            key = keyring.get_password(_SERVICE, _ACCOUNT)
        except Exception as exc:  # noqa: BLE001 - no backend, locked keyring, DBus absent
            log.info("keychain unavailable: %s", exc)
            return False
        if not key:
            return False
        _DATA_KEY = _check_key(key)
        return True


def remember_on_this_machine(on: bool) -> bool:
    """Store or clear the data key in the OS keychain. Returns whether the keychain accepted it."""
    try:
        import keyring
        if on:
            key = data_key()
            if not key:
                return False
            keyring.set_password(_SERVICE, _ACCOUNT, key)
        else:
            try:
                keyring.delete_password(_SERVICE, _ACCOUNT)
            except Exception:  # noqa: BLE001 - nothing stored is a successful clear
                pass
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("keychain write failed: %s", exc)
        return False


def is_remembered() -> bool:
    try:
        import keyring
        return bool(keyring.get_password(_SERVICE, _ACCOUNT))
    except Exception:  # noqa: BLE001
        return False


def change_password(old: str, new: str) -> bool:
    """Re-wrap the data key under a new password. Nothing on disk is re-encrypted."""
    with _LOCK:
        meta = _read_meta()
        salt_b64 = meta.get("kdf", {}).get("salt")
        if not salt_b64:
            return False
        key = _load_wrapped("password", _derive(old, base64.b64decode(salt_b64)))
        if key is None:
            return False
        salt = secrets.token_bytes(16)     # fresh salt on every change
        _store_wrapped("password", _derive(new, salt), key)
        meta["kdf"] = {"name": "argon2id", "salt": base64.b64encode(salt).decode(), **_KDF}
        _write_meta(meta)
        return True


def new_recovery_key() -> str | None:
    """Issue (or re-issue) a recovery key for the unlocked data key. Shown once."""
    with _LOCK:
        key = data_key()
        if not key:
            return None
        recovery = _new_key()
        _store_wrapped("recovery", recovery, key)
        meta = _read_meta()
        meta["has_recovery"] = True
        _write_meta(meta)
        return recovery


# ---------------------------------------------------------------------------- connections

def parquet_read(path_sql: str) -> str:
    """``read_parquet(...)`` argument list carrying the encryption config when it is on.

    Every read of a store file goes through here or ``read_clause`` so a new call site cannot
    silently omit the key and get "Computed AES tag differs" at runtime.
    """
    if enabled() and unlocked():
        return "'%s', encryption_config = {footer_key: '%s'}" % (path_sql, KEY_NAME)
    return "'%s'" % path_sql


def parquet_read_list(paths_sql: list[str]) -> str:
    inner = ", ".join("'%s'" % p for p in paths_sql)
    if enabled() and unlocked():
        return "[%s], encryption_config = {footer_key: '%s'}" % (inner, KEY_NAME)
    return "[%s]" % inner


def copy_options(extra: str = "FORMAT parquet, COMPRESSION zstd") -> str:
    """The ``COPY ... TO x (...)`` option list, with the encryption config when it is on."""
    if enabled() and unlocked():
        return "%s, ENCRYPTION_CONFIG {footer_key: '%s'}" % (extra, KEY_NAME)
    return extra


class _Session:
    """The throwaway encrypted database attached to one connection, removed when it closes."""

    def __init__(self, path: str):
        self.path = path

    def cleanup(self) -> None:
        for p in (self.path, self.path + ".wal"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def apply(con, temp_dir: str, memory_limit: str, threads: int) -> _Session | None:
    """Configure one DuckDB connection: limits, temp directory, and the encrypted session.

    When encryption is on this attaches a fresh encrypted database and switches the connection
    to it, because an in-memory session spills PLAINTEXT to ``temp_directory`` even while every
    input is encrypted Parquet (P6). The attached file is this connection's alone and is deleted
    when the session is cleaned up.
    """
    os.makedirs(temp_dir, exist_ok=True)
    con.execute("SET temp_directory='%s'" % _sql(temp_dir))
    con.execute("SET memory_limit='%s'" % memory_limit)
    con.execute("SET threads=%d" % threads)
    # Rule 1 of the store: results stay in DuckDB until the final small answer, and nothing
    # depends on input order. Insertion order costs memory in every hash aggregate.
    con.execute("SET preserve_insertion_order=false")
    if not (enabled() and unlocked()):
        return None
    key = _check_key(data_key())
    path = os.path.join(temp_dir, "session_%d_%s.db" % (os.getpid(), secrets.token_hex(6)))
    session = _Session(path)
    try:
        con.execute("ATTACH '%s' AS klsession (ENCRYPTION_KEY '%s')" % (_sql(path), key))
        con.execute("USE klsession")
        con.execute("SET temp_file_encryption=true")
        con.execute("PRAGMA add_parquet_key('%s', '%s')" % (KEY_NAME, key))
    except Exception:
        session.cleanup()
        raise
    return session


def sweep_sessions(temp_dir: str) -> int:
    """Remove session databases a crashed process left behind. Returns how many went."""
    gone = 0
    try:
        for fn in os.listdir(temp_dir):
            if fn.startswith("session_") and (fn.endswith(".db") or fn.endswith(".db.wal")):
                try:
                    os.remove(os.path.join(temp_dir, fn))
                    gone += 1
                except OSError:
                    pass               # still held by a live process
    except OSError:
        pass
    return gone
