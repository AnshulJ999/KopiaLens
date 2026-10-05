"""The desktop shell's feed: one status call for the tray, and the tray-owned preferences.

The shell authenticates with the per-launch secret it handed the server (``auth.shell_request_ok``),
so these work while sign-in is on. Only the paths in ``auth.SHELL_PATHS`` accept that secret.
"""
from __future__ import annotations

import sys

from fastapi import APIRouter, HTTPException

from config import APP_VERSION, DATA_DIR
from ..core import jobs, netaccess
from ..core.applog import LOG_DIR
from ..settings import load_settings, mutate_settings

router = APIRouter(prefix="/shell")

# Preferences the tray itself can change, with their allowed values (first = default).
PREFS = {
    "tray_click": ("window", "menu"),
    "tray_double_click": ("window", "none"),
    "notifications": ("warnings", "all", "off"),
}


def _prefs(kl: dict) -> dict:
    out = {k: (kl.get(k) if kl.get(k) in allowed else allowed[0]) for k, allowed in PREFS.items()}
    out["quit_indexing"] = "stop" if kl.get("quit_indexing") == "stop" else "finish"
    out["control_api"] = kl.get("control_api") is not False
    return out


@router.get("/state")
def state() -> dict:
    from .dashboard import index_freshness
    settings = load_settings()
    kl = settings.get("kopialens") or {}
    fresh = index_freshness()
    recent = [
        {k: j.get(k) for k in ("job_id", "kind_label", "label", "repo", "state", "error", "finished")}
        for j in jobs.list_jobs()[:20] if not j.get("dry")
    ]
    return {
        "version": APP_VERSION, "platform": sys.platform,
        "data_dir": DATA_DIR, "log_dir": LOG_DIR,
        "prefs": _prefs(kl), "last_repo": settings.get("last_repo"),
        "repos": fresh["repos"], "pause": fresh["pause"], "jobs": recent,
        "access": netaccess.STARTED.get("mode") or netaccess.mode(),
    }


@router.post("/open-code")
def open_code() -> dict:
    """A one-time code for the app's window (``/shell/open``); the window-only mode needs it."""
    return {"code": netaccess.issue_code()}


@router.post("/prefs")
def set_prefs(payload: dict) -> dict:
    bad = [k for k, v in payload.items() if k not in PREFS or v not in PREFS[k]]
    if bad:
        raise HTTPException(status_code=400, detail=f"Not a tray preference value: {', '.join(bad)}")
    mutate_settings(lambda s: s.setdefault("kopialens", {}).update(payload))
    return _prefs(load_settings().get("kopialens") or {})
