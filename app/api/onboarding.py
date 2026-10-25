"""Onboarding: whether the first-run flow is needed, its progress marker, and the setup-code entry.

Onboarding shows when no repository is set up (kopia or Backblaze), or while a started onboarding
has not been finished (so a reload mid-way resumes it). An install that already has repositories
and never ran onboarding counts as set up.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import is_demo

from ..core import auth, basepath, host, kopiabin, repos, users
from ..index import crypto
from ..settings import load_settings, mutate_settings, raw_kopialens_settings

router = APIRouter(tags=["onboarding"])


class CodeRequest(BaseModel):
    code: str = ""


class StartRequest(BaseModel):
    repos: list[str] = []


class AnswerRequest(BaseModel):
    kopia_user: str = ""


class SkipRequest(BaseModel):
    step: str = ""
    code: str = ""


_STEPS = ("connect", "protect", "start", "summary")


def needed() -> bool:
    kl = raw_kopialens_settings()
    if kl.get("onboarded") is False:
        return True
    if kl.get("setup_skipped"):
        return False
    return not repos.all_records()


def skipped() -> bool:
    return bool(raw_kopialens_settings().get("setup_skipped"))


def kopia_user() -> str:
    """The Welcome screen's answer for usage stats: yes, no, or unanswered."""
    value = raw_kopialens_settings().get("kopia_user")
    return value if value in ("yes", "no") else "unanswered"


def _update(**values) -> None:
    def change(s):
        kl = s.setdefault("kopialens", {})
        for key, value in values.items():
            if value is None:
                kl.pop(key, None)
            else:
                kl[key] = value
    mutate_settings(change)


def _set_onboarded(value: bool) -> None:
    _update(onboarded=value)


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
        "ingress": basepath.ingress_enabled(),
        "docker": host.in_docker(),
        "setup_skipped": skipped(),
    }
    if not unlocked:
        return out
    kl = load_settings().get("kopialens", {})
    raw = raw_kopialens_settings()
    out.update({
        "rerun": bool(raw.get("setup_rerun")),
        "resume_step": raw.get("setup_step") if raw.get("setup_step") in _STEPS else "",
        "kopia_user": raw.get("kopia_user") if raw.get("kopia_user") in ("yes", "no") else "",
        "protection_chosen": auth.protection_chosen(),
        "has_account": users.has_accounts(),
        "encryption_level": crypto.level() if crypto.configured() else "off",
        "kopia": kopiabin.info(with_version=False),
        "index_mode": kl.get("index_mode", "daily"),
        "index_time": kl.get("index_time", "23:00"),
        "index_time_set": "index_time" in raw,
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
                    max_age=auth.SETUP_WINDOW * 4, secure=auth.request_secure())
    return resp


@router.post("/onboarding/begin")
def begin() -> dict:
    """Mark onboarding as in progress, so a reload after the first repository resumes it."""
    if needed():
        _set_onboarded(False)
    return {"ok": True, "needed": needed()}


@router.post("/onboarding/restart")
def restart() -> dict:
    """Run setup again on an install that is already set up; nothing is reset."""
    _update(onboarded=False, setup_rerun=True)
    return {"ok": True}


@router.post("/onboarding/answer")
def answer(payload: AnswerRequest) -> dict:
    """The Welcome screen's "Have you used kopia before?" answer."""
    if payload.kopia_user not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="Answer yes or no.")
    _update(kopia_user=payload.kopia_user)
    return {"ok": True}


@router.post("/onboarding/skip")
def skip(payload: SkipRequest, request: Request) -> dict:
    """Skip setup, or leave it for later: the app opens with a Finish setup banner. With no
    protection choice yet, sign-in is switched off, as Skip protection does."""
    if not auth.protection_chosen():
        if not auth.first_run_allowed(payload.code, request.cookies.get(auth.SETUP_COOKIE, "")):
            raise HTTPException(status_code=403, detail="Setup is closed. Enter the setup code from the KopiaLens log.")
        auth.set_enabled(False)
    _update(onboarded=True, setup_skipped=True, setup_rerun=None,
            setup_step=payload.step if payload.step in _STEPS else None)
    return {"ok": True}


@router.post("/onboarding/resume")
def resume() -> dict:
    """Finish setup from the banner: setup opens again at the step it was left on."""
    _update(onboarded=False, setup_rerun=None)
    return {"ok": True}


@router.post("/onboarding/close")
def close() -> dict:
    """Close "Run setup again" without finishing it; nothing it already saved is undone."""
    _update(onboarded=True, setup_rerun=None)
    return {"ok": True}


@router.post("/onboarding/dismiss")
def dismiss() -> dict:
    """Hide the Finish setup banner for good."""
    _update(setup_skipped=None, setup_step=None)
    return {"ok": True}


@router.post("/onboarding/finish")
def finish() -> dict:
    if not repos.all_records():
        raise HTTPException(status_code=400, detail="Connect a repository first.")
    _update(onboarded=True, setup_skipped=None, setup_step=None, setup_rerun=None)
    return {"ok": True}


@router.post("/onboarding/start-indexing")
def start_indexing(payload: StartRequest) -> dict:
    """Index the new repositories now, one after another: each is queued with no wait, and the
    queue starts the next as soon as the previous pass finishes."""
    from ..core import reindexq
    known = [r for r in payload.repos if repos.exists(r)]
    if not known:
        raise HTTPException(status_code=400, detail="No repository to index.")
    # browse-only repos have no index; the sample data comes indexed; a showcase copy plays it
    from config import DEMO_SHOW
    wanted = [r for r in known if not repos.via_server(r) and (DEMO_SHOW is not None or not is_demo(r))]
    if not wanted:
        return {"ok": True, "queued": [], "started": False}
    for rid in wanted:
        reindexq.request(rid, wait=0, reason="first")
    first = reindexq.run_now(wanted[0])
    return {"ok": True, "queued": wanted, "started": first.get("started", False)}
