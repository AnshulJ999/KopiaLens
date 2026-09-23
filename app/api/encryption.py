"""Encryption routes (): status, setup, unlock, lock, password/recovery, the switch.

This is the backend the onboarding screens and the Settings section () call. Onboarding
POLICY - the default-ticked box, the pros and cons copy, the Docker choice - is the UI's job;
these routes only do what they say, and every one of them answers with the current status so the
caller never has to guess what state it left the install in.

Conventions that matter:

  * ``setup`` is refused while a keystore exists: a fresh data key would orphan everything
    already encrypted with the old one.
  * ``unlock`` says WHY it failed - a wrong password and a damaged keystore are different
    problems with different fixes.
  * The switch flips the setting and starts ONE job per repo. The job rewrites that repo's
    generation plus the side caches and reports progress through the normal job runner.
  * Login (``core/auth.py``) and encryption are separate switches; the same password may be used
    for both, or two different ones.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import jobs as jobrunner
from ..index import crypto, keyswitch, store
from ..settings import mutate_settings

router = APIRouter(tags=["encryption"])


class SetupRequest(BaseModel):
    password: str
    remember: bool = False
    recovery: bool = True                 # issue a recovery key, returned once


class UnlockRequest(BaseModel):
    password: str


class RecoveryRequest(BaseModel):
    recovery_key: str


class ChangePasswordRequest(BaseModel):
    old: str
    new: str


class ResetPasswordRequest(BaseModel):
    recovery_key: str
    new: str


class RememberRequest(BaseModel):
    on: bool


class SwitchRequest(BaseModel):
    repo: str = "1"
    on: bool


def _set_intent(on: bool) -> None:
    """Write the setting (the INTENT). The data follows through the switch, never on its own."""
    def _apply(settings: dict) -> None:
        settings.setdefault("kopialens", {})["encryption"] = bool(on)
    mutate_settings(_apply)


def _status() -> dict:
    repos = keyswitch.repo_states()
    return {
        "configured": crypto.configured(),
        "enabled": crypto.enabled(),
        "unlocked": crypto.unlocked(),
        "remembered": crypto.is_remembered(),
        "has_recovery": crypto.has_recovery(),
        "pending": [r["repo"] for r in repos if r["pending"]],
        "repos": repos,
    }


def _refuse(reason: str) -> HTTPException:
    """The reason codes, as messages a person can act on."""
    if reason == "wrong-password":
        return HTTPException(status_code=401, detail="Wrong password.")
    if reason == "corrupt-keystore":
        return HTTPException(status_code=500,
                             detail="The keystore file cannot be read. Restore it from a backup, "
                                    "or rebuild the index from kopia.")
    return HTTPException(status_code=409, detail="Encryption is not set up on this install.")


@router.get("/encryption/status")
def status() -> dict:
    """Everything the UI needs to draw the section: is it set up, on, unlocked, remembered;
    which repos still need a rewrite; and what each repo's data currently is."""
    return _status()


@router.post("/encryption/setup")
def setup(payload: SetupRequest) -> dict:
    """Create the keystore and turn the intent on. Existing data is NOT rewritten by this call -
    the response's ``pending`` lists the repos that need a switch."""
    if crypto.configured():
        raise HTTPException(status_code=409,
                            detail="Encryption is already set up. Use Change password, or the "
                                   "recovery key if the password is lost.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Use a password of at least 8 characters.")
    try:
        recovery_key = crypto.setup(payload.password, remember=payload.remember,
                                    with_recovery=payload.recovery)
    except RuntimeError as exc:               # a second keystore would orphan the first data key
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _set_intent(True)
    return {"ok": True, "recovery_key": recovery_key, "status": _status()}


@router.post("/encryption/unlock")
def unlock(payload: UnlockRequest) -> dict:
    ok, reason = crypto.unlock_detailed(payload.password)
    if not ok:
        raise _refuse(reason)
    return {"ok": True, "status": _status()}


@router.post("/encryption/unlock-recovery")
def unlock_recovery(payload: RecoveryRequest) -> dict:
    ok, reason = crypto.unlock_with_recovery_detailed(payload.recovery_key)
    if not ok:
        raise _refuse(reason)
    return {"ok": True, "status": _status()}


@router.post("/encryption/lock")
def lock() -> dict:
    """Forget the data key in this process and close every open store connection.

    Nothing can read the index until the next unlock; an install that remembered the key gets it
    back at the next app start (or the next unlock), which is what "remember on this machine"
    means."""
    crypto.lock()
    store.close_all()
    return {"ok": True, "status": _status()}


@router.post("/encryption/change-password")
def change_password(payload: ChangePasswordRequest) -> dict:
    if len(payload.new) < 8:
        raise HTTPException(status_code=400, detail="Use a password of at least 8 characters.")
    ok, reason = crypto.change_password_detailed(payload.old, payload.new)
    if not ok:
        raise _refuse(reason)
    return {"ok": True, "status": _status()}


@router.post("/encryption/reset-password")
def reset_password(payload: ResetPasswordRequest) -> dict:
    """Forgot the password: set a new one with the recovery key. The recovery key keeps working."""
    if len(payload.new) < 8:
        raise HTTPException(status_code=400, detail="Use a password of at least 8 characters.")
    ok, reason = crypto.reset_password_with_recovery(payload.recovery_key, payload.new)
    if not ok:
        raise _refuse(reason)
    return {"ok": True, "status": _status()}


@router.post("/encryption/recovery-key")
def new_recovery_key(payload: UnlockRequest) -> dict:
    """Issue a fresh recovery key. The password proves the caller may have one."""
    ok, reason = crypto.unlock_detailed(payload.password)
    if not ok:
        raise _refuse(reason)
    key = crypto.new_recovery_key()
    if not key:
        raise HTTPException(status_code=409, detail="Encryption is not set up on this install.")
    return {"ok": True, "recovery_key": key, "status": _status()}


@router.post("/encryption/remember")
def remember(payload: RememberRequest) -> dict:
    """Turn "remember on this machine" on or off (the OS keychain holds the data key)."""
    if payload.on and not crypto.unlocked():
        raise HTTPException(status_code=409, detail="Unlock KopiaLens first.")
    if not crypto.remember_on_this_machine(payload.on):
        raise HTTPException(status_code=500,
                            detail="The OS keychain refused to store the key. The nightly will "
                                   "need an unlock after a restart.")
    return {"ok": True, "status": _status()}


@router.post("/encryption/switch")
def switch(payload: SwitchRequest) -> dict:
    """Move one repo's data (and the side caches) between plaintext and encrypted.

    Flips the intent first, then runs the rewrite as a job - the job's progress appears wherever
    jobs appear. Both directions need the key, one to write the new files and the other to read
    the old ones.
    """
    from config import REPOS, repo_kind
    if payload.repo not in REPOS or repo_kind(payload.repo) == "backblaze":
        raise HTTPException(status_code=404, detail=f"Unknown repo {payload.repo}.")
    if not crypto.unlocked():
        raise HTTPException(status_code=409, detail="Unlock KopiaLens first.")
    if payload.on and not crypto.configured():
        raise HTTPException(status_code=409, detail="Set up encryption before turning it on.")
    _set_intent(payload.on)
    try:
        job = jobrunner.start_job("encryption_switch", payload.repo, {"on": payload.on})
    except jobrunner.JobRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": job["job_id"], "job": jobrunner.job_status(job["job_id"], after=0),
            "status": _status()}
