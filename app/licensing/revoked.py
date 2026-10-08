"""The signed list of revoked licence ids, fetched weekly from the licence service.

A refunded or leaked licence stops working between releases. The list is a PASETO signed by a
service key this build trusts, carrying {"product", "revoked": [ids], "iat"}; the newest list seen
is kept. Offline, or with no list published yet, nothing changes: this never locks anyone out by
failing.

The fetch is also the weekly licence check-in: an install holding licences sends them with its
licensing install id, so the service can count installs per licence. Free installs send nothing.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

from config import APP_VERSION, DATA_DIR, LICENCE_SERVICE_URL

from ..core.applog import get_logger
from .keys import PRODUCT, PUBLIC_KEYS

log = get_logger("licence")

LIST_FILE = os.path.join(DATA_DIR, "licence", "revoked.json")
REFRESH_EVERY = 7 * 24 * 3600
RETRY_AFTER_FAILURE = 24 * 3600

_cache: dict = {"mtime": None, "ids": frozenset()}


def _decode(token: str) -> dict | None:
    """The verified payload, or None when the signature, product or shape is wrong."""
    try:
        import pyseto

        from .licensekit.verify import footer_kid, load_public_keys, parse_time
        key = load_public_keys(PUBLIC_KEYS).get(footer_kid(token) or "")
        if key is None:
            return None
        data = json.loads(pyseto.decode(key, token).payload)
        ids = data.get("revoked")
        if data.get("product") != PRODUCT or not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return None
        data["iat_ts"] = parse_time(data.get("iat")).timestamp()
        return data
    except Exception:  # noqa: BLE001 - anything unreadable is simply not a list
        return None


def _read() -> dict:
    try:
        with open(LIST_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(LIST_FILE), exist_ok=True)
    tmp = LIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, LIST_FILE)


def banned() -> frozenset:
    """Revoked ids from the stored list, re-verified whenever the file changes."""
    try:
        mtime = os.path.getmtime(LIST_FILE)
    except OSError:
        return frozenset()
    if _cache["mtime"] != mtime:
        payload = _decode(_read().get("token") or "")
        _cache.update(mtime=mtime, ids=frozenset(payload["revoked"]) if payload else frozenset())
    return _cache["ids"]


def last_check() -> str | None:
    """When the list was last fetched (the weekly check-in), as ISO time."""
    at = _read().get("fetched_at")
    if not at:
        return None
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")


def _request() -> urllib.request.Request:
    """A plain GET for Free installs; a check-in POST carrying the held licences otherwise."""
    url = f"{LICENCE_SERVICE_URL.rstrip('/')}/v1/revoked"
    headers = {"User-Agent": f"KopiaLens/{APP_VERSION}"}
    try:
        from . import licence
        tokens, install = licence.held_tokens(), licence._install_id()
    except Exception:  # noqa: BLE001 - the list itself matters more than the check-in
        tokens, install = [], ""
    if not tokens:
        return urllib.request.Request(f"{url}?product={PRODUCT}", headers=headers)
    body = {"product": PRODUCT, "licences": tokens}
    if install:
        body["install"] = install
    return urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                  headers={**headers, "Content-Type": "application/json"})


def refresh() -> bool:
    """Fetch the list once. True when a newer, valid list was stored."""
    stored = _read()
    now = time.time()
    try:
        with urllib.request.urlopen(_request(), timeout=10) as resp:
            token = (json.loads(resp.read(262144)) or {}).get("token") or ""
    except Exception as exc:  # noqa: BLE001 - offline, or no list published yet
        log.debug("revoked list not fetched: %s", exc)
        _write({**stored, "retry_at": now + RETRY_AFTER_FAILURE})
        return False
    payload = _decode(token)
    if payload is None:
        log.warning("revoked list ignored: not signed by a trusted key")
        _write({**stored, "retry_at": now + RETRY_AFTER_FAILURE})
        return False
    old = _decode(stored.get("token") or "")
    newer = old is None or payload["iat_ts"] > old["iat_ts"]
    _write({"token": token if newer else stored.get("token"), "fetched_at": now, "retry_at": 0})
    if newer:
        from . import licence
        licence._forget()
        log.info("revoked list updated: %d id(s)", len(payload["revoked"]))
    return newer


def refresh_if_due() -> None:
    stored = _read()
    now = time.time()
    if now - (stored.get("fetched_at") or 0) >= REFRESH_EVERY and now >= (stored.get("retry_at") or 0):
        refresh()
