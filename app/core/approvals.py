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
    return {k: rec[k] for k in ("id", "token_id", "token_name", "token_mode", "method", "route", "label",
                                "detail", "facts", "sample", "sample_total", "request", "needs_password",
                                "created", "expires", "status", "decided_by", "decided")}


def _mode_label(mode) -> str:
    from . import writemode
    return writemode.LABELS.get(mode, mode or "")


def _request_text(method: str, path: str, body: dict, query: dict) -> str:
    q = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
    text = f"{method} {path}{'?' + q if q else ''}"
    if body:
        text += "\n" + json.dumps(body, indent=2, ensure_ascii=False, default=str)
    return text[:4000]


def request(tok: dict, method: str, route: str, path: str, body: dict, query: dict) -> dict:
    """The waiting request for this exact call: an existing one from the same token, or a new one."""
    fp = _fingerprint(method, path, body, query)
    with _LOCK:
        _prune(time.time())
        for rec in _ITEMS.values():
            if rec["token_id"] == tok["id"] and rec["fp"] == fp and rec["status"] in (WAITING, APPROVED):
                return _public(rec)
    d = describe(method, route, path, body, query)     # may ask kopia (a policy's retention): no lock
    now = time.time()
    rec = {"id": secrets.token_hex(4), "token_id": tok["id"], "token_name": tok["name"],
           "token_mode": _mode_label((tok.get("scopes") or {}).get("write_mode")), "method": method, "route": route,
           "fp": fp, "label": d["label"], "detail": d["detail"], "facts": d["facts"], "sample": d["sample"],
           "sample_total": d["sample_total"], "request": _request_text(method, path, body, query),
           "needs_password": d["needs_password"], "created": now, "expires": now + TTL,
           "status": WAITING, "decided_by": None, "decided": None}
    label = rec["label"]
    with _LOCK:
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


# ---------------------------------------------------------------------------- what the user sees

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


def _val(v) -> str:
    if v is None or v == "":
        return "not set"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or "none"
    return str(v)


def _change(before, after) -> str:
    return f"{_val(before)} → {_val(after)}"


def _delete_job(kind: str, p: dict, where: str, out: dict) -> None:
    if kind == "remove_files":
        from ..api.actions import previewed_plan
        plan = previewed_plan(str(p.get("plan_token") or "")) or {}
        n = plan.get("file_count")
        out["label"] = (f"Remove {_n(n, 'file')} from the backups in {where}" if n is not None
                        else f"Remove files from the backups in {where}")
        if n is not None:
            out["facts"] += [["Files", f"{n:,}"], ["Frees", f"{_size(plan.get('physical'))} of backup storage"],
                             ["File data", _size(plan.get("logical"))]]
            src = plan.get("source")
            out["facts"].append(["Backed-up folders", "every one" if src is None else _n(len(src), "folder")])
            extra = int(plan.get("filename_extra_count") or 0) + int(plan.get("content_extra_count") or 0)
            if extra:
                out["facts"].append(["Also removed with them", _n(extra, "other file") + " (same name or same content)"])
            out["detail"] = f"Frees about {_size(plan.get('physical'))} of backup storage."
            out["sample"] = list(plan.get("sample") or [])
            out["sample_total"] = n
        else:
            out["facts"].append(["Plan", "expired or not found: the removal will be refused"])
    elif kind == "snapshot_delete":
        ids = list(p.get("snapshot_ids") or [])
        out["label"] = f"Delete {_n(len(ids), 'snapshot')} from {where}"
        out["facts"].append(["Snapshots", f"{len(ids):,}"])
        out["sample"], out["sample_total"] = ids[:10], len(ids)
    elif kind == "prune_versions":
        out["label"] = f"Prune old file versions in {where}"
    elif kind == "source_delete":
        out["label"] = f"Delete every snapshot of {p.get('source') or 'a source'} in {where}"
        out["facts"].append(["Source", str(p.get("source") or "")])


def _policy_facts(repo, writes: list, out: dict) -> None:
    for w in writes:
        target, changes = w.get("target"), w.get("changes") or {}
        if changes.get("delete_policy"):
            out["facts"].append([str(target), "the whole policy is removed (it falls back to its parent's)"])
            continue
        try:
            from ..api.policies import Changes, _build_diff
            diff = _build_diff(str(repo), str(target), Changes(**changes), price=False)
        except Exception:  # noqa: BLE001
            out["facts"].append([str(target), "couldn't read the current policy"])
            continue
        for fc in diff.get("field_changes") or []:
            out["facts"].append([f"{target}: {fc['field'].split('.')[-1]}",
                                 _change(fc.get("effective_before"), fc.get("effective_after"))])


