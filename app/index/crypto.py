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

The setting is the INTENT, the data is the FACT. Each generation records its own state in the
manifest (``encrypted``) and readers follow the manifest, never the setting, so the two can never
disagree about what is on disk while a switch is pending. Files outside a generation (the side
caches) carry their state in the Parquet footer magic - ``PARE`` when encrypted, ``PAR1`` when
not - so they stay readable across a switch with no marker to get out of step.

Four traps this module exists to contain:

  * ``PRAGMA add_parquet_key`` REFUSES a bound parameter ("this type of statement can't be
    prepared"), so the key is inlined into SQL. Keys are therefore kept base64 - the charset is
    quote-safe - and ``_check_key`` enforces that before any string ever reaches a statement.
  * A DuckDB session spills PLAINTEXT to ``temp_directory`` even when every input is encrypted
    Parquet (P6). Only an attached encrypted database plus ``temp_file_encryption`` prevents it,
    which is why ``apply`` attaches a throwaway encrypted session database to every connection.
  * A wrong password and a damaged keystore file look identical to DuckDB unless the two error
    shapes are told apart, which is why ``_load_wrapped`` splits them.
  * Asking for encryption while the key is locked must FAIL, never quietly fall back to writing
    or reading plaintext - the silent-plaintext write is the one mistake nothing else catches.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import json
import os
import re
import secrets
import shutil
import threading

import duckdb

from config import CONFIG_DIR, DATA_DIR

from ..core.applog import get_logger
from ..core import atomicfile

log = get_logger("crypto")

KEY_NAME = "kl"                       # the parquet key name every statement refers to
SESSION_ALIAS = "klsession"           # the attached encrypted session database's name
_SERVICE = "KopiaLens"                # OS keychain service
_ACCOUNT = "data-key"                 # legacy entry name; new entries are scoped per install
_KEY_BYTES = 32                       # AES-256
_B64 = re.compile(r"\A[A-Za-z0-9+/]+={0,2}\Z")

# Parquet footer magic: the encrypted writer ends a file with PARE, the plaintext one with PAR1.
_MAGIC_PLAIN = b"PAR1"
_MAGIC_ENC = b"PARE"

# Argon2id parameters. Deliberately modest: this runs once per unlock on a desktop, and the
# thing it protects is a local metadata store, not a password database.
_KDF = {"time_cost": 3, "memory_cost": 65536, "parallelism": 4}

_LOCK = threading.RLock()
_DATA_KEY: str | None = None          # base64, memory only, never logged or persisted in clear
_SOURCE: str | None = None            # how it got here: "keychain" | "password" | "recovery" | "pipe"

# Files written inside this block go to the given state regardless of the setting. The index
# build sets it from its previous manifest - while a switch is pending, the setting is ahead of
# the data and a build that followed it would write a generation the manifest misdescribes.
_OVERRIDE: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "kl_encryption_override", default=None)


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
    try:
        with open(_meta_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 - absent or unreadable keystore = not set up
        return {}


def _write_meta(meta: dict) -> None:
    os.makedirs(keystore_dir(), exist_ok=True)
    tmp = _meta_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    atomicfile.replace(tmp, _meta_path())


def _store_wrapped(kind: str, kek: str, data_key: str) -> None:
    """Write the data key into a tiny encrypted DuckDB file opened with ``kek``."""
    os.makedirs(keystore_dir(), exist_ok=True)   # first setup on a fresh install gets here first
    path = _wrap_path(kind)
    tmp = path + ".tmp"
    # The TARGET's `.wal` too, not just the temp's: os.replace would otherwise leave a stale
    # DuckDB WAL beside the new database file, and the next open could replay it onto it.
    for p in (tmp, tmp + ".wal", path + ".wal"):
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
    atomicfile.replace(tmp, path)


class StoreLocked(PermissionError):
    """The data key is not in memory, so an encrypted store cannot be read or written.

    A PermissionError subclass so every existing ``except PermissionError`` keeps working,
    while the API can tell a LOCK from a real filesystem permission problem and answer the
    unlock screen with a structured 423 instead of a raw 500.
    """


class KeystoreError(Exception):
    """A wrap file would not open."""


class WrongPassword(KeystoreError):
    """The password or recovery key did not unwrap the data key."""


class CorruptKeystore(KeystoreError):
    """The wrap file is damaged, unreadable, or not a keystore at all."""


def _redact(text: str, *secrets_: str) -> str:
    """Drop key material from a message before it can reach a log, a traceback, or a job record.

    DuckDB echoes the failing statement in some errors, and our statements inline the key.
    """
    out = text
    for secret in secrets_:
        if secret:
            out = out.replace(secret, "***")
    return out


def _load_wrapped(kind: str, kek: str) -> str | None:
    return _load_wrapped_path(_wrap_path(kind), kek)


def _load_wrapped_path(path: str, kek: str) -> str | None:
    """The data key from a wrap file, or None when the file is absent.

    DuckDB's database encryption is authenticated, so a wrong key raises "Wrong encryption key
    used to open the database file" rather than returning garbage - which is what makes this a
    usable password check. That error is told apart from a damaged file: they used to be the
    same ``None``, so a corrupt keystore read to the user as "wrong password" forever.
    """
    if not os.path.exists(path):
        return None
    con = duckdb.connect()
    try:
        con.execute("ATTACH '%s' AS ks (READ_ONLY, ENCRYPTION_KEY '%s')" % (_sql(path), _check_key(kek)))
        row = con.execute("SELECT val FROM ks.k WHERE name = 'data_key'").fetchone()
        con.execute("DETACH ks")
        return _check_key(row[0]) if row else None
    except duckdb.Error as exc:
        low = str(exc).lower()
        if "wrong encryption key" in low or "aes tag" in low:
            raise WrongPassword("the password or recovery key did not open the keystore") from None
        raise CorruptKeystore(_redact(str(exc)[:300], kek)) from None
    finally:
        con.close()


def _open_wrap(kind: str, secret: str) -> tuple[str | None, str]:
    """``(data_key, reason)`` with reason one of ok | wrong-password | corrupt-keystore | not-set-up."""
    try:
        key = _load_wrapped(kind, secret)
    except WrongPassword:
        return None, "wrong-password"
    except CorruptKeystore as exc:
        log.error("the %s keystore file cannot be read: %s", kind, exc)
        return None, "corrupt-keystore"
    if key is None:
        return None, "not-set-up"
    return key, "ok"


def _sql(path: str) -> str:
    return path.replace("\\", "/").replace("'", "''")


# ---------------------------------------------------------------------------- public state

def configured() -> bool:
    """True once a password has been set up (a wrap file exists)."""
    return os.path.exists(_wrap_path("password"))


LEVEL_OFF = "off"                    # nothing encrypted
LEVEL_INDEX = "index"                # the index, its caches and the side caches
LEVEL_FULL = "full"                  # + the small JSON files, audit/history, logs, Backblaze
LEVELS = (LEVEL_OFF, LEVEL_INDEX, LEVEL_FULL)

_LEVEL_CACHE: tuple[tuple, str] | None = None     # ((mtime_ns, size, reader), level)


def level() -> str:
    """The user's INTENT: how much of the data should be encrypted.

    ``encryption_level`` is the setting. An install that predates it carries the old boolean
    ``encryption``, which reads as index-level when on - the only level that existed then.

    Reads the FILE's own keys (``raw_kopialens_settings``), never the defaults-merged view: the
    default for ``encryption_level`` is "off", and taking it as the answer would silently read
    every part-1 install's already-encrypted index as plaintext.

    Cached on the settings file's stat: every log line asks, and re-parsing the file each time
    cost two full reads per line. A write from any process changes the stat.
    """
    global _LEVEL_CACHE
    from ..settings import SETTINGS_FILE, raw_kopialens_settings
    try:
        st = os.stat(SETTINGS_FILE)
        stamp = (st.st_mtime_ns, st.st_size, raw_kopialens_settings)
    except OSError:
        stamp = (0, 0, raw_kopialens_settings)    # a replaced reader (tests) misses too
    cached = _LEVEL_CACHE
    if cached is not None and cached[0] == stamp:
        return cached[1]
    kl = raw_kopialens_settings()
    chosen = str(kl.get("encryption_level") or "").strip().lower()
    value = chosen if chosen in LEVELS else (LEVEL_INDEX if kl.get("encryption") else LEVEL_OFF)
    _LEVEL_CACHE = (stamp, value)
    return value


def enabled() -> bool:
    """The INDEX is covered: the level includes it and a keystore exists.

    This is not the state of any file on disk - generations follow their manifest and side caches
    follow their own footer - it is what a NEW index file should be written as.
    """
    return level() != LEVEL_OFF and configured()


def covers_files() -> bool:
    """The non-index layer is covered: small JSON files, audit/history, logs, policy backups,
    Backblaze state. ``core.filecrypto`` owns the actual work."""
    return level() == LEVEL_FULL and configured()


def unlocked() -> bool:
    with _LOCK:
        return _DATA_KEY is not None


def data_key() -> str | None:
    """The unlocked data key (base64), or None. Memory only - never write this anywhere."""
    with _LOCK:
        return _DATA_KEY


def lock() -> None:
    global _DATA_KEY, _SOURCE
    with _LOCK:
        _DATA_KEY = None
        _SOURCE = None
    from ..core import filecrypto
    filecrypto.forget_key()


def writes_encrypted() -> bool:
    """The state a NEW file should be written in: the build override when one is active, else
    the setting. A setting with no keystore cannot encrypt anything, so it reads as off."""
    override = _OVERRIDE.get()
    return bool(override) if override is not None else enabled()


@contextlib.contextmanager
def writing_state(encrypted: bool | None):
    """Force the state of files written inside this block (None = no override).

    The index build sets it from its previous manifest: a build must not flip a generation's
    state on its own, and while a switch is pending the setting says the opposite of the data.
    """
    token = _OVERRIDE.set(encrypted)
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


def _resolve(encrypted: bool | None) -> bool:
    """The one decision point for "does this file carry a key": an explicit state wins, then the
    override, then the setting. Asking for encryption while locked is REFUSED - falling back to
    plaintext here would write an unencrypted file into an encrypted generation, silently."""
    state = writes_encrypted() if encrypted is None else bool(encrypted)
    if state and not unlocked():
        raise StoreLocked("the data key is locked - refusing to read or write as if "
                          "encryption were off")
    return state


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
    existing data is a separate rewrite (``index.keyswitch``). Refuses to run over an existing
    keystore: a second setup would generate a different data key and orphan everything already
    encrypted with the first one.
    """
    global _DATA_KEY, _SOURCE
    with _LOCK:
        if configured():
            raise RuntimeError("a keystore already exists - use change_password or the recovery "
                               "key; creating a second one would make the existing data unreadable")
        salt = secrets.token_bytes(16)
        key = _new_key()
        _store_wrapped("password", _derive(password, salt), key)
        recovery = None
        if with_recovery:
            recovery = _new_key()
            _store_wrapped("recovery", recovery, key)
        _write_meta({"version": 1, "kdf": {"name": "argon2id", "salt": base64.b64encode(salt).decode(), **_KDF},
                     "has_recovery": bool(recovery)})
        _DATA_KEY, _SOURCE = key, "password"
        if remember:
            remember_on_this_machine(True)
        log.info("keystore created (recovery key %s)", "issued" if recovery else "not issued")
        return recovery


def _password_kek(password: str, kdf: dict | None = None) -> str | None:
    kdf = (_read_meta().get("kdf") or {}) if kdf is None else kdf
    salt_b64 = kdf.get("salt")
    if not salt_b64:
        return None
    try:
        return _derive(password, base64.b64decode(salt_b64))
    except ValueError:
        log.error("the keystore's KDF parameters are damaged")
        return None


def _retry_previous_wrap(password: str) -> str | None:
    """The data key opened with the PREVIOUS salt, when a password change was interrupted.

    ``_rewrap_password`` writes two files (the wrap and the salt in the meta); a crash between
    them leaves the current pair unable to open anything. The previous salt is kept in the
    meta and the old wrap is kept beside it until both writes land, so the OLD password still
    opens the keystore and the change can simply be retried. Caller holds ``_LOCK``.
    """
    prev_kdf = _read_meta().get("kdf_prev") or {}
    if not prev_kdf:
        return None
    prev_kek = _password_kek(password, prev_kdf)
    if prev_kek is None:
        return None
    wrap = _wrap_path("password")
    for path in (wrap, wrap + ".prev"):
        if not os.path.exists(path):
            continue
        try:
            key = _load_wrapped_path(path, prev_kek)
        except (WrongPassword, CorruptKeystore):
            continue
        if key:
            log.warning("an interrupted password change was recovered with the previous state")
            return key
    return None


def unlock_detailed(password: str) -> tuple[bool, str]:
    """Unlock by password. Returns ``(ok, reason)`` - the reason is what the UI says out loud:
    ok | wrong-password | corrupt-keystore | not-set-up."""
    global _DATA_KEY, _SOURCE
    with _LOCK:
        kek = _password_kek(password)
        if kek is None:
            return False, "not-set-up"
        key, reason = _open_wrap("password", kek)
        if key is None and reason == "wrong-password":
            key = _retry_previous_wrap(password)
            if key is not None:
                reason = "ok"
        if key is None:
            return False, reason
        _DATA_KEY, _SOURCE = key, "password"
        return True, "ok"


def unlock(password: str) -> bool:
    return unlock_detailed(password)[0]


def unlock_from_file() -> bool:
    """Unlock with the encryption password in the file ``KOPIALENS_KEY_FILE`` names (a Docker
    secret, for installs with no keychain). False when unset, unreadable or wrong."""
    path = (os.environ.get("KOPIALENS_KEY_FILE") or "").strip()
    if not path:
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            password = fh.read().strip()
    except OSError as exc:
        log.warning("the key file %s can't be read: %s", path, exc)
        return False
    ok, reason = unlock_detailed(password)
    if ok:
        log.info("index unlocked from the key file")
    else:
        log.warning("the key file didn't unlock the index (%s)", reason)
    return ok


def unlock_with_recovery_detailed(recovery_key: str) -> tuple[bool, str]:
    """Unlock with the recovery key. Same reasons as ``unlock_detailed``."""
    global _DATA_KEY, _SOURCE
    with _LOCK:
        try:
            key, reason = _open_wrap("recovery", recovery_key.strip())
        except (ValueError, TypeError):
            return False, "wrong-password"   # not even base64: a mistyped key, not a crash
        if key is None:
            return False, reason
        _DATA_KEY, _SOURCE = key, "recovery"
        return True, "ok"


def unlock_with_recovery(recovery_key: str) -> bool:
    return unlock_with_recovery_detailed(recovery_key)[0]


def _account() -> str:
    """The keychain entry name, scoped to THIS install's data folder.

    Unscoped, two installs on one machine (a desktop app and a Docker one, or a worktree pointed
    at another data folder) would share one entry: the second would unlock with the first's key
    and every read would fail with an AES tag mismatch. Installs that remembered the key before
    the scoping still work - ``unlock_from_keychain`` falls back to the bare name and re-saves
    under the scoped one.
    """
    digest = hashlib.sha256(keystore_dir().encode("utf-8")).hexdigest()[:12]
    return f"data-key:{digest}"


# The OS keychain is only ever used from one background thread at a time. On a Linux desktop
# without an open login keyring, a keychain call opens a password window and waits for a person;
# a request, startup, the nightly or Quit must never wait on that, and a second call must not
# open a second window. A call still waiting simply finishes (or fails) later.
_KC_LOCK = threading.Lock()
_KC_THREAD: threading.Thread | None = None
_KC_FAILED = False                    # the last keychain call raised (no keychain, refused, cancelled)
_REMEMBERED: bool | None = None       # last known: the data key is in the keychain


def _keychain_job(work, wait: float) -> bool:
    """Run ``work`` (keychain calls) on the keychain thread and wait up to ``wait`` seconds.
    True when it finished in time; False when it is still waiting or another call is."""
    global _KC_THREAD
    with _KC_LOCK:
        if _KC_THREAD is not None and _KC_THREAD.is_alive():
            return False

        def run():
            global _KC_FAILED
            try:
                work()
                _KC_FAILED = False
            except Exception as exc:  # noqa: BLE001 - no backend, locked keyring, DBus absent, cancelled
                _KC_FAILED = True
                log.info("keychain unavailable: %s", exc)

        _KC_THREAD = threading.Thread(target=run, name="keychain", daemon=True)
        _KC_THREAD.start()
        thread = _KC_THREAD
    thread.join(wait)
    return not thread.is_alive()


def _set_remembered(on: bool) -> None:
    global _REMEMBERED
    _REMEMBERED = on
    meta = _read_meta()
    if meta and meta.get("remembered") != on:
        meta["remembered"] = on
        _write_meta(meta)


def unlock_from_keychain(wait: float = 10.0) -> bool:
    """Unlock from the OS keychain ("remember on this machine"). False when nothing is stored, the
    keychain is unavailable, or it hasn't answered within ``wait`` seconds; a late answer still
    unlocks when it comes. Not tried when the key was never saved there."""
    if _read_meta().get("remembered") is False:
        return False

    def work():
        global _DATA_KEY, _SOURCE, _REMEMBERED
        import keyring
        key = keyring.get_password(_SERVICE, _account())
        if not key:
            key = keyring.get_password(_SERVICE, _ACCOUNT)
            if key:
                try:
                    keyring.set_password(_SERVICE, _account(), key)   # migrate to the scoped name
                except Exception:  # noqa: BLE001 - the read already worked; the write is a bonus
                    pass
        _REMEMBERED = bool(key)
        if not key:
            return
        try:
            checked = _check_key(key)
        except ValueError:                 # not a key: fall back to the pipe / a password
            log.warning("the keychain entry is not a usable key - ignoring it")
            return
        with _LOCK:
            if _DATA_KEY is None:
                _DATA_KEY, _SOURCE = checked, "keychain"
                log.info("index unlocked from the keychain")

    _keychain_job(work, wait)
    return unlocked()


def remember_on_this_machine(on: bool, wait: float = 10.0) -> str:
    """Store or clear the data key in the OS keychain: "on" / "off" when done, "waiting" while the
    keychain hasn't answered (it finishes in the background), "unavailable" when it refused."""
    key = data_key() if on else None
    if on and not key:
        return "unavailable"

    def work():
        import keyring
        if on:
            keyring.set_password(_SERVICE, _account(), key)
        else:
            for account in (_account(), _ACCOUNT):
                try:
                    keyring.delete_password(_SERVICE, account)
                except Exception:  # noqa: BLE001 - nothing stored is a successful clear
                    pass
        _set_remembered(on)

    if not _keychain_job(work, wait):
        return "waiting"
    return "unavailable" if _KC_FAILED else ("on" if on else "off")


def is_remembered() -> bool:
    """Whether the data key is in the keychain, as last known (never asks the keychain)."""
    return bool(_REMEMBERED)


def remember_state() -> str:
    """For the screens: "waiting" | "unavailable" | "on" | "off"."""
    if _KC_THREAD is not None and _KC_THREAD.is_alive():
        return "waiting"
    if _KC_FAILED:
        return "unavailable"
    return "on" if _REMEMBERED else "off"


# --------------------------------------------------------------- worker key handoff (stdin)

def adopt(key: str) -> bool:
    """Install a base64 data key handed over by the parent process (the worker's stdin pipe)."""
    global _DATA_KEY, _SOURCE
    with _LOCK:
        _DATA_KEY, _SOURCE = _check_key(key.strip()), "pipe"
        return True


def worker_payload() -> str:
    """The one line the app writes to a worker's stdin. JSON, newline-terminated by the caller.

    ``source`` matters: when the app itself unlocked from the keychain the worker should ask the
    keychain first too (a remembered install must not depend on the pipe), while a typed password
    gives the key over the pipe with the keychain only as a fallback. The key rides along either
    way so a worker never fails for want of one that the parent was holding.
    """
    with _LOCK:
        source = _SOURCE or ("keychain" if is_remembered() else "password")
        return json.dumps({"source": source, "key": _DATA_KEY})


def read_key_payload(flagged: bool, stream=None) -> dict | None:
    """The parent's hand-over line, read ONCE from stdin - and only when it flagged it.

    A manual ``python -m app.index.worker`` run has the console on stdin; reading it unasked
    would block the worker forever waiting for a line nobody is going to type.
    """
    if not flagged:
        return None
    import sys
    stream = stream if stream is not None else sys.stdin
    try:
        line = (stream.readline() or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable stdin is the same as no hand-over
        return None
    if not line:
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        log.warning("worker: the key hand-over line was not JSON; falling back to the keychain")
        return None
    return payload if isinstance(payload, dict) else None


def worker_key(payload: dict | None) -> bool:
    """Give a worker process the data key. True when it can read and write the store.

    Keychain first when the app unlocked from the keychain, pipe first otherwise, each with the
    other as fallback - both carriers hold the same data key, so the order only decides which one
    is asked first, never which key is used.
    """
    if not enabled():
        return True
    if unlocked():
        return True
    order = ("keychain", "pipe") if (payload or {}).get("source") == "keychain" else ("pipe", "keychain")
    for source in order:
        if source == "keychain":
            if unlock_from_keychain():
                return True
        else:
            handed = (payload or {}).get("key")
            if handed:
                try:
                    if adopt(handed):
                        return True
                except ValueError:
                    log.warning("worker: the handed-over key was not usable")
    return False


def _rewrap_password(key: str, new: str) -> None:
    """Write the password wrap of ``key`` under ``new`` with a fresh salt. Caller holds _LOCK.

    Two files move (the wrap and the salt in the meta); a crash between them would leave neither
    the old nor the new password able to open the keystore. So the previous wrap is copied beside
    the new one and its salt is kept in the meta until both writes land: the OLD password still
    opens the keystore through ``_retry_previous_wrap`` and the change is simply retried."""
    wrap = _wrap_path("password")
    prev = wrap + ".prev"
    old_kdf = dict(_read_meta().get("kdf") or {})
    if os.path.exists(wrap):
        tmp = prev + ".tmp"
        with open(wrap, "rb") as src, open(tmp, "wb") as dst:
            dst.write(src.read())
        atomicfile.replace(tmp, prev)
    meta = _read_meta()
    meta["kdf_prev"] = old_kdf
    _write_meta(meta)                      # crash here: the current pair still opens the old way
    salt = secrets.token_bytes(16)         # fresh salt on every change
    _store_wrapped("password", _derive(new, salt), key)
    meta = _read_meta()
    meta["kdf"] = {"name": "argon2id", "salt": base64.b64encode(salt).decode(), **_KDF}
    meta.pop("kdf_prev", None)
    _write_meta(meta)                      # both writes landed: the previous state is not needed
    try:
        os.remove(prev)
    except OSError:
        pass


def change_password_detailed(old: str, new: str) -> tuple[bool, str]:
    """Re-wrap the data key under a new password. Nothing on disk is re-encrypted.

    ``(ok, reason)`` so the caller can tell a wrong password from a damaged keystore. An
    interrupted earlier change is recovered here too: the old password opened ``.prev``, so
    retrying with it completes the change instead of reporting a wrong password.
    """
    with _LOCK:
        kek = _password_kek(old)
        if kek is None:
            return False, "not-set-up"
        key, reason = _open_wrap("password", kek)
        if key is None and reason == "wrong-password":
            key = _retry_previous_wrap(old)
        if key is None:
            return False, reason
        _rewrap_password(key, new)
        return True, "ok"


def change_password(old: str, new: str) -> bool:
    return change_password_detailed(old, new)[0]


def reset_password_with_recovery(recovery_key: str, new: str) -> tuple[bool, str]:
    """Forgot the password: set a new one with the recovery key. ``(ok, reason)``.

    Unwraps the data key with the recovery key and re-wraps it under ``new``; the recovery key
    itself keeps working. The old password stops working, which is the point.
    """
    with _LOCK:
        try:
            key, reason = _open_wrap("recovery", recovery_key.strip())
        except (ValueError, TypeError):
            return False, "wrong-password"
        if key is None:
            return False, reason
        _rewrap_password(key, new)
        log.info("password reset with the recovery key")
        return True, "ok"


def has_recovery() -> bool:
    """Whether a recovery key was issued for this keystore."""
    return bool(_read_meta().get("has_recovery")) and os.path.exists(_wrap_path("recovery"))


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

def parquet_read(path_sql: str, encrypted: bool | None = None) -> str:
    """``read_parquet(...)`` argument list carrying the encryption config when it applies.

    Every read of a store file goes through here or ``parquet_read_list`` so a new call site
    cannot silently omit the key and get "Computed AES tag differs" at runtime. Pass
    ``encrypted`` explicitly when the file's state comes from its manifest; None means "use the
    current setting or the build's override", which is right for files this process writes.
    """
    if _resolve(encrypted):
        return "'%s', encryption_config = {footer_key: '%s'}" % (path_sql, KEY_NAME)
    return "'%s'" % path_sql


def parquet_read_list(paths_sql: list[str], encrypted: bool | None = None) -> str:
    inner = ", ".join("'%s'" % p for p in paths_sql)
    if _resolve(encrypted):
        return "[%s], encryption_config = {footer_key: '%s'}" % (inner, KEY_NAME)
    return "[%s]" % inner


def copy_options(extra: str = "FORMAT parquet, COMPRESSION zstd",
                 encrypted: bool | None = None) -> str:
    """The ``COPY ... TO x (...)`` option list, with the encryption config when it applies."""
    if _resolve(encrypted):
        return "%s, ENCRYPTION_CONFIG {footer_key: '%s'}" % (extra, KEY_NAME)
    return extra


def file_is_encrypted(path: str) -> bool:
    """Whether a Parquet file carries the encrypted footer, read from its 4 magic bytes.

    No key needed, so a side cache's state is knowable while locked and a switch can tell what
    still needs rewriting. Any file too short to have a footer reads as plaintext.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(-4, os.SEEK_END)
            return fh.read(4) == _MAGIC_ENC
    except OSError:
        return False


def wrong_state(files: list[str], encrypted: bool) -> list[str]:
    """The files whose footer does not match ``encrypted`` - the leak check a switch runs before
    it publishes, and a build runs over its staged artifacts."""
    return [f for f in files if file_is_encrypted(f) != encrypted]


def cursor_search_path(session: _Session | None) -> str | None:
    """The search path a CURSOR needs to resolve views bound in the encrypted session database.

    A DuckDB cursor is a fresh connection: it does NOT inherit ``USE``, so an unqualified view
    name resolves in ``memory`` and every read fails with "Table with name cid does not exist".
    That is exactly what the store did before this existed - the read side of an encrypted
    install was broken end to end, and nobody had turned encryption on to find out.
    """
    return None if session is None or session.path is None else f"{SESSION_ALIAS}.main"


class _Session:
    """One connection's private temp folder, plus the encrypted database attached to it when
    encryption is on. Both are removed when the connection closes."""

    def __init__(self, folder: str, path: str | None = None):
        self.folder = folder
        self.path = path

    def cleanup(self) -> None:
        if self.path:
            for p in (self.path, self.path + ".wal"):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
        shutil.rmtree(self.folder, ignore_errors=True)   # anything still held goes at the startup sweep


def apply(con, temp_dir: str, memory_limit: str, threads: int | None,
          encrypted: bool | None = None) -> _Session:
    """Configure one DuckDB connection: limits, a private temp folder, and the encrypted session.

    The temp folder is a fresh subfolder of ``temp_dir`` for this connection alone: DuckDB names
    its spill files the same in every connection (``duckdb_temp_storage_S64K-0.tmp``), so two
    connections sharing a folder overwrite each other's ("Corrupt temporary file", or a crash).

    When the connection's files are encrypted this also attaches a fresh encrypted database and
    switches the connection to it, because an in-memory session spills PLAINTEXT to
    ``temp_directory`` even while every input is encrypted Parquet (P6).

    The returned session owns both; call ``cleanup`` after closing the connection.
    ``encrypted`` is explicit for readers bound to one manifest; None follows the setting.
    """
    key = _check_key(data_key()) if _resolve(encrypted) else None
    folder = os.path.join(temp_dir, "c%d_%s" % (os.getpid(), secrets.token_hex(6)))
    os.makedirs(folder, exist_ok=True)
    con.execute("SET temp_directory='%s'" % _sql(folder))
    con.execute("SET memory_limit='%s'" % memory_limit)
    if threads:               # None keeps the caller's own thread setting (the BZ store)
        con.execute("SET threads=%d" % threads)
    # Rule 1 of the store: results stay in DuckDB until the final small answer, and nothing
    # depends on input order. Insertion order costs memory in every hash aggregate.
    con.execute("SET preserve_insertion_order=false")
    session = _Session(folder)
    if key is None:
        return session
    path = session.path = os.path.join(folder, "session.db")
    try:
        con.execute("ATTACH '%s' AS %s (ENCRYPTION_KEY '%s')" % (_sql(path), SESSION_ALIAS, key))
        con.execute("USE %s" % SESSION_ALIAS)
        con.execute("SET temp_file_encryption=true")
        con.execute("PRAGMA add_parquet_key('%s', '%s')" % (KEY_NAME, key))
    except Exception as exc:  # noqa: BLE001 - re-raised sanitized: DuckDB error text can echo the key
        session.cleanup()
        raise RuntimeError(_redact(str(exc), key)[:300]) from None
    return session


def open_scratch(path: str, temp_dir: str, memory_limit: str, threads: int,
                 encrypted: bool | None = None):
    """A file-backed scratch database for the index build, encrypted when the build is.

    Not ``duckdb.connect(path)``: DuckDB takes the encryption key on ATTACH, so the scratch is
    attached and made current instead. That matters more than it looks - the build's scratch holds
    the whole tree, i.e. every path in the repository, until the build finishes and deletes it.

    Registering the Parquet key here is the other half. Without it a ``COPY ... TO`` writes a
    PLAINTEXT Parquet file into an otherwise encrypted generation, and nothing complains: the file
    is perfectly readable, just not encrypted.

    The caller owns ``path`` and deletes it, so no ``_Session`` is returned.
    """
    os.makedirs(temp_dir, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET temp_directory='%s'" % _sql(temp_dir))
    con.execute("SET memory_limit='%s'" % memory_limit)
    con.execute("SET threads=%d" % threads)
    # Rule 1 of the store: nothing depends on input order, and insertion order costs memory in
    # every hash aggregate. Every ordering the build relies on is an explicit ORDER BY.
    con.execute("SET preserve_insertion_order=false")
    on = _resolve(encrypted)
    key = _check_key(data_key()) if on else None
    try:
        con.execute("ATTACH '%s' AS build%s"
                    % (_sql(path), (" (ENCRYPTION_KEY '%s')" % key) if on else ""))
        con.execute("USE build")
        if on:
            con.execute("SET temp_file_encryption=true")
            con.execute("PRAGMA add_parquet_key('%s', '%s')" % (KEY_NAME, key))
    except Exception as exc:  # noqa: BLE001 - re-raised sanitized: DuckDB error text can echo the key
        con.close()
        raise RuntimeError(_redact(str(exc), key or "")[:300]) from None
    return con


def register_key(con, encrypted: bool | None = None) -> None:
    """Register the Parquet key on a plain connection that only reads or writes store files.

    ``copy_options`` / ``parquet_read`` name the key; only this puts it on the connection. A
    connection that has one without the other fails at run time with "Computed AES tag differs" or
    an unknown-key error, which is a confusing place to learn the key was never registered.
    """
    if _resolve(encrypted):
        con.execute("PRAGMA add_parquet_key('%s', '%s')" % (KEY_NAME, _check_key(data_key())))


_SESSION_PID = re.compile(r"^(?:session_|c)(\d+)_")
_PRIVATE_DIR = re.compile(r"^c\d+_[0-9a-f]+$")


def _owner_alive(fn: str) -> bool:
    """The session file's or private folder's process (its pid is in the name) is still running.
    POSIX lets a live file be unlinked, so a worker the app took over at startup would lose it."""
    m = _SESSION_PID.match(fn)
    if not m or int(m.group(1)) == os.getpid():
        return False
    try:
        import psutil
        return psutil.pid_exists(int(m.group(1)))
    except Exception:  # noqa: BLE001 - cannot tell: keep the file
        return True


def sweep_sessions(temp_dir: str) -> int:
    """Remove session databases and private temp folders a crashed process left behind. Returns
    how many went."""
    gone = 0
    try:
        for fn in os.listdir(temp_dir):
            path = os.path.join(temp_dir, fn)
            if fn.startswith("session_") and (fn.endswith(".db") or fn.endswith(".db.wal")):
                if _owner_alive(fn):
                    continue
                try:
                    os.remove(path)
                    gone += 1
                except OSError:
                    pass               # still held by a live process
            elif _PRIVATE_DIR.match(fn) and os.path.isdir(path) and not _owner_alive(fn):
                shutil.rmtree(path, ignore_errors=True)
                gone += not os.path.exists(path)
    except OSError:
        pass
    return gone


def temp_dirs() -> list[str]:
    """Every directory a connection may have left a session database or private temp folder in:
    the read temp dirs (``data/duck_tmp_*``), each repo's staging tmp and the Backblaze store's."""
    out = []
    try:
        for name in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, name)
            if name.startswith("duck_tmp") and os.path.isdir(path):
                out.append(path)
            staging = os.path.join(path, "_staging", "tmp")
            if name.startswith("repo") and os.path.isdir(staging):
                out.append(staging)
            if name == "bz" and os.path.isdir(os.path.join(path, "_tmp")):
                out.append(os.path.join(path, "_tmp"))
    except OSError:
        pass
    return out


def sweep_all() -> int:
    """Startup hygiene: remove the session databases and temp folders a crashed process left behind.

    They are encrypted when encryption is on, but they accumulate unboundedly - a crash leaves
    one file per connection, and nothing else ever looks in those directories.
    """
    gone = sum(sweep_sessions(d) for d in temp_dirs())
    if gone:
        log.info("removed %d leftover session database(s) or temp folder(s)", gone)
    return gone
