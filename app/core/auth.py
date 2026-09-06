"""Single shared-password access control (REL2, "Option A").

Shape, deliberately small: no user accounts, no roles, no session store. One password guards the
whole app; a correct login sets a signed cookie derived from the stored password hash, so changing
the password invalidates every existing session and nothing has to be persisted server-side.

Auth is on BECAUSE a password exists, not the other way round: a fresh install has no password and
no lockout. The middleware only guards once a hash is stored and auth is not explicitly disabled.
Choosing the password belongs to onboarding (); until then Settings carries the control, and
KOPIALENS_PASSWORD can seed one so a headless/Docker image is provisionable without a browser.

Two independent protections live here:

* the password itself, for anyone who can reach the port;
* Host-header validation, against DNS rebinding - a website the user visits cannot be allowed to
  script requests at this app just because it is reachable on their network. IP literals and
  localhost are always accepted (rebinding needs a DOMAIN name to point at a private address);
  any other hostname must be listed in ``allowed_hosts``.

The bind default is PER INSTALL PROFILE: the tray declares its child "desktop" (loopback);
anything else - bare python, systemd, Docker - is a server (all interfaces, the container
convention: the user's port mapping decides exposure, not the app's bind). Env beats an explicit
settings value, which beats the profile.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets

from ..settings import load_settings, mutate_settings, raw_kopialens_settings

COOKIE_NAME = "kopialens_session"
_ITERATIONS = 200_000
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", ""}
# Multi-user groundwork (2026-09-06): today there is exactly one identity. Everything that will
# need a user - the session subject, the audit actor - goes through here, so adding a real user
# store later changes this function and nothing else.
SUBJECT = "admin"


def _kl() -> dict:
    try:
        return load_settings().get("kopialens", {}) or {}
    except Exception:  # noqa: BLE001 - an unreadable settings file must not lock the app open
        return {}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def profile() -> str:
    """How this install was launched: the tray declares "desktop" for its child; anything else is
    a server. A fact about the launch, not a heuristic."""
    return "desktop" if _env("KOPIALENS_PROFILE").lower() == "desktop" else "server"


def enabled() -> bool:
    """Auth guards only when a password was actually chosen AND has not been explicitly disabled -
    a fresh install (no password yet) is never locked out."""
    return bool(_kl().get("auth_enabled", True)) and bool(password_hash())


def password_hash() -> str:
    return str(_kl().get("auth_password_hash") or "")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iters))
    except Exception:  # noqa: BLE001 - a malformed stored hash is simply not a match
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def set_password(password: str) -> None:
    stored = hash_password(password)

    def _apply(settings: dict) -> None:
        settings.setdefault("kopialens", {})["auth_password_hash"] = stored
        settings["kopialens"]["auth_enabled"] = True

    mutate_settings(_apply)


def clear_password() -> None:
    """Turn auth off entirely: drop the hash and record the explicit choice (the Settings note
    "not password protected" reads this), so a later hash can flip it back on."""

    def _apply(settings: dict) -> None:
        settings.setdefault("kopialens", {})["auth_password_hash"] = ""
        settings["kopialens"]["auth_enabled"] = False

    mutate_settings(_apply)


def current_actor() -> str:
    """Who is performing the action, for audit records. Returns SUBJECT for the single shared
    password; a user store replaces this one function (and the session subject) later."""
    return SUBJECT


def session_token() -> str:
    """Stateless session value: an HMAC over the current password hash AND the subject, so the
    cookie format already carries an identity and never has to change when users arrive.
    Restart-safe (nothing is held in memory) and self-invalidating when the password changes."""
    stored = password_hash()
    if not stored:
        return ""
    mac = hmac.new(stored.encode("utf-8"), f"kopialens-session-v1:{SUBJECT}".encode(),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def valid_session(token: str | None) -> bool:
    expected = session_token()
    return bool(expected) and bool(token) and hmac.compare_digest(token, expected)


def _hostname(host_header: str) -> str:
    """Strip the port from a Host header, leaving a bare hostname or IP literal."""
    host = (host_header or "").strip()
    if host.startswith("["):                       # [::1]:7123
        return host[: host.find("]") + 1].lower()
    return host.rsplit(":", 1)[0].lower() if ":" in host else host.lower()


def allowed_hosts() -> list[str]:
    """Hostnames allowed in the Host header (a reverse proxy's domain). The env override exists so
    a provisioned image can set it without touching settings."""
    env = _env("KOPIALENS_ALLOWED_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    return [str(h).strip() for h in (_kl().get("allowed_hosts") or []) if str(h).strip()]


def host_allowed(host_header: str) -> bool:
    name = _hostname(host_header)
    if name in _LOCAL_HOSTS:
        return True
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True    # an IP literal cannot be the target of DNS rebinding
    except ValueError:
        pass
    return name in {h.lower() for h in allowed_hosts()}


def bind_host() -> str:
    """Interface to listen on. Env beats an explicit settings value, which beats the launch
    profile: desktop = loopback only, server = all interfaces. Only an explicit choice in the
    settings FILE counts here - the merged default would be indistinguishable from one."""
    env = _env("KOPIALENS_BIND_HOST")
    if env:
        return env
    raw = raw_kopialens_settings()
    if str(raw.get("bind_host") or "").strip():
        return str(raw["bind_host"]).strip()
    return "127.0.0.1" if profile() == "desktop" else "0.0.0.0"


def exempt(path: str) -> bool:
    """Paths reachable without a session: the login page itself, its POST target, logout, and the
    health probe (used by supervisors that hold no cookie). Deliberately EXACT matches - a prefix
    rule here would leave set/remove-password open to unauthenticated callers."""
    return path in ("/login", "/health", "/api/auth/login", "/api/auth/logout")


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>KopiaLens</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
      background:#1c1917;color:#e7e5e4;font:14px system-ui,Segoe UI,sans-serif}
 form{width:320px;padding:28px;background:#292524;border:1px solid #44403c;border-radius:10px}
 h1{margin:0 0 4px;font-size:18px;font-weight:600}
 p{margin:0 0 18px;color:#a8a29e;font-size:12px;line-height:1.5}
 input{width:100%;box-sizing:border-box;padding:9px 10px;margin-bottom:12px;border-radius:6px;
       border:1px solid #57534e;background:#1c1917;color:#e7e5e4;font-size:14px}
 button{width:100%;padding:9px;border:0;border-radius:6px;background:#0d9488;color:#fff;
        font-size:14px;font-weight:600;cursor:pointer}
 .err{color:#fda4af;font-size:12px;margin-bottom:12px}
</style></head><body>
<form method="post" action="/api/auth/login">
  <h1>KopiaLens</h1>
  <p>Enter the shared password to continue.</p>
  __ERROR__
  <input type="password" name="password" placeholder="Password" autofocus required>
  <button type="submit">Sign in</button>
</form></body></html>"""


def login_page(error: str = "") -> str:
    err = f'<div class="err">{error}</div>' if error else ""
    return LOGIN_PAGE.replace("__ERROR__", err)


def cookie_kwargs() -> dict:
    """Session cookie flags. Not ``secure``: the app is commonly reached over plain HTTP on a LAN,
    and a secure-only cookie would silently never be sent there."""
    return {"httponly": True, "samesite": "lax", "max_age": 60 * 60 * 24 * 30, "path": "/"}