def describe(method: str, route: str, path: str, body: dict, query: dict) -> dict:
    """What the approval shows: a one-line label, facts with real values, a sample for deletes."""
    key = f"{method} {route}"
    repo = body.get("repo") or query.get("repo")
    if key == "DELETE /api/setup/repos/{repo}":
        repo = path.rstrip("/").rsplit("/", 1)[-1]
    where = _repo(repo)
    out = {"label": key, "detail": "", "facts": [], "sample": [], "sample_total": 0, "needs_password": False}
    if where:
        out["facts"].append(["Repository", where])

    if key == "POST /api/jobs/start":
        kind, params = body.get("kind") or "", body.get("params") or {}
        then = params.get("then") or {}
        if kind in ("remove_files", "snapshot_delete", "prune_versions", "source_delete"):
            _delete_job(kind, params, where, out)
        elif kind == "policy_write" and then.get("kind"):
            _delete_job(then["kind"], then.get("params") or {}, where, out)
            out["label"] = "Save policies, then: " + out["label"][0].lower() + out["label"][1:]
            _policy_facts(repo, params.get("writes") or [], out)
        elif kind == "policy_write":
            out["label"] = f"Save policies in {where} that lower retention or remove a policy"
            _policy_facts(repo, params.get("writes") or [], out)
        else:
            from . import jobs
            out["label"] = f"Start \"{(jobs.KINDS.get(kind) or {}).get('label') or kind}\" in {where}"
        return out
    if key == "POST /api/policies/save":
        out["label"] = f"Save a policy for {body.get('target')} in {where} that lowers retention or removes the policy"
        _policy_facts(repo, [{"target": body.get("target"), "changes": body.get("changes")}], out)
        return out
    if key == "POST /api/write-mode":
        from . import writemode
        new = body.get("mode")
        target = where or "every repository"
        after = new if body.get("repo") is None else (new or writemode.global_mode())
        out["label"] = f"Set the write mode of {target} to {writemode.LABELS.get(after, after)}"
        out["facts"].append(["Write mode", _change(writemode.LABELS.get(writemode.mode(body.get("repo"))),
                                                   writemode.LABELS.get(after, after))])
        out["needs_password"] = after == "full"
        return out
    if key == "POST /api/settings":
        from ..settings import load_settings
        cur = load_settings().get("kopialens") or {}
        new = body.get("kopialens") if isinstance(body.get("kopialens"), dict) else {}
        out["label"] = "Change settings: " + ", ".join(sorted(new) or sorted(body))
        out["facts"] += [[k, _change(cur.get(k), v)] for k, v in sorted(new.items())]
        return out
    if key == "POST /api/account/enabled":
        out["label"] = "Turn sign-in off"
        out["facts"].append(["Sign-in", _change(True, False)])
        return out
    if key == "POST /api/access":
        from . import netaccess
        out["label"] = "Change remote access"
        out["facts"] += [[k, _change(netaccess.mode() if k == "access" else None, v)]
                         for k, v in sorted(body.items()) if v is not None]
        return out
    if key == "POST /api/encryption/level":
        from ..index import crypto
        out["label"] = f"Lower index encryption to {body.get('level')}"
        out["facts"].append(["Index encryption", _change(crypto.level(), body.get("level"))])
        return out
    if key in ("POST /api/engine/source-pause",):
        out["facts"].append(["Source", str(body.get("source") or "")])
    simple = {
        "POST /api/actions/remove-files": "Remove files from the backups",
        "POST /api/actions/remove-bat": "Create a script that removes files from the backups",
        "POST /api/actions/snapshot-delete": "Delete snapshots",
        "POST /api/policies/restore": f"Restore the policies of {where} from a backup",
        "POST /api/client/scripts": "Allow snapshot scripts (kopia runs commands on this computer)",
        "POST /api/engine/pause": "Pause all backups",
        "POST /api/engine/source-pause": f"Pause backups of {body.get('source') or 'a source'}",
        "POST /api/config-backups/import/apply": "Import a settings backup",
        "POST /api/config-backups/{backup_id}/restore": "Restore a settings backup",
        "POST /api/notify/profile/delete": "Delete a notification profile",
        "DELETE /api/setup/repos/{repo}": f"Remove {where or 'a repository'} from KopiaLens",
        "POST /api/setup/kopia": "Change the kopia program KopiaLens runs",
        "DELETE /api/licence": "Remove the licence",
        "POST /api/encryption/remember": "Remember the encryption password on this computer",
        "POST /api/encryption/reset": "Reset index encryption",
        "POST /api/account/username": "Change the account name",
        "POST /api/account/sign-out-all": "Sign everyone out",
    }
    if key in simple:
        out["label"] = simple[key]
    elif route.startswith("/api/devnotes/"):
        out["label"] = "Change review notes"
    return out
