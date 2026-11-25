"""Account store: the list of accounts behind sign-in.

v1 holds one admin account; the store is a list so more accounts (and roles) are an addition, not
a rewrite. Passwords are hashed by ``core/auth.py``; this module only stores and finds records.

``users.json`` sits next to ``settings.json`` and stays PLAIN at every encryption level: sign-in
happens before the index can be unlocked. It holds hashes, never passwords, plus the random secret
that signs session cookies - which is why it is a separate file from settings (whose content is
sent to the browser).

Record: ``username``, ``password_hash``, ``role``, ``epoch`` (bumped by "sign out all devices"),
``unlocks_encryption`` (this account's password is also the encryption password), ``created``.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import time

from config import CONFIG_DIR

from ..settings import mutate_settings, raw_kopialens_settings
from .applog import get_logger

log = get_logger("users")

USERS_FILE = os.path.join(CONFIG_DIR, "users.json")
ROLE_ADMIN = "admin"
_USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_LOCK = threading.RLock()
_CACHE: dict = {"mtime": None, "data": None}
_MIGRATED = False


def _path() -> str:
    return USERS_FILE


def _empty() -> dict:
    return {"version": 1, "secret": secrets.token_hex(32), "users": []}


def _read() -> dict:
    """The file's content, cached by mtime (sign-in checks read this on every request)."""
    path = _path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except FileNotFoundError:            # no file yet: one stable empty store until the first write
        with _LOCK:
            if _CACHE["data"] is None or _CACHE["mtime"] is not None:
                _CACHE.update(mtime=None, data=_empty())
            return _CACHE["data"]
    except OSError as exc:
        return _last_good(exc)
    with _LOCK:
        if _CACHE["mtime"] == mtime and _CACHE["data"] is not None:
            return _CACHE["data"]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as exc:           # transient (a lock held by antivirus or backup): never cached
            return _last_good(exc)
        except ValueError as exc:
            # Damaged: put back the newest good backup rather than silently switching sign-in off.
            from . import configbackup
            log.error("users.json unreadable (%s)", exc)
            data = _empty()
            if configbackup.recover("users.json"):
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    mtime = os.stat(path).st_mtime_ns
                except (OSError, ValueError):
                    data = _empty()
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            data = _empty()
        _CACHE.update(mtime=mtime, data=data)
        return data


def _last_good(exc: OSError) -> dict:
    """A failed read keeps the last good content so a momentary lock cannot switch sign-in off."""
    log.warning("users.json could not be read (%s); using the last good copy", exc)
    with _LOCK:
        if _CACHE["data"] is not None and _CACHE["mtime"] is not None:
            return _CACHE["data"]
    return _empty()


def _write(data: dict) -> None:
    from .dataupgrade import atomic_json
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_json(path, data)
    _CACHE.update(mtime=os.stat(path).st_mtime_ns, data=data)
    from . import configbackup
    configbackup.remember("users.json")


def _mutate(fn) -> dict:
    from . import configbackup
    from .filelock import locked
    with _LOCK, locked(_path()):
        configbackup.before_write()
        data = copy.deepcopy(_read())
        if not data.get("secret"):
            data["secret"] = secrets.token_hex(32)
        result = fn(data)
        _write(data)
        return result


def accounts() -> list[dict]:
    ensure_migrated()
    return [dict(u) for u in _read().get("users", [])]


def has_accounts() -> bool:
    return bool(accounts())


def find(username: str) -> dict | None:
    """Usernames match case-insensitively."""
    key = (username or "").strip().casefold()
    return next((u for u in accounts() if u.get("username", "").casefold() == key), None) if key else None


def primary() -> dict | None:
    """The account the single-user UI acts on: the first admin."""
    return next((u for u in accounts() if u.get("role") == ROLE_ADMIN), None)


def secret() -> bytes:
    """Signs session cookies. Created with the file and kept for its life."""
    data = _read()
    if not data.get("secret"):
        _mutate(lambda d: None)
        data = _read()
    return bytes.fromhex(data["secret"])


def username_problem(name: str) -> str:
    """Why a username cannot be used, or "" when it can."""
    if not _USERNAME.match(name or ""):
        return "Use 1-32 letters, numbers, dots, dashes or underscores."
    return ""


def create(username: str, password_hash: str, role: str = ROLE_ADMIN,
           unlocks_encryption: bool = False) -> dict:
    record = {"username": username.strip(), "password_hash": password_hash, "role": role,
              "epoch": 0, "unlocks_encryption": bool(unlocks_encryption), "created": time.time()}

    def _add(data: dict) -> dict:
        if any(u.get("username", "").casefold() == record["username"].casefold() for u in data["users"]):
            raise ValueError("That username is taken.")
        data["users"].append(record)
        return dict(record)
    return _mutate(_add)


def _update(who: str, /, **fields) -> dict:
    key = who.casefold()

    def _apply(data: dict) -> dict:
        for u in data["users"]:
            if u.get("username", "").casefold() == key:
                u.update(fields)
                return dict(u)
        raise KeyError(who)
    return _mutate(_apply)


def set_password_hash(username: str, password_hash: str) -> dict:
    """A new hash also invalidates every session of this account (the hash is part of the MAC)."""
    return _update(username, password_hash=password_hash)


def rename(username: str, new_name: str) -> dict:
    other = find(new_name)
    if other and other["username"].casefold() != username.casefold():
        raise ValueError("That username is taken.")
    return _update(username, username=new_name.strip())


def bump_epoch(username: str) -> dict:
    user = find(username)
    if user is None:
        raise KeyError(username)
    return _update(username, epoch=int(user.get("epoch", 0)) + 1)


def set_unlocks_encryption(username: str, on: bool) -> dict:
    return _update(username, unlocks_encryption=bool(on))


def migrate_legacy() -> dict:
    """Move the old single shared password (``settings.kopialens.auth_password_hash``) into an
    ``admin`` account. Idempotent; the data-folder step and the first read both call it."""
    global _MIGRATED
    with _LOCK:
        legacy = str(raw_kopialens_settings().get("auth_password_hash") or "")
        moved = False
        if legacy and not _read().get("users"):
            _mutate(lambda d: d["users"].append({
                "username": "admin", "password_hash": legacy, "role": ROLE_ADMIN, "epoch": 0,
                "unlocks_encryption": False, "created": time.time()}))
            moved = True
        if legacy:
            mutate_settings(lambda s: s.setdefault("kopialens", {}).pop("auth_password_hash", None))
        _MIGRATED = True
    if moved:
        log.info("the shared password moved to the account 'admin' in users.json")
    return {"moved": moved}


def ensure_migrated() -> None:
    if not _MIGRATED:
        try:
            migrate_legacy()
        except Exception as exc:  # noqa: BLE001 - a failed move retries on the next read
            log.error("moving the shared password into users.json failed: %s", exc)
