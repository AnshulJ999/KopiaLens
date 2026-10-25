"""Sign-in and access control.

Accounts live in ``core/users.py`` (v1: one admin account). A correct sign-in sets a stateless
signed cookie carrying the username, the sign-in time, the account's session epoch and whether
the user ticked "keep me signed in"; the MAC also covers the account's password hash. So the
server enforces the session length itself, a password change or "sign out all devices" (epoch
bump) invalidates every existing session, and nothing is stored per session.

Sign-in is on when an account exists and it has not been switched off (setting ``auth_enabled``,
env ``KOPIALENS_AUTH_ENABLED``); switching it off keeps the account. A fresh install has no account
and no lockout. KOPIALENS_PASSWORD seeds the first account so a headless image is provisionable.

First run on a server install: while no protection choice exists (no account, sign-in not
explicitly off), creating the first account or skipping protection is allowed for
``SETUP_WINDOW`` seconds after start, then only with the one-time setup code printed at startup.

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
import contextvars
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time

from ..settings import load_settings, mutate_settings, raw_kopialens_settings
from . import basepath, users
from .applog import get_logger

log = get_logger("auth")

_ITERATIONS = 200_000
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", ""}
# The audit actor when nobody is signed in (sign-in off): the single-user identity.
SUBJECT = "admin"
MIN_PASSWORD = 8
SESSION_CHOICES = (1, 7, 14, 30, 90, 180, 365)   # days; "stay signed in for" (advanced setting)
DEFAULT_SESSION_DAYS = 30
SETUP_WINDOW = 30 * 60                            # seconds first-run setup is open after start
_STARTED = time.time()
_SETUP_CODE = ""
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I
_ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar("kopialens_actor", default=None)
# verify_password against this when the username is unknown, so both cases take the same time
_DUMMY_HASH = f"pbkdf2_sha256${_ITERATIONS}${'00' * 16}${'00' * 32}"


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


def switched_on() -> bool:
    """The on/off choice alone (env beats the setting), whether or not an account exists."""
    env = _env("KOPIALENS_AUTH_ENABLED").lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return bool(_kl().get("auth_enabled", True))


def enabled() -> bool:
    """Sign-in guards only when an account exists AND it is switched on - a fresh install (no
    account yet) is never locked out, and switching off keeps the account."""
    return switched_on() and users.has_accounts()


def set_enabled(on: bool) -> None:
    mutate_settings(lambda s: s.setdefault("kopialens", {}).__setitem__("auth_enabled", bool(on)))


def protection_chosen() -> bool:
    """Onboarding's Protect decision was made: an account exists, or sign-in was explicitly
    switched off ("skip protection")."""
    return users.has_accounts() or raw_kopialens_settings().get("auth_enabled") is False


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


def authenticate(username: str, password: str) -> dict | None:
    """The account these credentials sign in to, or None. An unknown username costs the same
    hashing time as a wrong password."""
    user = users.find(username)
    ok = verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
    return user if (ok and user) else None


def current_actor() -> str:
    """Who is performing the action, for audit records: the signed-in account, else SUBJECT."""
    return _ACTOR.get() or SUBJECT


def set_actor(username: str | None) -> contextvars.Token:
    return _ACTOR.set(username)


# ---- sessions --------------------------------------------------------------------------------

def session_days() -> int:
    try:
        days = int(_kl().get("session_days", DEFAULT_SESSION_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_DAYS
    return days if days in SESSION_CHOICES else DEFAULT_SESSION_DAYS


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(payload: str, user: dict) -> str:
    msg = f"kopialens-session-v2|{payload}|{user.get('password_hash', '')}".encode()
    return _b64(hmac.new(users.secret(), msg, hashlib.sha256).digest())


def issue_session(user: dict, persist: bool, now: float | None = None) -> str:
    """Cookie value: base64(username|signed-in-at|epoch|persist) "." MAC."""
    issued = int(now if now is not None else time.time())
    payload = _b64(f"{user['username']}|{issued}|{int(user.get('epoch', 0))}|{int(persist)}".encode())
    return f"{payload}.{_mac(payload, user)}"


def session_user(token: str | None, now: float | None = None) -> dict | None:
    """The account a cookie signs in, or None when it is malformed, forged, from before a password
    change or "sign out all devices", or older than the session length."""
    if not token or "." not in token:
        return None
    payload, mac = token.rsplit(".", 1)
    try:
        username, issued, epoch, _persist = _unb64(payload).decode().split("|")
        issued_at, epoch_n = int(issued), int(epoch)
    except (ValueError, UnicodeDecodeError):
        return None
    user = users.find(username)
    if user is None or not hmac.compare_digest(mac, _mac(payload, user)):
        return None
    if epoch_n != int(user.get("epoch", 0)):
        return None
    now = now if now is not None else time.time()
    if issued_at > now + 300 or now - issued_at > session_days() * 86400:
        return None
    return user


def valid_session(token: str | None) -> bool:
    return session_user(token) is not None


def session_persists(token: str | None) -> bool:
    """Whether a cookie was issued with "keep me signed in" (unreadable = yes, the default)."""
    try:
        return _unb64((token or "").rsplit(".", 1)[0]).decode().split("|")[3] == "1"
    except (ValueError, IndexError, UnicodeDecodeError):
        return True


# ---- first run (server installs) -------------------------------------------------------------

def mark_started() -> None:
    global _STARTED
    _STARTED = time.time()


def setup_code() -> str:
    global _SETUP_CODE
    if not _SETUP_CODE:
        chars = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        _SETUP_CODE = f"{chars[:4]}-{chars[4:]}"
    return _SETUP_CODE


def first_run_state(now: float | None = None) -> dict:
    """``required``: a server install with no protection choice yet. ``open``: setup works without
    a code. ``needs_code``: the window has closed; the setup code is required."""
    required = profile() == "server" and not protection_chosen()
    closes = _STARTED + SETUP_WINDOW
    is_open = (not required) or (now if now is not None else time.time()) < closes
    return {"required": required, "open": is_open, "closes_at": closes if required else None,
            "needs_code": required and not is_open}


def first_run_allowed(code: str = "", cookie: str = "") -> bool:
    if not first_run_state()["needs_code"]:
        return True
    return hmac.compare_digest((code or "").strip().upper(), setup_code()) or setup_cookie_ok(cookie)


# Entering the setup code once sets this cookie, so the rest of setup needs no code. Its value is
# bound to this process's code, so a restart (which reopens the window anyway) invalidates it.
_COOKIE_TAG = ""


def cookie_tag() -> str:
    """A short per-install suffix for cookie names. Browsers keep cookies per host, not per port,
    so two copies on one address would otherwise share, and overwrite, one sign-in."""
    global _COOKIE_TAG
    if not _COOKIE_TAG:
        from . import checkin
        _COOKIE_TAG = hashlib.sha256(f"cookie:{checkin.install_id()}".encode()).hexdigest()[:8]
    return _COOKIE_TAG


def __getattr__(name: str) -> str:
    # COOKIE_NAME / SETUP_COOKIE are read as module attributes; the tag needs the data folder.
    if name == "COOKIE_NAME":
        return f"kopialens_session_{cookie_tag()}"
    if name == "SETUP_COOKIE":
        return f"kopialens_setup_{cookie_tag()}"
    raise AttributeError(name)


def setup_cookie_value() -> str:
    return hmac.new(users.secret(), f"setup:{setup_code()}".encode(), hashlib.sha256).hexdigest()


def setup_cookie_ok(cookie: str | None) -> bool:
    return bool(cookie) and hmac.compare_digest(str(cookie), setup_cookie_value())


def setup_gate_exempt(path: str) -> bool:
    """API paths reachable while setup is closed and no code was entered: the state the setup page
    needs to explain itself, the code entry, and the sign-in/health paths."""
    return exempt(path) or path in ("/api/onboarding/state", "/api/onboarding/setup-code")


def announce_setup() -> None:
    """Print the setup code where a headless install's owner can read it (stdout = docker logs)."""
    if not first_run_state()["required"]:
        return
    msg = (f"Setup is open for {SETUP_WINDOW // 60} minutes. After that, enter this setup code: "
           f"{setup_code()} (restarting KopiaLens also reopens setup).")
    log.warning(msg)
    print(f"KopiaLens: {msg}", flush=True)


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
    """Interface to listen on. Env beats an explicit settings value, which beats the access mode
    (whose own default follows the launch profile: desktop = loopback, server = all interfaces).
    Only an explicit choice in the settings FILE counts here - the merged default would be
    indistinguishable from one."""
    env = _env("KOPIALENS_BIND_HOST")
    if env:
        return env
    raw = raw_kopialens_settings()
    if str(raw.get("bind_host") or "").strip():
        return str(raw["bind_host"]).strip()
    from . import netaccess
    return netaccess.bind_for_mode(netaccess.mode())


