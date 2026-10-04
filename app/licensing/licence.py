"""The stored licence: read, add, activate online, Patreon sign-in, quiet renewal.

Every failure leaves the current licence untouched; the worst case is Free, never locked out.
The licence lives in <data>/licence/license.key (LicenseKit's own format); online.json beside it
keeps what renewal needs (the store key and activation id, or the Patreon link)."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

from config import APP_VERSION, DATA_DIR, DEMO_INSTANCE, LICENCE_SERVICE_URL

from ..core.applog import get_logger
from .keys import BANNED_IDS, PRODUCT, PUBLIC_KEYS

log = get_logger("licence")

LICENCE_DIR = os.path.join(DATA_DIR, "licence")
META_FILE = "online.json"
SHARED_DIR = Path.home() / ".licensekit"   # one bundle licence serving several apps
CACHE_SECONDS = 60           # expiry is re-checked at least this often
RENEW_FIRST_DELAY = 120      # after startup
RENEW_EVERY = 6 * 3600

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "lic": None}


class LicenceError(Exception):
    """A licence action that did not happen; the message is shown as is."""


class _NoKit:
    """Stands in when the licence library can't load (the web demo): plain Free."""
    state = None
    reason = ""
    renew_due = False

    def has(self, _capability: str) -> bool:
        return False


_NO_KIT = _NoKit()


def _app_major() -> int | None:
    try:
        return int(APP_VERSION.split(".")[0])
    except (ValueError, IndexError):
        return None


def _kit():
    try:
        from .licensekit import LicenseKit
    except Exception as exc:  # noqa: BLE001 - a missing crypto library means Free, not a crash
        log.warning(f"licence library unavailable: {exc}")
        return None
    return LicenseKit(product=PRODUCT, public_keys=PUBLIC_KEYS, banned_ids=BANNED_IDS,
                      app_major=_app_major(), app_dir=LICENCE_DIR, shared_dir=SHARED_DIR)


def _service():
    try:
        from .licensekit import LicenseService
    except Exception as exc:  # noqa: BLE001
        raise LicenceError("Licences can't be checked in this copy of KopiaLens.") from exc
    return LicenseService(LICENCE_SERVICE_URL, PRODUCT, user_agent=f"KopiaLens/{APP_VERSION}")


def current():
    """The best stored licence (cached briefly). Never raises."""
    now = time.monotonic()
    with _lock:
        if _cache["lic"] is not None and now - _cache["at"] < CACHE_SECONDS:
            return _cache["lic"]
    kit = _kit()
    try:
        lic = kit.load() if kit else _NO_KIT
    except Exception:  # noqa: BLE001
        lic = _NO_KIT
    with _lock:
        _cache.update(at=now, lic=lic)
    return lic


def _forget() -> None:
    with _lock:
        _cache.update(at=0.0, lic=None)


