"""API tokens (core/tokens) and approvals (core/approvals). Tokens can never manage tokens or
decide an approval; a token reads only its own approval request."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import approvals, audit, auth, tokens, writemode

router = APIRouter()


def _state() -> dict:
    return {"tokens": tokens.list_tokens(), "modes": list(writemode.MODES), "labels": writemode.LABELS,
            "expiry_days": list(tokens.EXPIRY_DAYS), "default_expiry_days": tokens.DEFAULT_EXPIRY_DAYS,
            "default_mode": tokens.DEFAULT_MODE, "sign_in": auth.enabled()}


@router.get("/tokens")
def get_tokens() -> dict:
    return _state()


class CreateRequest(BaseModel):
    name: str
    mode: str = tokens.DEFAULT_MODE
    days: int = tokens.DEFAULT_EXPIRY_DAYS
    password: str | None = None


@router.post("/tokens")
def create_token(payload: CreateRequest) -> dict:
    """A token above Read-only asks for the KopiaLens password while sign-in is on."""
    if payload.mode != "read_only" and auth.enabled() and not writemode._password_ok(payload.password):
        raise HTTPException(status_code=401, detail="Enter your KopiaLens password to create this token.")
    try:
        secret, rec = tokens.create(payload.name, payload.mode, payload.days, auth.current_actor())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.audit("executed", "token-create", summary={"name": rec["name"], "mode": rec["mode"],
                                                    "expires": rec["expires"]})
    return {**_state(), "created": rec, "secret": secret}


class EditRequest(BaseModel):
    name: str | None = None
    mode: str | None = None
    days: int | None = None     # new expiry, counted from now (0 = never)
    password: str | None = None


@router.patch("/tokens/{token_id}")
def edit_token(token_id: str, payload: EditRequest) -> dict:
    """Raising the permission or extending the expiry asks for the password while sign-in is on."""
    old = next((t for t in tokens.list_tokens() if t["id"] == token_id), None)
    if old is None:
        raise HTTPException(status_code=404, detail="That token doesn't exist.")
    modes = list(writemode.MODES)
    raises = (payload.mode in modes and old["mode"] in modes
              and modes.index(payload.mode) > modes.index(old["mode"]))
    new_exp = None if payload.days in (None, 0) else time.time() + payload.days * 86400
    extends = (payload.days is not None and old["expires"] is not None
               and (new_exp is None or new_exp > old["expires"] + 3600))   # re-picking the same choice is not an extension
    if (raises or extends) and auth.enabled() and not writemode._password_ok(payload.password):
        raise HTTPException(status_code=401, detail="Enter your KopiaLens password to save this change.")
    try:
        res = tokens.update(token_id, payload.name, payload.mode, payload.days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if res is None:
        raise HTTPException(status_code=404, detail="That token doesn't exist.")
    before, after = res
    audit.audit("executed", "token-edit", summary={
        "name": after["name"], "renamed_from": before["name"] if before["name"] != after["name"] else None,
        "mode": after["mode"], "mode_before": before["mode"], "expires": after["expires"],
        "expires_before": before["expires"]})
    return _state()


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: str) -> dict:
    gone = tokens.revoke(token_id)
    if gone is None:
        raise HTTPException(status_code=404, detail="That token doesn't exist.")
    audit.audit("executed", "token-revoke", summary={"name": gone["name"]})
    return _state()


# ---- approvals

@router.get("/approvals")
def list_approvals() -> dict:
    return {"approvals": approvals.listing(), "waiting": approvals.waiting_count(), "ttl": approvals.TTL,
            "sign_in": auth.enabled()}


@router.get("/approvals/{approval_id}")
def approval_status(approval_id: str) -> dict:
    rec = approvals.status(approval_id, tokens.current())
    if rec is None:
        raise HTTPException(status_code=404, detail="There's no such approval request. It may have expired.")
    return rec


class DecideRequest(BaseModel):
    password: str | None = None


def _decide(approval_id: str, approve: bool, password: str | None) -> dict:
    try:
        rec = approvals.decide(approval_id, approve, auth.current_actor(), password)
    except approvals.PasswordNeeded as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if rec is None:
        raise HTTPException(status_code=404, detail="There's no such approval request. It may have expired.")
    return list_approvals()


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, payload: DecideRequest | None = None) -> dict:
    return _decide(approval_id, True, payload.password if payload else None)


@router.post("/approvals/{approval_id}/deny")
def deny(approval_id: str) -> dict:
    return _decide(approval_id, False, None)
