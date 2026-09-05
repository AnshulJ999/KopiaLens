"""Single shared-password access control (REL2, "Option A").

Shape, deliberately small: no user accounts, no roles, no session store. One password guards the
whole app; a correct login sets a signed cookie derived from the stored password hash, so changing
the password invalidates every existing session and nothing has to be persisted server-side.

Two independent protections live here:

* the password itself, for anyone who can reach the port;
* Host-header validation, against DNS rebinding - a website the user visits cannot be allowed to
  script requests at this app just because it is reachable on their network. IP literals and
  localhost are always accepted (rebinding needs a DOMAIN name to point at a private address);
  any other hostname must be listed in ``allowed_hosts``.

Choosing whether to enable this, and choosing the password, belongs to first-run onboarding
(). Until that exists, ``needs_setup()`` drives a minimal password-choosing form.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets

from ..settings import load_settings, mutate_settings

COOKIE_NAME = "kopialens_session"
_ITERATIONS = 200_000
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", ""}


def _kl() -> dict:
    try:
        return load_settings().get("kopialens", {}) or {}
    except Exception:  # noqa: BLE001 - an unreadable settings file must not lock the app open
        return {}


def enabled() -> bool:
    return bool(_kl().get("auth_enabled", True))


def password_hash() -> str:
    return str(_kl().get("auth_password_hash") or "")


def needs_setup() -> bool:
    """Auth is on but no password has been chosen yet."""
    return enabled() and not password_hash()


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


def session_token() -> str:
    """Stateless session value: an HMAC over the current password hash. Restart-safe (nothing is
    held in memory) and self-invalidating when the password changes."""
    stored = password_hash()
    if not stored:
        return ""
    mac = hmac.new(stored.encode("utf-8"), b"kopialens-session-v1", hashlib.sha256).digest()
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


def host_allowed(host_header: str) -> bool:
    name = _hostname(host_header)
    if name in _LOCAL_HOSTS:
        return True
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True    # an IP literal cannot be the target of DNS rebinding
    except ValueError:
        pass
    allowed = {str(h).strip().lower() for h in (_kl().get("allowed_hosts") or []) if str(h).strip()}
    return name in allowed


def bind_host(default: str) -> str:
    """Interface to listen on. Loopback-only by default: reaching the app from another device is
    a deliberate choice, not the out-of-the-box state."""
    value = str(_kl().get("bind_host") or "").strip()
    return value or default


def exempt(path: str) -> bool:
    """Paths reachable without a session: the login page itself, its POST target, and the
    health probe (used by supervisors that hold no cookie)."""
    return path in ("/login", "/health") or path.startswith("/api/auth/")


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
<form method="post" action="/api/auth/__ACTION__">
  <h1>__TITLE__</h1>
  <p>__BLURB__</p>
  __ERROR__
  <input type="password" name="password" placeholder="Password" autofocus required>
  __CONFIRM__
  <button type="submit">__BUTTON__</button>
</form></body></html>"""


def login_page(error: str = "") -> str:
    setup = needs_setup()
    err = f'<div class="err">{error}</div>' if error else ""
    return (LOGIN_PAGE
            .replace("__ACTION__", "setup" if setup else "login")
            .replace("__TITLE__", "Choose a password" if setup else "KopiaLens")
            .replace("__BLURB__",
                     "This KopiaLens is protected by a single shared password. Pick one now - "
                     "anyone who can reach this address will need it."
                     if setup else "Enter the shared password to continue.")
            .replace("__ERROR__", err)
            .replace("__CONFIRM__",
                     '<input type="password" name="confirm" placeholder="Confirm password" required>'
                     if setup else "")
            .replace("__BUTTON__", "Set password" if setup else "Sign in"))


def cookie_kwargs() -> dict:
    """Session cookie flags. Not ``secure``: the app is commonly reached over plain HTTP on a LAN,
    and a secure-only cookie would silently never be sent there."""
    return {"httponly": True, "samesite": "lax", "max_age": 60 * 60 * 24 * 30, "path": "/"}
