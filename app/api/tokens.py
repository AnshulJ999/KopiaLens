"""API tokens (core/tokens): list, create and revoke. Tokens themselves can never reach these."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import audit, auth, tokens, writemode

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


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: str) -> dict:
    gone = tokens.revoke(token_id)
    if gone is None:
        raise HTTPException(status_code=404, detail="That token doesn't exist.")
    audit.audit("executed", "token-revoke", summary={"name": gone["name"]})
    return _state()
