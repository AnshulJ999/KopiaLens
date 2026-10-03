"""Onboarding: whether the first-run flow is needed, its progress marker, and the setup-code entry.

Onboarding shows when no kopia repository is set up, or while a started onboarding has not been
finished (so a reload mid-way resumes it). An install that already has repositories and never ran
onboarding counts as set up.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import is_demo

from ..core import auth, kopiabin, repos, users
from ..index import crypto
from ..settings import load_settings, mutate_settings, raw_kopialens_settings

router = APIRouter(tags=["onboarding"])


class CodeRequest(BaseModel):
    code: str = ""


class StartRequest(BaseModel):
    repos: list[str] = []


def needed() -> bool:
    if raw_kopialens_settings().get("onboarded") is False:
        return True
    return not any(r.get("kind", "kopia") == "kopia" for r in repos.all_records())


def _set_onboarded(value: bool) -> None:
    mutate_settings(lambda s: s.setdefault("kopialens", {}).__setitem__("onboarded", value))


@router.get("/onboarding/state")
def state(request: Request) -> dict:
    """Everything the onboarding screens need before any other call. Reachable while setup is
    closed, so it carries nothing about the repositories beyond whether any exist."""
    first_run = auth.first_run_state()
    unlocked = not first_run["needs_code"] or auth.setup_cookie_ok(request.cookies.get(auth.SETUP_COOKIE))
    out = {
        "needed": needed(),
        "first_run": first_run,
        "setup_unlocked": unlocked,
        "profile": auth.profile(),
    }
    if not unlocked:
        return out
    kl = load_settings().get("kopialens", {})
    out.update({
        "protection_chosen": auth.protection_chosen(),
        "has_account": users.has_accounts(),
        "encryption_level": crypto.level() if crypto.configured() else "off",
        "kopia": kopiabin.info(with_version=False),
        "index_mode": kl.get("index_mode", "daily"),
        "index_time": kl.get("index_time", "23:00"),
        "allowed_hosts": kl.get("allowed_hosts") or [],
        "repos": [repos.detail(r) for r in repos.all_records()],
    })
    return out


@router.post("/onboarding/setup-code")
def setup_code(payload: CodeRequest) -> JSONResponse:
    """Check the setup code once and remember it in this browser for the rest of setup."""
    if not auth.first_run_state()["needs_code"]:
        return JSONResponse({"ok": True})
    if not auth.first_run_allowed(payload.code):
        raise HTTPException(status_code=403, detail="That setup code is wrong. It's in the KopiaLens log.")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.SETUP_COOKIE, auth.setup_cookie_value(), httponly=True, samesite="strict",
                    max_age=auth.SETUP_WINDOW * 4)
    return resp


@router.post("/onboarding/begin")
def begin() -> dict:
    """Mark onboarding as in progress, so a reload after the first repository resumes it."""
    if needed():
        _set_onboarded(False)
    return {"ok": True, "needed": needed()}


@router.post("/onboarding/finish")
def finish() -> dict:
    if not any(r.get("kind", "kopia") == "kopia" for r in repos.all_records()):
        raise HTTPException(status_code=400, detail="Connect a repository first.")
    _set_onboarded(True)
    return {"ok": True}


@router.post("/onboarding/start-indexing")
def start_indexing(payload: StartRequest) -> dict:
    """Index the new repositories now, one after another: each is queued with no wait, and the
    queue starts the next as soon as the previous pass finishes."""
    from ..core import reindexq
    known = [r for r in payload.repos if repos.exists(r)]
    if not known:
        raise HTTPException(status_code=400, detail="No repository to index.")
    # browse-only repos have no index; the sample data comes indexed
    wanted = [r for r in known if not repos.via_server(r) and not is_demo(r)]
    if not wanted:
        return {"ok": True, "queued": [], "started": False}
    for rid in wanted:
        reindexq.request(rid, wait=0, reason="first")
    first = reindexq.run_now(wanted[0])
    return {"ok": True, "queued": wanted, "started": first.get("started", False)}
