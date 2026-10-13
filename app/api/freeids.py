"""Free installs get no object ids: an object id is the ingredient a content delete (Pro) is built
from. These trims run on Free replies only; Pro replies are unchanged. Snapshot ids stay (Free
deletes, restores and mounts whole snapshots). 
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from ..licensing import features

_HELD: dict[str, tuple[str, float]] = {}
_SALT = secrets.token_bytes(16)
_LOCK = threading.Lock()
_HOLD_SECONDS = 86400
PREFIX = "kl-version-"


def free(repo: str) -> bool:
    return not features.allowed("remove", repo)


def same_key(obj: str | None) -> str | None:
    """Equal for equal objects (identical snapshots group by it), but not the object id."""
    if not obj:
        return None
    return hmac.new(_SALT, obj.encode(), hashlib.sha256).hexdigest()[:12]


def contents(versions: list[dict]) -> int:
    """How many different contents a file's versions hold (the panel's "Changed N times")."""
    return len({v.get("content_id") for v in versions})


def versions(repo: str, rows: list[dict]) -> list[dict]:
    if free(repo):
        for v in rows:
            v.pop("content_id", None)
    return rows


def detail(repo: str, d: dict) -> dict:
    """File panel detail: no ids; storage pieces numbered like ``breakdown`` numbers them."""
    if not free(repo) or not d.get("found"):
        return d
    d.pop("content_id", None)
    versions(repo, d.get("versions") or [])
    for i, ck in enumerate(d.get("chunks") or []):
        ck["cid"] = f"#{i + 1}"
    return d


def breakdown(repo: str, b: dict) -> dict:
    if not free(repo) or not b.get("found"):
        return b
    b["pieces"] = {f"#{p['n']}": {k: v for k, v in p.items() if k != "n"}
                   for p in (b.get("pieces") or {}).values()}
    return b


def hold(obj: str) -> str:
    """A restore source that is a bare object (its snapshot expired): a handle the restore job
    turns back into the object, so the id never reaches a Free browser."""
    token = PREFIX + secrets.token_hex(8)
    now = time.time()
    with _LOCK:
        for k in [k for k, (_o, at) in _HELD.items() if now - at > _HOLD_SECONDS]:
            _HELD.pop(k, None)
        _HELD[token] = (obj, now)
    return token


def release(source_id: str) -> str | None:
    """The object behind a handle; None for an unknown or expired one. Other ids pass through."""
    if not source_id.startswith(PREFIX):
        return source_id
    with _LOCK:
        held = _HELD.get(source_id)
    return held[0] if held else None
