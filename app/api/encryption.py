"""Encryption routes (): status, setup, unlock, lock, password/recovery, the switch.

This is the backend the onboarding screens and the Settings section () call. Onboarding
POLICY - the default-ticked box and its level, the pros and cons copy, the Docker choice -
is the UI's job; these routes only do what they say, and every one of them answers with the
current status so the caller never has to guess what state it left the install in.

The three levels (Off / Index / Full) are the one user-facing choice:
  off        nothing
  index      the index generations + side caches + policy caches (the full map of names)
  full       the above + the small JSON files, audit/history, logs, policy backups, Backblaze
`level` moves the whole install; every part of it is a verified rewrite, never a flag flip.

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
    level: str = "index"                   # onboarding's default level


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


class LevelRequest(BaseModel):
    level: str = "full"
    password: str = ""                    # the encryption password, needed to lower the level


def _set_level(level: str) -> None:
    """Write the setting (the INTENT). The data follows through the switch, never on its own."""
    def _apply(settings: dict) -> None:
        kl = settings.setdefault("kopialens", {})
        kl["encryption_level"] = level
        kl.pop("encryption", None)        # the old boolean is folded in by the REL30 step
    mutate_settings(_apply)


def _status() -> dict:
    from ..core import filecrypto
    repos = keyswitch.repo_states()
    bz = keyswitch.bz_state()
    try:
        files_pending = len(filecrypto.pending_files())
    except Exception:  # noqa: BLE001 - a status route never fails on a scan
        files_pending = 0
    return {
        "configured": crypto.configured(),
        "level": crypto.level(),
        "enabled": crypto.enabled(),
        "unlocked": crypto.unlocked(),
        "remembered": crypto.is_remembered(),
        "remember_state": crypto.remember_state(),   # waiting | unavailable | on | off
        "has_recovery": crypto.has_recovery(),
        "pending": [r["repo"] for r in repos if r["pending"]],
        "repos": repos,
        "backblaze": bz,
        "files_pending": files_pending,
        "set_aside": keyswitch.set_aside(),
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
    level = (payload.level or crypto.LEVEL_INDEX).strip().lower()
    if level not in crypto.LEVELS:            # checked BEFORE the keystore exists
        raise HTTPException(status_code=400, detail=f"Unknown level {payload.level!r}.")
    try:
        recovery_key = crypto.setup(payload.password, remember=payload.remember,
                                    with_recovery=payload.recovery)
    except RuntimeError as exc:               # a second keystore would orphan the first data key
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _set_level(level)
    from .account import link_if_matches
    link_if_matches(payload.password)         # the account's own password = one password for both
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
    from .account import unlink
    unlink()                                  # changed on its own: no longer the sign-in password
    return {"ok": True, "status": _status()}


@router.post("/encryption/reset-password")
def reset_password(payload: ResetPasswordRequest) -> dict:
    """Forgot the password: set a new one with the recovery key. The recovery key keeps working."""
    if len(payload.new) < 8:
        raise HTTPException(status_code=400, detail="Use a password of at least 8 characters.")
    ok, reason = crypto.reset_password_with_recovery(payload.recovery_key, payload.new)
    if not ok:
        raise _refuse(reason)
    from .account import unlink
    unlink()
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
    if crypto.remember_on_this_machine(payload.on) == "unavailable":
        raise HTTPException(status_code=500,
                            detail="This computer's keychain didn't accept the key. KopiaLens will "
                                   "ask for the password after a restart.")
    return {"ok": True, "status": _status()}


@router.post("/encryption/level")
def set_level(payload: LevelRequest) -> dict:
    """Move the whole install to an encryption level.

    Flips the intent, then runs ONE job that makes the data match: every index generation
    whose state differs, the file layer (small JSON, audit/history, logs, policy backups) and
    the Backblaze store. Each part is a verified rewrite, so a failure leaves everything
    readable and the job can simply be run again; a repo that is mid-index is deferred.
    """
    level = (payload.level or "").strip().lower()
    if level not in crypto.LEVELS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown level {payload.level!r}. One of: "
                                   f"{', '.join(crypto.LEVELS)}.")
    if level != crypto.LEVEL_OFF and not crypto.configured():
        raise HTTPException(status_code=409, detail="Set up encryption before turning it on.")
    if crypto.configured() and not crypto.unlocked():
        raise HTTPException(status_code=409, detail="Unlock KopiaLens first.")
    if crypto.configured() and crypto.LEVELS.index(level) < crypto.LEVELS.index(crypto.level()):
        if not payload.password:
            raise HTTPException(status_code=401, detail="Enter the encryption password to lower encryption.")
        ok, reason = crypto.unlock_detailed(payload.password)
        if not ok:
            raise _refuse(reason)
    _set_level(level)
    try:
        # 'all' is deliberate: this job is install-wide, not a repo write, and it defers any
        # repo that is busy rather than refusing to start.
        job = jobrunner.start_job("encryption_level", "all", {"level": level})
    except jobrunner.JobRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": job["job_id"], "job": jobrunner.job_status(job["job_id"], after=0),
            "status": _status()}


class ResetRequest(BaseModel):
    confirm: bool = False
    keep_copy: bool = False               # set the unreadable files aside instead of deleting them


@router.get("/encryption/reset-preview")
def reset_preview() -> dict:
    """Start over after losing the password AND the recovery key: what goes, what rebuilds."""
    return keyswitch.reset_preview()


@router.post("/encryption/reset")
def reset(payload: ResetRequest) -> dict:
    """Forget the keystore, remove (or set aside) what it encrypted, turn encryption off and
    re-index from kopia. The kopia repositories are never touched."""
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Starting over needs confirm=true "
                                                    "(see reset-preview first).")
    try:
        out = keyswitch.reset(keep_copy=payload.keep_copy)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**out, "status": _status()}
