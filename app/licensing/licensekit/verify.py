"""Pure licence verification: token + keys + clock -> Licence. Never raises."""
import base64
import json
import re
from datetime import datetime, timedelta, timezone

import pyseto
from pyseto import Key

from .model import Licence, State

FORMAT_VERSION = 1
GRACE = timedelta(days=7)  # renewable licences only: covers payment retries and outages
RENEW_WINDOW = timedelta(days=10)
HEADER = "v4.public."
MAX_TOKEN_LEN = 8192


def normalise(text) -> str:
    """Strip what pasting adds: BOM, whitespace, line breaks, '#' comment lines."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return ""
    lines = [ln for ln in text.replace("﻿", "").splitlines() if not ln.lstrip().startswith("#")]
    return re.sub(r"\s+", "", "".join(lines))


def load_public_keys(mapping) -> dict:
    """{kid: 'k4.public...'} -> {kid: Key}. Entries that do not parse are skipped."""
    keys = {}
    for kid, value in (mapping or {}).items():
        try:
            key = Key.from_paserk(value)
            if key.version == 4 and key.purpose == "public":
                keys[str(kid)] = key
        except Exception:
            continue
    return keys


def footer_kid(token: str):
    parts = token.split(".")
    if len(parts) != 4:
        return None
    raw = base64.urlsafe_b64decode(parts[3] + "=" * (-len(parts[3]) % 4))
    data = json.loads(raw)
    kid = data.get("kid") if isinstance(data, dict) else None
    return kid if isinstance(kid, str) else None


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("time must be a string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("time needs a timezone")
    return dt.astimezone(timezone.utc)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str(data, name, required=True) -> str:
    value = data.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value):
        raise ValueError(name)
    return value


def verify(text, *, product: str, keys: dict, banned=frozenset(), app_major=None, now=None, origin="") -> Licence:
    """Check a licence for one product. Any failure gives INVALID; never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return _verify(text, product, keys, banned, app_major, now, origin)
    except Exception:
        return Licence(State.INVALID, reason="unreadable", origin=origin)


def _verify(text, product, keys, banned, app_major, now, origin) -> Licence:
    token = normalise(text)
    if not token:
        return Licence(State.NONE, origin=origin)
    if len(token) > MAX_TOKEN_LEN or not token.startswith(HEADER):
        return Licence(State.INVALID, reason="not a licence", origin=origin)

    try:
        kid = footer_kid(token)
    except Exception:
        kid = None
    key = keys.get(kid) if kid else None
    if key is None:
        return Licence(State.INVALID, reason="unknown signing key", origin=origin)

    try:
        payload = pyseto.decode(key, token).payload  # signature only; claims checked below
    except Exception:
        return Licence(State.INVALID, reason="signature check failed", origin=origin)

    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("v") != FORMAT_VERSION or not _is_int(data.get("v")):
            raise ValueError("v")
        lic_id = _str(data, "id")
        name = _str(data, "to")
        email = _str(data, "em", required=False)
        plan = _str(data, "plan")
        source = _str(data, "src")
        products = data.get("p")
        if not isinstance(products, dict) or not all(
            isinstance(k, str) and isinstance(v, list) and all(isinstance(c, str) for c in v)
            for k, v in products.items()
        ):
            raise ValueError("p")
        issued = parse_time(data.get("iat"))
        expires = parse_time(data["exp"]) if data.get("exp") is not None else None
        upd = data.get("upd")
        if upd is not None and not (_is_int(upd) and upd >= 0):
            raise ValueError("upd")
        renewable = data.get("rn", False)
        if not isinstance(renewable, bool):
            raise ValueError("rn")
    except Exception:
        return Licence(State.INVALID, reason="unsupported licence format", origin=origin)

    if product not in products:
        return Licence(State.INVALID, reason="licence is for another product", origin=origin)

    base = dict(
        id=lic_id,
        name=name,
        email=email,
        plan=plan,
        capabilities=frozenset(products[product]),
        issued=issued,
        expires=expires,
        updates_through=upd,
        renewable=renewable,
        source=source,
        origin=origin,
        token=token,
    )
    if lic_id in banned:
        return Licence(State.BANNED, reason="licence revoked", **base)
    if expires is not None and now >= expires + (GRACE if renewable else timedelta(0)):
        return Licence(State.EXPIRED, reason="licence ended", **base)
    if upd is not None and app_major is not None and app_major > upd:
        return Licence(State.NOT_COVERED, reason=f"licence covers up to version {upd}.x", **base)
    renew_due = bool(renewable and expires is not None and expires - now <= RENEW_WINDOW)
    return Licence(State.ACTIVE, renew_due=renew_due, **base)
