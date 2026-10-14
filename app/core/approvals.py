"""Approvals: a token's request that needs a person waits here until someone approves it in KopiaLens.

The token sends the identical request again with ``X-KopiaLens-Approval: <id>``. It runs once,
only for the same token, only while the approval is fresh (one hour). A token can never approve.
Kept in memory only: a restart drops every waiting request, and the agent asks again.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import secrets
import threading
import time

import config

from .applog import get_logger

log = get_logger("approvals")

HEADER = "X-KopiaLens-Approval"
TTL = 3600
_KEEP_DECIDED = 3600          # decided requests stay listed this long after their expiry
_LOCK = threading.Lock()
_ITEMS: dict[str, dict] = {}
_CONFIRMED: contextvars.ContextVar[bool] = contextvars.ContextVar("kl_approval_password", default=False)

WAITING, APPROVED, DENIED, USED, EXPIRED = "waiting", "approved", "denied", "used", "expired"


class PasswordNeeded(Exception):
    pass


def _fingerprint(method: str, path: str, body: dict, query: dict) -> str:
    raw = json.dumps([method, path, body, sorted(query.items())], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prune(now: float) -> None:
    for aid, rec in list(_ITEMS.items()):
        if rec["status"] in (WAITING, APPROVED) and rec["expires"] <= now:
            rec["status"] = EXPIRED
        if rec["status"] not in (WAITING, APPROVED) and now - rec["expires"] > _KEEP_DECIDED:
            del _ITEMS[aid]


def _public(rec: dict) -> dict:
    return {k: rec[k] for k in ("id", "token_id", "token_name", "method", "route", "label", "detail",
                                "needs_password", "created", "expires", "status", "decided_by",
                                "decided")}


def request(tok: dict, method: str, route: str, path: str, body: dict, query: dict) -> dict:
    """The waiting request for this exact call: an existing one from the same token, or a new one."""
    fp = _fingerprint(method, path, body, query)
    now = time.time()
    with _LOCK:
        _prune(now)
        for rec in _ITEMS.values():
            if rec["token_id"] == tok["id"] and rec["fp"] == fp and rec["status"] in (WAITING, APPROVED):
                return _public(rec)
        label, detail, needs_password = describe(method, route, body, query)
        rec = {"id": secrets.token_hex(4), "token_id": tok["id"], "token_name": tok["name"],
               "method": method, "route": route, "fp": fp, "label": label, "detail": detail,
               "needs_password": needs_password, "created": now, "expires": now + TTL,
               "status": WAITING, "decided_by": None, "decided": None}
        _ITEMS[rec["id"]] = rec
    from . import audit, engine
    audit.audit("requested", "approval", summary={"id": rec["id"], "label": label, "route": f"{method} {route}"})
    engine.alert(None, "warn", "Approval needed", f"API token \"{tok['name']}\" asks to: {label}. "
                 "Approve or deny it in KopiaLens, Settings > API tokens.")
    return _public(rec)


def redeem(approval_id: str, tok: dict, method: str, path: str, body: dict, query: dict) -> str | None:
    """None when this call may run on the approval (it is then used up), else why not."""
    now = time.time()
    with _LOCK:
        _prune(now)
        rec = _ITEMS.get(approval_id or "")
        if rec is None or rec["token_id"] != tok["id"]:
            return "There's no such approval request for this token. It may have expired after a restart."
        if rec["status"] == WAITING:
            return "This request is still waiting for the user to approve it in KopiaLens."
        if rec["status"] != APPROVED:
            return f"This approval request was {rec['status']}. Ask again with a new request."
        if rec["fp"] != _fingerprint(method, path, body, query):
            return "This request isn't the one that was approved. Send exactly the approved request."
        rec["status"] = USED
        rec["used"] = now
        confirmed = rec.get("password_ok", False)
    if confirmed:
        _CONFIRMED.set(True)
    return None


def status(approval_id: str, tok: dict | None) -> dict | None:
    """One request; a token sees only its own."""
    with _LOCK:
        _prune(time.time())
        rec = _ITEMS.get(approval_id)
        if rec is None or (tok is not None and rec["token_id"] != tok["id"]):
            return None
        return _public(rec)


def listing() -> list[dict]:
    with _LOCK:
        _prune(time.time())
        return sorted((_public(r) for r in _ITEMS.values()), key=lambda r: -r["created"])


def waiting_count() -> int:
    with _LOCK:
        _prune(time.time())
        return sum(1 for r in _ITEMS.values() if r["status"] == WAITING)


def decide(approval_id: str, approve: bool, actor: str, password: str | None = None) -> dict | None:
    """Approve or deny a waiting request. Approving one that needs the password asks for it."""
    from . import audit, auth, writemode
    with _LOCK:
        _prune(time.time())
        rec = _ITEMS.get(approval_id)
        if rec is None:
            return None
        if rec["status"] != WAITING:
            raise ValueError(f"This request was already {rec['status']}.")
        pw_ok = False
        if approve and rec["needs_password"] and auth.enabled():
            if not writemode._password_ok(password):
                raise PasswordNeeded("Enter your KopiaLens password to approve this.")
            pw_ok = True
        rec.update(status=APPROVED if approve else DENIED, decided_by=actor, decided=time.time(),
                   password_ok=pw_ok)
        out = _public(rec)
    audit.audit("executed", "approval-approve" if approve else "approval-deny",
                summary={"id": approval_id, "label": rec["label"], "token": rec["token_name"]})
    return out


def password_confirmed() -> bool:
    """True inside a request running on an approval whose approver entered the password."""
    return _CONFIRMED.get()


# ---------------------------------------------------------------------------- labels

def _repo(repo) -> str:
    if not repo:
        return ""
    rec = config.repo_record(str(repo)) if str(repo) in config.REPOS else {}
    return rec.get("short_name") or rec.get("name") or f"repo {repo}"


def _n(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _size(b) -> str:
    try:
        b = float(b or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return ""


def _job(body: dict) -> tuple[str, str]:
    kind = body.get("kind") or ""
    params = body.get("params") or {}
    where = _repo(body.get("repo"))
    if kind == "remove_files" or (kind == "policy_write" and (params.get("then") or {}).get("kind") == "remove_files"):
        p = params if kind == "remove_files" else (params.get("then") or {}).get("params") or {}
        from ..api.actions import previewed_plan
        plan = previewed_plan(str(p.get("plan_token") or "")) or {}
        if plan.get("file_count") is not None:
            return (f"Remove {_n(plan['file_count'], 'file')} from the backups in {where}",
                    f"Frees about {_size(plan.get('physical'))} of backup storage "
                    f"({_size(plan.get('logical'))} of file data).")
        return f"Remove files from the backups in {where}", ""
    if kind == "snapshot_delete" or (kind == "policy_write" and (params.get("then") or {}).get("kind") == "snapshot_delete"):
        p = params if kind == "snapshot_delete" else (params.get("then") or {}).get("params") or {}
        return f"Delete {_n(len(p.get('snapshot_ids') or []), 'snapshot')} from {where}", ""
    if kind == "prune_versions":
        return f"Prune old file versions in {where}", ""
    if kind == "source_delete":
        return f"Delete every snapshot of {params.get('source') or 'a source'} in {where}", ""
    if kind == "policy_write":
        targets = ", ".join(str(w.get("target")) for w in params.get("writes") or [])
        return f"Save policies in {where} that lower retention ({targets})", ""
    from . import jobs
    label = (jobs.KINDS.get(kind) or {}).get("label") or kind
    return f"Start \"{label}\" in {where}", ""


def describe(method: str, route: str, body: dict, query: dict) -> tuple[str, str, bool]:
    """(label, detail, needs_password) for the approval list."""
    key = f"{method} {route}"
    if key == "POST /api/jobs/start":
        label, detail = _job(body)
        return label, detail, False
    if key == "POST /api/write-mode":
        from . import writemode
        new = body.get("mode")
        target = _repo(body.get("repo")) or "every repository"
        after = new if body.get("repo") is None else (new or writemode.global_mode())
        name = writemode.LABELS.get(new, "follow the app-wide mode") if new else "follow the app-wide mode"
        return f"Set the write mode of {target} to {name}", "", after == "full"
    if key == "POST /api/policies/save":
        return (f"Save a policy for {body.get('target')} in {_repo(body.get('repo'))} that lowers "
                "retention or removes the policy"), "", False
    if key == "POST /api/settings":
        keys = sorted(set(body.get("kopialens") or {}) | (set(body) - {"kopialens", "backblaze"}))
        return f"Change settings: {', '.join(keys)}", "", False
    simple = {
        "POST /api/actions/remove-files": "Remove files from the backups",
        "POST /api/actions/remove-bat": "Create a script that removes files from the backups",
        "POST /api/actions/snapshot-delete": "Delete snapshots",
        "POST /api/policies/restore": f"Restore the policies of {_repo(body.get('repo'))} from a backup",
        "POST /api/client/scripts": "Allow snapshot scripts (kopia runs commands on this computer)",
        "POST /api/engine/pause": "Pause all backups",
        "POST /api/engine/source-pause": f"Pause backups of {body.get('source') or 'a source'}",
        "POST /api/config-backups/import/apply": "Import a settings backup",
        "POST /api/config-backups/{backup_id}/restore": "Restore a settings backup",
        "POST /api/notify/profile/delete": "Delete a notification profile",
        "POST /api/access": "Change remote access",
        "DELETE /api/setup/repos/{repo}": "Remove a repository from KopiaLens",
        "POST /api/setup/kopia": "Change the kopia program KopiaLens runs",
        "DELETE /api/licence": "Remove the licence",
        "POST /api/encryption/level": f"Lower index encryption to {body.get('level')}",
        "POST /api/encryption/remember": "Remember the encryption password on this computer",
        "POST /api/encryption/reset": "Reset index encryption",
        "POST /api/account/enabled": "Turn sign-in off",
        "POST /api/account/username": "Change the account name",
        "POST /api/account/sign-out-all": "Sign everyone out",
    }
    if key in simple:
        return simple[key], "", False
    if route.startswith("/api/devnotes/"):
        return "Change review notes", "", False
    return key, "", False