def exempt(path: str) -> bool:
    """Paths reachable without a session: the login and password-recovery pages, their POST
    targets, logout, and the health probe (used by supervisors that hold no cookie). Deliberately
    EXACT matches - a prefix rule here would leave account routes open to unauthenticated callers."""
    return path in ("/login", "/login/recover", "/health", "/api/auth/login", "/api/auth/recover",
                    "/api/auth/logout", "/shell/open")


# The desktop shell's per-launch secret (env KOPIALENS_SHELL_TOKEN, set by the shell that started
# this server) opens only these paths, and only from this computer.
SHELL_HEADER = "x-kopialens-shell"
SHELL_PATHS = frozenset({"/api/shell/state", "/api/shell/prefs", "/api/shell/open-code", "/api/shell/autostart",
                         "/api/shell/update-progress", "/api/index/build",
                         "/api/index/stop", "/api/index/pause", "/api/index/resume",
                         "/api/engine/pause", "/api/engine/resume"})


def shell_request_ok(path: str, header: str | None, client_host: str | None) -> bool:
    token = _env("KOPIALENS_SHELL_TOKEN")
    if not token or not header or path not in SHELL_PATHS:
        return False
    try:
        local = ipaddress.ip_address(client_host or "").is_loopback
    except ValueError:
        return False
    return local and hmac.compare_digest(header.encode(), token.encode())


