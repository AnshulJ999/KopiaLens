"""API tokens: named, revocable credentials for scripts, the CLI and AI agents.

A request carrying ``Authorization: Bearer kl_...`` acts as that token (audit actor
``token:<name>``). What it may do is decided in ONE place, ``check``: every route is sorted in
``classify`` into read / app / repo / person / never, and the token's scopes (today only its write
mode) are applied on top. The repository's own write mode still applies afterwards, so the stricter
of the two wins.

``tokens.json`` sits next to ``users.json``: plain at every encryption level (it is read before an
unlock), hashes only (SHA-256 of a 32-byte random secret), and never part of a settings backup.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from config import CONFIG_DIR

from .applog import get_logger

log = get_logger("tokens")

TOKENS_FILE = os.path.join(CONFIG_DIR, "tokens.json")
PREFIX = "kl_"
EXPIRY_DAYS = (30, 90, 180, 365, 0)   # 0 = never
DEFAULT_EXPIRY_DAYS = 90
DEFAULT_MODE = "read_only"
_NAME = re.compile(r"^[^\x00-\x1f]{1,40}$")
_LAST_USED_EVERY = 60                 # seconds between last-used writes per token
_LOCK = threading.RLock()
_CACHE: dict = {"mtime": None, "data": None}
_CURRENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("kl_token", default=None)


# ---------------------------------------------------------------------------- store

def _empty() -> dict:
    return {"version": 1, "tokens": []}


def _read() -> dict:
    try:
        mtime = os.stat(TOKENS_FILE).st_mtime_ns
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        log.warning("tokens.json could not be read (%s); using the last good copy", exc)
        return _CACHE["data"] or _empty()
    with _LOCK:
        if _CACHE["mtime"] == mtime and _CACHE["data"] is not None:
            return _CACHE["data"]
        try:
            with open(TOKENS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.error("tokens.json unreadable (%s): no token is accepted until it is fixed", exc)
            data = _empty()
        if not isinstance(data, dict) or not isinstance(data.get("tokens"), list):
            data = _empty()
        _CACHE.update(mtime=mtime, data=data)
        return data


def _mutate(fn) -> dict:
    from .dataupgrade import atomic_json
    from .filelock import locked
    with _LOCK, locked(TOKENS_FILE):
        _CACHE.update(mtime=None, data=None)
        data = json.loads(json.dumps(_read()))
        out = fn(data)
        os.makedirs(os.path.dirname(TOKENS_FILE) or ".", exist_ok=True)
        atomic_json(TOKENS_FILE, data)
        _CACHE.update(mtime=os.stat(TOKENS_FILE).st_mtime_ns, data=data)
        return out


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _public(rec: dict) -> dict:
    exp = rec.get("expires")
    return {"id": rec["id"], "name": rec["name"], "mode": (rec.get("scopes") or {}).get("write_mode"),
            "created": rec.get("created"), "created_by": rec.get("created_by", ""),
            "expires": exp, "expired": bool(exp and exp <= time.time()),
            "last_used": rec.get("last_used")}


def list_tokens() -> list[dict]:
    return [_public(r) for r in _read()["tokens"]]


def create(name: str, mode: str, days: int, created_by: str = "") -> tuple[str, dict]:
    """A new token: the secret (shown once, never stored) and its public record."""
    from . import writemode
    name = (name or "").strip()
    if not _NAME.match(name):
        raise ValueError("Give the token a name of up to 40 characters.")
    if mode not in writemode.MODES:
        raise ValueError("Pick one of the listed permissions.")
    if days not in EXPIRY_DAYS:
        raise ValueError("Pick one of the listed expiry choices.")
    secret = PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    rec = {"id": secrets.token_hex(4), "name": name, "hash": _hash(secret),
           "scopes": {"write_mode": mode}, "created": now, "created_by": created_by,
           "expires": now + days * 86400 if days else None, "last_used": None}

    def _add(data: dict) -> None:
        if any(t["name"].lower() == name.lower() for t in data["tokens"]):
            raise ValueError("A token with that name already exists.")
        data["tokens"].append(rec)

    _mutate(_add)
    return secret, _public(rec)


def revoke(token_id: str) -> dict | None:
    """Remove a token; its public record, or None when there is no such token."""
    def _drop(data: dict):
        for i, t in enumerate(data["tokens"]):
            if t["id"] == token_id:
                return data["tokens"].pop(i)
        return None
    gone = _mutate(_drop)
    return _public(gone) if gone else None


def bearer(header: str | None) -> str | None:
    """The token in an ``Authorization: Bearer kl_...`` header, else None (other schemes ignored)."""
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value.startswith(PREFIX):
        return None
    return value


def verify(secret: str) -> dict | None:
    """The token record for ``secret`` when it exists and has not expired."""
    digest = _hash(secret)
    now = time.time()
    for rec in _read()["tokens"]:
        if hmac.compare_digest(rec.get("hash") or "", digest):
            if rec.get("expires") and rec["expires"] <= now:
                return None
            if not rec.get("last_used") or now - rec["last_used"] > _LAST_USED_EVERY:
                _touch(rec["id"], now)
            return rec
    return None


def _touch(token_id: str, now: float) -> None:
    def _set(data: dict) -> None:
        for t in data["tokens"]:
            if t["id"] == token_id:
                t["last_used"] = now
    try:
        _mutate(_set)
    except OSError as exc:   # a missed last-used stamp must never refuse the request
        log.warning("token last-used not saved: %s", exc)


def use(rec: dict | None) -> contextvars.Token:
    return _CURRENT.set(rec)


def reset(tok: contextvars.Token) -> None:
    _CURRENT.reset(tok)


def current() -> dict | None:
    """The token this request authenticated with, or None (a person, the desktop app, no sign-in)."""
    return _CURRENT.get()


# ---------------------------------------------------------------------------- what a token may do

READ, APP, REPO, PERSON, NEVER = "read", "app", "repo", "person", "never"

_DELETE_JOBS = {"remove_files", "prune_versions", "snapshot_delete", "source_delete"}
# Settings keys that decide who can reach KopiaLens or how its data is protected.
_GUARDED_SETTINGS = {"auth_enabled", "access", "access_addresses", "allowed_hosts", "bind_host",
                     "https", "https_cert", "https_key", "session_days", "trusted_proxies",
                     "encryption", "encryption_level", "write_mode", "kopia_path"}


def _job(body: dict, _q: dict) -> str:
    kind = body.get("kind") or ""
    params = body.get("params") or {}
    if kind in _DELETE_JOBS or (kind == "policy_write" and params.get("then")):
        return READ if params.get("dry") else PERSON
    if kind == "encryption_level":
        return PERSON          # the level is set through /api/encryption/level
    from . import writemode
    return REPO if kind in writemode.JOBS else PERSON


def _settings(body: dict, _q: dict) -> str:
    extra = set(body) - {"kopialens", "backblaze"}
    kl = body.get("kopialens") if isinstance(body.get("kopialens"), dict) else {}
    return PERSON if extra or (_GUARDED_SETTINGS & set(kl)) else APP


def _write_mode(body: dict, _q: dict) -> str:
    """Stricter is the safe side; anything looser needs a person."""
    from . import writemode
    new = body.get("mode")
    repo = body.get("repo")
    before = writemode.mode(repo)
    after = new if repo is None else (new or writemode.global_mode())
    if after not in writemode.MODES:
        return PERSON
    return APP if writemode.MODES.index(after) <= writemode.MODES.index(before) else PERSON


def _encryption_level(body: dict, _q: dict) -> str:
    from ..index import crypto
    order = list(crypto.LEVELS)
    new = str(body.get("level") or "").strip().lower()
    cur = crypto.level()
    if new not in order or cur not in order:
        return PERSON
    return APP if order.index(new) >= order.index(cur) else PERSON


def _sign_in(body: dict, _q: dict) -> str:
    return APP if body.get("on") is True else PERSON


def _access(body: dict, _q: dict) -> str:
    """Only a move to a stricter access mode, with nothing else changed, is the safe side."""
    from . import netaccess
    if set(k for k, v in body.items() if v is not None) != {"access"}:
        return PERSON
    order = list(netaccess.MODES)
    new, cur = body.get("access"), netaccess.mode()
    if new not in order or cur not in order:
        return PERSON
    return APP if order.index(new) <= order.index(cur) else PERSON


def _export(_body: dict, query: dict) -> str:
    groups = {g for g in str(query.get("groups") or "settings,repos").split(",") if g}
    return READ if groups <= {"settings"} else PERSON   # repos/accounts/keys carry credentials


# Every write route, sorted. The guard test fails on a write route missing here.
# read = changes nothing (previews, plans, dry runs, lookups sent as POST);
# app = KopiaLens's own state, the safe side (needs a token above Read-only);
# repo = changes a repository: the token's write mode must allow the route's need;
# person = the unsafe side, a person does it in KopiaLens; never = sets or reveals a secret.
_WRITES: dict[str, object] = {
    **{f"POST /api/actions/{p}": READ for p in (
        "batch-detail", "browse-folder", "folder-reclaim", "remove-detail", "remove-files-dry-run",
        "remove-list", "remove-plan", "remove-plan-dry-run", "snapshot-delete-dry-run", "compact",
        "mount", "mount-browse", "mount-open", "unmount", "unmount-all")},
    **{f"POST /api/actions/{p}": PERSON for p in ("remove-bat", "remove-files", "snapshot-delete")},
    **{f"POST /api/policies/{p}": READ for p in (
        "diff", "exclusion-targets", "exclusion-variants", "multi-plan", "preview", "price", "resolve")},
    "POST /api/policies/backup": APP,
    "POST /api/policies/save": REPO,
    "POST /api/policies/restore": REPO,
    "POST /api/client/scripts": PERSON,          # lets kopia run commands on this computer
    **{f"POST /api/{p}": READ for p in (
        "delete/plan", "delete/stats", "folder/detail", "prune/plan", "prune/price", "restore/plan",
        "restore/check", "restore/check/{check_id}/stop", "restore/open", "search/folder-values",
        "search/folder-versions", "search/versions", "simulate", "simulate/all", "snapshots/analyze",
        "snapshots/analyze-all", "tree/holders", "help/check-updates", "setup/probe")},
    "POST /api/snapshots/pin": REPO,
    "POST /api/snapshots/set-reclaim": APP,
    "POST /api/stats/description": REPO,
    "POST /api/jobs/start": _job,
    "POST /api/jobs/stop": APP,
    "POST /api/tasks/cancel": REPO,
    **{f"POST /api/engine/{p}": REPO for p in ("cancel", "choice", "identity", "snapshot",
                                               "snapshot-description")},
    "POST /api/engine/resume": APP,
    "POST /api/engine/source-resume": APP,
    "POST /api/engine/pause": PERSON,            # pausing backups is the unsafe side
    "POST /api/engine/source-pause": PERSON,
    **{f"POST /api/index/{p}": APP for p in ("backups/copy", "build", "notices/{notice_id}/dismiss",
                                             "pause", "pending/run", "resume", "stop")},
    "POST /api/app/restart": APP,
    "POST /api/help/install-update": APP,
    **{f"POST /api/backblaze/{p}": APP for p in ("exclusions/generate", "import-archive", "refresh")},
    "POST /api/config-backups": APP,
    "POST /api/config-backups/notices/{notice_id}/dismiss": APP,
    "POST /api/config-backups/import": READ,     # a preview; applying is separate
    "POST /api/config-backups/import/apply": PERSON,
    "POST /api/config-backups/{backup_id}/restore": PERSON,
    "POST /api/notify/profile": REPO,
    "POST /api/notify/profile/delete": PERSON,   # would silence alerts
    "POST /api/notify/profile/test": APP,
    "POST /api/notify/webhook/test": APP,
    "POST /api/settings": _settings,
    "POST /api/settings/table": APP,
    "POST /api/write-mode": _write_mode,
    "POST /api/access": _access,
    **{f"POST /api/onboarding/{p}": APP for p in ("begin", "finish", "restart", "start-indexing")},
    "POST /api/onboarding/setup-code": NEVER,
    **{f"POST /api/setup/{p}": APP for p in ("attach", "backblaze", "connect", "create",
                                             "index/rebuild", "sample", "repos/{repo}/unlock")},
    "PATCH /api/setup/repos/{repo}": APP,
    "DELETE /api/setup/repos/{repo}": PERSON,
    "POST /api/setup/kopia": PERSON,             # picks the program KopiaLens runs
    **{f"POST /api/licence{p}": APP for p in ("", "/activate", "/renew", "/patreon/start",
                                              "/patreon/claim")},
    "DELETE /api/licence": PERSON,
    "POST /api/encryption/level": _encryption_level,
    "POST /api/encryption/lock": APP,
    "POST /api/encryption/remember": PERSON,
    "POST /api/encryption/reset": PERSON,
    **{f"POST /api/encryption/{p}": NEVER for p in ("setup", "change-password", "reset-password",
                                                    "unlock", "unlock-recovery", "recovery-key")},
    "POST /api/account/enabled": _sign_in,
    **{f"POST /api/account/{p}": PERSON for p in ("username", "sign-out-all")},
    **{f"POST /api/account/{p}": NEVER for p in ("create", "password", "encryption/split",
                                                 "encryption/join")},
    **{f"POST /api/auth/{p}": NEVER for p in ("login", "logout", "recover")},
    **{f"POST /api/shell/{p}": NEVER for p in ("open-code", "prefs", "update-progress")},
    "POST /api/tokens": PERSON,
    "DELETE /api/tokens/{token_id}": PERSON,
    **{f"{m} /api/devnotes/{p}": PERSON for m, p in (
        ("POST", "backup"), ("POST", "notes"), ("PATCH", "notes/{note_id}"),
        ("DELETE", "notes/{note_id}"), ("PUT", "reviews/{slug}/verdicts/{card}"),
        ("DELETE", "reviews/{slug}/verdicts/{card}"), ("POST", "round"), ("POST", "state"))},
}
# Reads a token may not make: they hand out credentials or manage tokens.
_READS: dict[str, object] = {
    "GET /api/config-backups/export": _export,
    "GET /api/tokens": PERSON,
}


def classify(method: str, route: str, body: dict, query: dict) -> str:
    """The class of a request. ``route`` is the route's path template (``/api/x/{id}``).
    A write route missing from the table is treated as needing a person."""
    key = f"{method} {route}"
    if method in ("GET", "HEAD", "OPTIONS"):
        rule = _READS.get(key.replace("HEAD ", "GET ", 1), READ)
    else:
        rule = _WRITES.get(key, PERSON)
    return rule(body, query) if callable(rule) else rule


MESSAGES = {
    PERSON: "Ask the user to do this in KopiaLens. An API token can't do it on its own.",
    NEVER: "This step stays with the user in KopiaLens; an API token can never do it.",
}


def check(rec: dict, method: str, route: str, path: str, body: dict, query: dict) -> tuple[str, str] | None:
    """None when the token may make this request, else (kind, message). ``kind`` is
    person / never / mode; a person request is what approvals will pick up."""
    from . import writemode
    cls = classify(method, route, body, query)
    if cls == READ:
        return None
    if cls in MESSAGES:
        return cls, MESSAGES[cls]
    need = writemode.route_need(method, path, body, query) if cls == REPO else None
    if cls == REPO and need is None:
        return None            # e.g. an estimate or verify job: reads the repository only
    mode = (rec.get("scopes") or {}).get("write_mode") or DEFAULT_MODE
    label = writemode.LABELS.get(mode, mode)
    if mode not in writemode.MODES or mode == "read_only":
        return "mode", f"This token is {label}, so it can't change anything."
    if need and writemode.MODES.index(mode) < writemode.MODES.index(need):
        return "mode", f"This token is {label}. This needs {writemode.LABELS[need]}."
    return None
