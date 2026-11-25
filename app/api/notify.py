"""Notifications: kopia's own notification profiles (stored in the repository, shared with KopiaUI
and the command line) and KopiaLens's own webhook for events kopia does not send.

kopia keeps a profile's existing values for every option left out, so editing a profile without
retyping its password keeps the password. kopia takes these options only as command-line flags.
"""

from __future__ import annotations

import json
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config

from ..core import audit, kopiabin, notify
from ..core.kopia import _repo_args

router = APIRouter(tags=["notify"])

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SEVERITIES = ("verbose", "success", "report", "warning", "error")
_SECRETS = ("smtpPassword", "appToken", "userKey")
# profile field -> kopia flag, per method
_FLAGS = {
    "email": {"smtpServer": "--smtp-server", "smtpPort": "--smtp-port", "smtpIdentity": "--smtp-identity",
              "smtpUsername": "--smtp-username", "smtpPassword": "--smtp-password", "from": "--mail-from",
              "to": "--mail-to", "cc": "--mail-cc", "format": "--format"},
    "pushover": {"appToken": "--app-token", "userKey": "--user-key", "format": "--format"},
    "webhook": {"endpoint": "--endpoint", "method": "--method", "format": "--format"},
}


def _need(repo: str) -> None:
    if repo not in config.REPOS or config.repo_kind(repo) != "kopia" or config.is_demo(repo):
        raise HTTPException(status_code=404, detail=f"unknown repository: {repo}")


def _kopia(repo: str, args: list[str], timeout: float = 90.0) -> subprocess.CompletedProcess:
    return subprocess.run([kopiabin.exe()] + _repo_args(repo) + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                          creationflags=_NO_WINDOW)


def _err(res: subprocess.CompletedProcess, fallback: str) -> str:
    lines = [ln.strip() for ln in (res.stderr or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "") or fallback


def _scrub(cfg: dict) -> dict:
    return {k: ("" if k in _SECRETS else v) for k, v in (cfg or {}).items()} | {
        k + "Set": bool((cfg or {}).get(k)) for k in _SECRETS if k in (cfg or {})}


@router.get("/notify/profiles")
def profiles(repo: str) -> dict:
    _need(repo)
    res = _kopia(repo, ["notification", "profile", "list", "--json", "--raw"])
    if res.returncode != 0:
        raise HTTPException(status_code=502, detail=_err(res, "kopia couldn't list the notification profiles."))
    try:
        raw = json.loads(res.stdout or "[]") or []
    except ValueError:
        raw = []
    out = []
    for p in raw:
        m = p.get("method") or {}
        out.append({"name": p.get("profile") or "", "type": m.get("type") or "",
                    "min_severity": p.get("minSeverity", 0), "config": _scrub(m.get("config") or {})})
    return {"profiles": out, "severities": SEVERITIES}


class ProfileRequest(BaseModel):
    repo: str
    name: str
    type: str                   # email | pushover | webhook
    min_severity: str = ""
    fields: dict = {}
    headers: list[str] = []     # webhook "Key: value"
    test: bool = False


@router.post("/notify/profile")
def save_profile(payload: ProfileRequest) -> dict:
    """Create or update a profile (``test`` also sends a test message first)."""
    _need(payload.repo)
    flags = _FLAGS.get(payload.type)
    name = (payload.name or "").strip()
    if flags is None:
        raise HTTPException(status_code=400, detail="Unknown notification type.")
    if not name or any(c in name for c in (chr(10), chr(13))):
        raise HTTPException(status_code=400, detail="Enter a profile name.")
    args = ["notification", "profile", "configure", payload.type, f"--profile-name={name}"]
    if payload.min_severity:
        if payload.min_severity not in SEVERITIES:
            raise HTTPException(status_code=400, detail="Unknown severity.")
        args.append(f"--min-severity={payload.min_severity}")
    for key, flag in flags.items():
        val = payload.fields.get(key)
        if val in (None, ""):
            continue                       # left out: kopia keeps the stored value
        if any(c in str(val) for c in (chr(10), chr(13))):
            raise HTTPException(status_code=400, detail=f"{key} can't contain a line break.")
        args.append(f"{flag}={val}")
    for h in payload.headers:
        if h.strip():
            args.append(f"--http-header={h.strip()}")
    if payload.test:
        args.append("--send-test-notification")
    res = _kopia(payload.repo, args)
    audit.audit("executed", "notification-profile", repo=payload.repo,
                summary={"profile": name, "type": payload.type, "test": payload.test}, rc=res.returncode)
    if res.returncode != 0:
        raise HTTPException(status_code=502, detail=_err(res, "kopia couldn't save the profile."))
    return {"ok": True}


class ProfileName(BaseModel):
    repo: str
    name: str


@router.post("/notify/profile/test")
def test_profile(payload: ProfileName) -> dict:
    _need(payload.repo)
    res = _kopia(payload.repo, ["notification", "profile", "test", f"--profile-name={payload.name}"])
    if res.returncode != 0:
        raise HTTPException(status_code=502, detail=_err(res, "The test message wasn't sent."))
    return {"ok": True}


@router.post("/notify/profile/delete")
def delete_profile(payload: ProfileName) -> dict:
    _need(payload.repo)
    res = _kopia(payload.repo, ["notification", "profile", "delete", f"--profile-name={payload.name}"])
    audit.audit("executed", "notification-profile-delete", repo=payload.repo, summary={"profile": payload.name},
                rc=res.returncode)
    if res.returncode != 0:
        raise HTTPException(status_code=502, detail=_err(res, "kopia couldn't delete the profile."))
    return {"ok": True}


@router.post("/notify/webhook/test")
def test_webhook() -> dict:
    """Send a test message through KopiaLens's own webhook (Settings > Notifications)."""
    try:
        notify.send("KopiaLens test message", "Notifications from KopiaLens reach you here.", "info", force=True)
    except Exception as exc:  # noqa: BLE001 - the message is the user's feedback
        raise HTTPException(status_code=502, detail=f"The test message wasn't sent: {exc}") from exc
    return {"ok": True}