_LOGO = ('<svg class="logo" viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="27" fill="#0f766e"/>'
         '<g transform="translate(32 32) scale(0.52) translate(-35 -32)"><rect x="10" y="8" width="11" height="48" rx="2" '
         'fill="#fff"/><polygon points="24,30 45,8 57,8 36,30" fill="#fff"/><polygon points="28,35 40,35 60,56 48,56" '
         'fill="#f59e0b"/></g></svg>')  # the Disc logo (packaging/build_logos.py)

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Sign in - KopiaLens</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{--canvas:#f5f5f4;--surface:#fff;--bd:#d6d3d1;--fg:#1c1917;--muted:#78716c;--bad:#b91c1c;
       --accent:#4f46e5;--accent-bd:#a5b4fc;--accent-bg:#eef2ff}
 @media (prefers-color-scheme: dark){:root{--canvas:#0f0f10;--surface:#18181b;--bd:#3a3a42;
       --fg:#e7e5e4;--muted:#a1a1aa;--bad:#f87171;--accent:#a5b4fc;--accent-bd:#4338ca;--accent-bg:#1e1b4b}}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;
      background:var(--canvas);color:var(--fg);font:14px/1.45 system-ui,"Segoe UI",sans-serif}
 form{width:100%;max-width:340px;padding:24px;background:var(--surface);border:1px solid var(--bd);
      border-radius:12px;display:flex;flex-direction:column;gap:12px}
 .brand{display:flex;gap:8px;align-items:center;font-weight:600}
 .logo{width:24px;height:24px;flex-shrink:0}
 h1{margin:0;font-size:17px}
 label.f{display:flex;flex-direction:column;gap:4px;font-size:12px;font-weight:500}
 input[type=text],input[type=password]{height:34px;padding:0 9px;border-radius:6px;border:1px solid var(--bd);
      background:var(--surface);color:var(--fg);font-size:14px}
 input.bad{border-color:var(--bad)}
 label.c{display:flex;gap:7px;align-items:center;font-size:13px}
 button{height:34px;border:1px solid var(--fg);border-radius:6px;background:var(--fg);color:var(--surface);
        font-size:14px;font-weight:600;cursor:pointer}
 .err{color:var(--bad);font-size:12px}
 .lock{border:1px solid var(--accent-bd);background:var(--accent-bg);border-radius:6px;padding:8px 10px;font-size:12px}
 .note{color:var(--muted);font-size:12px}
 a{color:var(--accent);font-size:12px}
 code{font-family:ui-monospace,Consolas,monospace;font-size:12px}
</style></head><body>
<form method="post" action="__BASE__/api/auth/login">
  <div class="brand">__LOGO__KopiaLens</div>
  <h1>Sign in</h1>
  __LOCK__
  <label class="f">Username<input type="text" name="username" autocomplete="username" value="__USER__" required __FOCUS_USER__></label>
  <label class="f">Password<input type="password" name="password" autocomplete="current-password" required __BAD__ __FOCUS_PW__></label>
  __ERROR__
  <label class="c"><input type="checkbox" name="remember" value="1" checked>Keep me signed in</label>
  <button type="submit">__BUTTON__</button>
  <div class="note">__REMEMBER_NOTE__</div>
  <a href="__BASE__/login/recover">Forgot password?</a>
</form></body></html>""".replace("__LOGO__", _LOGO)

def _read_pw_script() -> str:
    """The show/hide password script, inlined so the sign-in pages need no static files."""
    try:
        from pathlib import Path
        js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "pwreveal.js").read_text(encoding="utf-8")
        return f"<script>{js}</script>"
    except OSError:
        return ""


_PW_SCRIPT = _read_pw_script()

_LOCAL_RESET = ("On the computer running KopiaLens, run <code>kopialens --reset-password</code> "
                "(from source: <code>python main.py --reset-password</code>).")


def _days_label(days: int) -> str:
    return {1: "1 day", 7: "1 week", 14: "2 weeks", 30: "30 days", 90: "3 months",
            180: "6 months", 365: "1 year"}.get(days, f"{days} days")


def login_page(error: str = "", username: str = "", unlocks: bool = False) -> str:
    """The sign-in form. ``unlocks``: the index is locked and signing in will unlock it."""
    from html import escape
    lock = ('<div class="lock">The index is locked since KopiaLens restarted. '
            'Signing in unlocks it.</div>') if unlocks else ""
    note = (f"Stays signed in for {_days_label(session_days())} on this browser. "
            "Unticked: until you close the browser.")
    return (LOGIN_PAGE.replace("__LOCK__", lock)
            .replace("__USER__", escape(username, quote=True))
            .replace("__FOCUS_USER__", "" if username else "autofocus")
            .replace("__FOCUS_PW__", "autofocus" if username else "")
            .replace("__BAD__", 'class="bad"' if error else "")
            .replace("__ERROR__", f'<div class="err">{escape(error)}</div>' if error else "")
            .replace("__BUTTON__", "Sign in and unlock" if unlocks else "Sign in")
            .replace("__REMEMBER_NOTE__", note)
            .replace("__BASE__", basepath.current())
            .replace("</body></html>", _PW_SCRIPT + "</body></html>"))


def recover_page(error: str = "", with_key: bool = False) -> str:
    """Forgotten password. ``with_key``: the password also unlocks encryption and a recovery key
    exists, so the recovery key can reset it here; otherwise only the local reset applies."""
    from html import escape
    head, _form_open, _rest = LOGIN_PAGE.partition('<form method="post" action="__BASE__/api/auth/login">')
    err = f'<div class="err">{escape(error)}</div>' if error else ""
    if with_key:
        body = ('<form method="post" action="__BASE__/api/auth/recover">'
                f'<div class="brand">{_LOGO}KopiaLens</div>'
                '<h1>Reset your password</h1>'
                '<div class="note">Your recovery key sets a new password for signing in and for '
                'encryption.</div>'
                '<label class="f">Recovery key<input type="text" name="recovery_key" '
                'autocomplete="off" spellcheck="false" required autofocus></label>'
                '<label class="f">New password<input type="password" name="new" '
                'autocomplete="new-password" required minlength="8"></label>'
                '<label class="f">Confirm new password<input type="password" name="confirm" '
                'autocomplete="new-password" required minlength="8"></label>'
                f'{err}<button type="submit">Reset password</button>'
                f'<div class="note">No recovery key? {_LOCAL_RESET}</div>'
                '<a href="__BASE__/login">Back to sign in</a></form>')
    else:
        body = ('<form onsubmit="return false">'
                f'<div class="brand">{_LOGO}KopiaLens</div>'
                '<h1>Reset your password</h1>'
                f'<div class="note">{_LOCAL_RESET}</div>'
                '<a href="__BASE__/login">Back to sign in</a></form>')
    return head.replace("<title>Sign in - KopiaLens</title>",
                        "<title>Reset password - KopiaLens</title>") + body.replace(
        "__BASE__", basepath.current()) + _PW_SCRIPT + "</body></html>"


# Whether the current request arrived over HTTPS (built-in, or a trusted proxy's X-Forwarded-Proto);
# set by the access middleware so every cookie writer marks its cookie secure without a request.
_SECURE: contextvars.ContextVar[bool] = contextvars.ContextVar("kopialens_https", default=False)


def set_secure(on: bool):
    return _SECURE.set(bool(on))


def request_secure() -> bool:
    return _SECURE.get()


def cookie_kwargs(persist: bool = True) -> dict:
    """Session cookie flags. ``secure`` only for a request that came over HTTPS: on plain HTTP a
    secure-only cookie would silently never be sent. Without ``persist`` it is a browser-session
    cookie; the server enforces the session length either way."""
    kwargs = {"httponly": True, "samesite": "lax", "path": "/", "secure": request_secure()}
    if persist:
        kwargs["max_age"] = session_days() * 86400
    return kwargs


# ---- login throttling -------------------------------------------------------------------
# Wrong passwords cost an increasing wait; a right one clears the record. Deliberately NOT a
# lockout: v1 has one admin account, so guessing must never be able to lock the owner out - the
# worst case here is a 30 s wait. In memory only, so a restart
# forgives everything, and a quiet period forgets too.
#
# Behind a reverse proxy every request arrives from the proxy, so all proxy users share one
# bucket - unless the proxy is listed as trusted, which makes the client address the real one.
_THROTTLE_AFTER = 5                    # failures before the first wait
_THROTTLE_MAX = 30.0                   # seconds; the wait never exceeds this
_THROTTLE_FORGET = 900.0               # a quiet period this long clears the record
_THROTTLE_CAP = 1024                   # addresses remembered; past it expired entries are dropped
_THROTTLE: dict[str, tuple[int, float]] = {}
_THROTTLE_LOCK = threading.Lock()


def login_delay(address: str) -> float:
    """Seconds ``address`` must wait before another attempt; 0.0 when it may try now."""
    now = time.time()
    with _THROTTLE_LOCK:
        count, last = _THROTTLE.get(address, (0, 0.0))
        if count <= _THROTTLE_AFTER or now - last > _THROTTLE_FORGET:
            return 0.0
        return min(_THROTTLE_MAX, float(2 ** (count - _THROTTLE_AFTER)))


def record_login_failure(address: str) -> None:
    now = time.time()
    with _THROTTLE_LOCK:
        count, last = _THROTTLE.get(address, (0, 0.0))
        _THROTTLE[address] = (count + 1 if now - last <= _THROTTLE_FORGET else 1, now)
        if len(_THROTTLE) > _THROTTLE_CAP:
            for addr in [a for a, (_c, t) in _THROTTLE.items() if now - t > _THROTTLE_FORGET]:
                _THROTTLE.pop(addr, None)
            if len(_THROTTLE) > _THROTTLE_CAP:          # all fresh: a spray, not a guesser
                _THROTTLE.clear()


def record_login_success(address: str) -> None:
    with _THROTTLE_LOCK:
        _THROTTLE.pop(address, None)
