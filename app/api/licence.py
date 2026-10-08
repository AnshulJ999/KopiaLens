"""Settings -> Licence, and what the frontend needs to mark Pro buttons.

GET /api/licence answers both: the stored licence, the Pro features this install has not
unlocked, and each feature's upgrade line."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import DEMO_INSTANCE, LICENCE_SERVICE_URL, PRICING_URL

from ..licensing import features, licence

router = APIRouter(tags=["licence"])


class TextBody(BaseModel):
    text: str = ""


class KeyBody(BaseModel):
    key: str = ""


class SessionBody(BaseModel):
    session: str = ""


def _state() -> dict:
    return {**licence.status(), "locked": features.locked(), "features": features.PRO,
            "pricing_url": PRICING_URL, "offline_url": LICENCE_SERVICE_URL.rstrip("/") + "/offline",
            "check_host": urlparse(LICENCE_SERVICE_URL).netloc,
            "demo": DEMO_INSTANCE}


def _run(fn, *args):
    try:
        return fn(*args)
    except licence.LicenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/licence")
def get_licence() -> dict:
    return _state()


@router.post("/licence")
def add_licence(body: TextBody) -> dict:
    _run(licence.add, body.text)
    return _state()


@router.post("/licence/activate")
def activate(body: KeyBody) -> dict:
    _run(licence.activate, body.key)
    return _state()


@router.post("/licence/renew")
def renew_licence() -> dict:
    _run(licence.renew_now)
    return _state()


@router.post("/licence/patreon/start")
def patreon_start() -> dict:
    return _run(licence.patreon_start)


@router.post("/licence/patreon/claim")
def patreon_claim(body: SessionBody) -> dict:
    if not body.session:
        raise HTTPException(status_code=400, detail="No sign-in in progress.")
    r = _run(licence.patreon_claim, body.session)
    return {"pending": True} if r.get("pending") else {"pending": False, **_state()}


@router.delete("/licence")
def remove_licence(id: str = "") -> dict:
    licence.remove(id)
    return _state()
