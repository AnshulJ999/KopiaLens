"""Account routes (,): the first account, password, username, sign-in on/off,
sign out all devices, and the link between the sign-in password and the encryption password.

v1 acts on the one admin account (``users.primary()``). When the account's password is also the
encryption password (``unlocks_encryption``), a password change updates both, signing in unlocks
a locked index, and splitting or re-joining the two is explicit. Every route answers with the
current state so the caller never has to guess.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..core import auth, users
from ..core.applog import get_logger
from ..index import crypto

log = get_logger("account")
router = APIRouter(tags=["account"])


class CreateRequest(BaseModel):
    username: str = "admin"
    password: str
    code: str = ""                        # the setup code, once the first-run window has closed


class PasswordRequest(BaseModel):
    current: str
    new: str


class UsernameRequest(BaseModel):
    username: str


class EnabledRequest(BaseModel):
    on: bool
    code: str = ""


class SplitRequest(BaseModel):
    current: str                          # the shared password (sign-in + encryption today)
    encryption_password: str              # the new, separate encryption password


class JoinRequest(BaseModel):
    password: str                         # the sign-in password, which becomes the shared one
    encryption_password: str              # the current encryption password


def _state() -> dict:
    user = users.primary()
    return {
        "has_account": user is not None,
        "username": user["username"] if user else None,
        "sign_in_on": auth.switched_on(),
        "protected": auth.enabled(),
        "unlocks_encryption": bool(user and user.get("unlocks_encryption")),
        "session_days": auth.session_days(),
        "session_choices": list(auth.SESSION_CHOICES),
        "encryption": {"configured": crypto.configured(), "enabled": crypto.enabled(),
                       "unlocked": crypto.unlocked()},
        "first_run": auth.first_run_state(),
    }


def _account() -> dict:
    user = users.primary()
    if user is None:
        raise HTTPException(status_code=409, detail="No account yet. Create one first.")
    return user


def _check_new_password(password: str) -> None:
    if len(password) < auth.MIN_PASSWORD:
        raise HTTPException(status_code=400,
                            detail=f"Use a password of at least {auth.MIN_PASSWORD} characters.")


def _first_run_gate(code: str) -> None:
    if not auth.first_run_allowed(code):
        raise HTTPException(status_code=403,
                            detail="Setup is closed. Enter the setup code from the KopiaLens log.")


def _signed_in(body: dict, user: dict, persist: bool = True) -> JSONResponse:
    resp = JSONResponse(body)
    resp.set_cookie(auth.COOKIE_NAME, auth.issue_session(user, persist), **auth.cookie_kwargs(persist))
    return resp


def unlock_if_linked(user: dict, password: str) -> None:
    """Signing in with the shared password also unlocks a locked index."""
    if not user.get("unlocks_encryption") or not crypto.configured() or crypto.unlocked():
        return
    ok, reason = crypto.unlock_detailed(password)
    if not ok:
        log.warning("the sign-in password did not unlock the index (%s)", reason)


def link_if_matches(password: str) -> None:
    """Called after encryption is set up: the same password as the account links the two."""
    user = users.primary()
    if user and auth.verify_password(password, user["password_hash"]):
        users.set_unlocks_encryption(user["username"], True)


def unlink() -> None:
    """The encryption password changed on its own: the two are separate from now on."""
    user = users.primary()
    if user and user.get("unlocks_encryption"):
        users.set_unlocks_encryption(user["username"], False)


@router.get("/account")
def state() -> dict:
    return _state()


@router.post("/account/create")
def create(payload: CreateRequest):
    """The first account (onboarding's Protect screen, or Settings before one exists)."""
    if users.has_accounts():
        raise HTTPException(status_code=409, detail="An account already exists.")
    _first_run_gate(payload.code)
    problem = users.username_problem(payload.username.strip())
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    _check_new_password(payload.password)
    # An existing encryption password that matches becomes shared (and unlocks the index).
    linked = crypto.configured() and crypto.unlock_detailed(payload.password)[0]
    user = users.create(payload.username, auth.hash_password(payload.password),
                        unlocks_encryption=linked)
    auth.set_enabled(True)
    return _signed_in({"ok": True, "state": _state()}, user)


@router.post("/account/password")
def change_password(payload: PasswordRequest):
    """Change the sign-in password - and the encryption password too while they are shared.
    Every other browser is signed out; this one gets a fresh session."""
    user = _account()
    if not auth.verify_password(payload.current, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Your current password is wrong.")
    _check_new_password(payload.new)
    shared = bool(user.get("unlocks_encryption")) and crypto.configured()
    if shared:
        ok, reason = crypto.change_password_detailed(payload.current, payload.new)
        if not ok:
            raise HTTPException(status_code=409, detail=(
                "Couldn't change the encryption password, so nothing was changed."
                if reason != "wrong-password" else
                "Your sign-in password no longer unlocks encryption. Use separate passwords instead."))
    try:
        user = users.set_password_hash(user["username"], auth.hash_password(payload.new))
    except Exception:
        if shared:  # keep the two equal: put the encryption password back
            crypto.change_password_detailed(payload.new, payload.current)
        raise
    return _signed_in({"ok": True, "both": shared, "state": _state()}, user)


@router.post("/account/username")
def change_username(payload: UsernameRequest, request: Request):
    user = _account()
    name = payload.username.strip()
    problem = users.username_problem(name)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    try:
        user = users.rename(user["username"], name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # The cookie carries the username: re-issue it for this browser, keeping its persistence.
    persist = auth.session_persists(request.cookies.get(auth.COOKIE_NAME))
    return _signed_in({"ok": True, "state": _state()}, user, persist)


@router.post("/account/sign-out-all")
def sign_out_all():
    """Every existing session of the account stops working, this browser's included."""
    user = _account()
    users.bump_epoch(user["username"])
    resp = JSONResponse({"ok": True, "state": _state()})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@router.post("/account/enabled")
def set_enabled(payload: EnabledRequest):
    """Sign-in on/off. Off keeps the account; on needs one. Switching off before any protection
    choice is onboarding's "skip protection", so it is under the first-run gate."""
    if payload.on:
        user = _account()
        auth.set_enabled(True)
        return _signed_in({"ok": True, "state": _state()}, user)   # this browser stays signed in
    if not auth.protection_chosen():
        _first_run_gate(payload.code)
    auth.set_enabled(False)
    return {"ok": True, "state": _state()}


@router.post("/account/encryption/split")
def split(payload: SplitRequest):
    """Give encryption its own password; the sign-in password stays as it is."""
    user = _account()
    if not user.get("unlocks_encryption"):
        raise HTTPException(status_code=409, detail="The passwords are already separate.")
    if not auth.verify_password(payload.current, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Your current password is wrong.")
    _check_new_password(payload.encryption_password)
    ok, _reason = crypto.change_password_detailed(payload.current, payload.encryption_password)
    if not ok:
        raise HTTPException(status_code=409, detail="Couldn't change the encryption password.")
    users.set_unlocks_encryption(user["username"], False)
    return {"ok": True, "state": _state()}


@router.post("/account/encryption/join")
def join(payload: JoinRequest):
    """Use the sign-in password for encryption again (one password for both)."""
    user = _account()
    if user.get("unlocks_encryption"):
        raise HTTPException(status_code=409, detail="One password already does both.")
    if not crypto.configured():
        raise HTTPException(status_code=409, detail="Encryption is not set up.")
    if not auth.verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Your sign-in password is wrong.")
    ok, reason = crypto.change_password_detailed(payload.encryption_password, payload.password)
    if not ok:
        raise HTTPException(status_code=401 if reason == "wrong-password" else 409,
                            detail="The encryption password is wrong." if reason == "wrong-password"
                            else "Couldn't change the encryption password.")
    users.set_unlocks_encryption(user["username"], True)
    return {"ok": True, "state": _state()}