def _read_meta() -> dict:
    try:
        with open(os.path.join(LICENCE_DIR, META_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(meta: dict) -> None:
    os.makedirs(LICENCE_DIR, exist_ok=True)
    path = os.path.join(LICENCE_DIR, META_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp, path)


def _clear_meta() -> None:
    try:
        os.remove(os.path.join(LICENCE_DIR, META_FILE))
    except OSError:
        pass


def _iso(dt) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def status() -> dict:
    lic = current()
    state = lic.state.value if lic.state is not None else "none"
    out = {"state": state, "reason": getattr(lic, "reason", "")}
    if state != "none" and lic.state is not None:
        out.update({
            "id": lic.id, "plan": lic.plan, "name": lic.name, "email": lic.email,
            "expires": _iso(lic.expires), "renewable": lic.renewable, "source": lic.source,
            "origin": lic.origin, "stored": lic.origin == "app folder",
        })
        meta = _read_meta()
        if lic.source == "patreon" and meta.get("licence_id") == lic.id and meta.get("threshold_cents"):
            out["patreon"] = {"support_cents": int(meta.get("support_cents") or 0),
                              "threshold_cents": int(meta["threshold_cents"])}
    return out


def _save(token: str, meta: dict):
    """Verify and store a licence; only an active (or older-version) licence is kept."""
    kit = _kit()
    if kit is None:
        raise LicenceError("Licences can't be checked in this copy of KopiaLens.")
    lic = kit.verify(token)
    if lic.state.value == "expired":
        raise LicenceError(f"This licence ended on {lic.expires:%d %b %Y}.")
    if lic.state.value not in ("active", "not_covered"):
        raise LicenceError(_REASONS.get(lic.reason, "That isn't a valid KopiaLens licence."))
    kit.save(token)
    if meta:
        _write_meta({**meta, "licence_id": lic.id})
    else:
        _clear_meta()
    _forget()
    log.info(f"licence {lic.id} stored ({lic.plan})")
    return lic


_REASONS = {
    "licence is for another product": "That licence is for another app.",
    "licence revoked": "That licence was withdrawn. Contact support if you think this is wrong.",
}


def add(text: str):
    """A pasted licence or the content of a .license file."""
    if not (text or "").strip():
        raise LicenceError("Paste a licence or choose a licence file.")
    return _save(text, {})


def activate(key: str):
    """Exchange a store key for a licence (once, online). A pasted licence is stored as is."""
    key = (key or "").strip()
    if not key:
        raise LicenceError("Enter your licence key.")
    if "v4.public." in key:
        return add(key)
    res = _service().activate(key, label=socket.gethostname()[:100])
    if not res.ok:
        raise LicenceError(res.message or "Couldn't activate that key.")
    return _save(res.licence, {"key": key, "activation_id": res.activation_id})


def patreon_start() -> dict:
    url, session = _service().patreon_start()
    return {"url": url, "session": session}


def patreon_claim(session: str) -> dict:
    """{"pending": True} until the browser sign-in finishes, then the stored licence."""
    res = _service().patreon_claim(session)
    if res.pending:
        return {"pending": True}
    if not res.ok:
        raise LicenceError(res.message or "Couldn't sign in with Patreon.")
    _save(res.licence, {"patreon": True, **_progress(res.extra)})
    return {"pending": False}


def _progress(extra: dict) -> dict:
    try:
        return {"support_cents": int(extra.get("support_cents") or 0),
                "threshold_cents": int(extra.get("lifetime_threshold_cents") or 0)}
    except (TypeError, ValueError):
        return {}


def remove() -> None:
    kit = _kit()
    if kit is not None:
        kit.clear()
    _clear_meta()
    _forget()
    log.info("licence removed")


def renew_if_due() -> bool:
    """Renew a renewable licence close to its end. True when a new licence was stored."""
    lic = current()
    if not getattr(lic, "renew_due", False):
        return False
    meta = _read_meta()
    mine = meta.get("licence_id") == lic.id
    res = _service().renew(lic.token, key=meta.get("key", "") if mine else "",
                           activation_id=meta.get("activation_id", "") if mine else "")
    if not res.ok:
        log.info(f"licence renewal not done: {res.error or res.status} {res.message}")
        return False
    keep = {k: v for k, v in meta.items() if k in ("key", "activation_id", "patreon")} if mine else {}
    try:
        _save(res.licence, {**keep, **_progress(res.extra)} if keep.get("patreon") else keep)
    except LicenceError as exc:
        log.warning(f"renewed licence not stored: {exc}")
        return False
    return True


def _renew_loop() -> None:
    time.sleep(RENEW_FIRST_DELAY)
    while True:
        try:
            renew_if_due()
        except Exception as exc:  # noqa: BLE001 - renewal is best-effort
            log.warning(f"licence renewal failed: {exc}")
        time.sleep(RENEW_EVERY)


def start_renewal() -> None:
    """Background renewal for subscription and Patreon licences. Not in demos or the browser."""
    if DEMO_INSTANCE or sys.platform == "emscripten":
        return
    threading.Thread(target=_renew_loop, name="licence-renewal", daemon=True).start()
