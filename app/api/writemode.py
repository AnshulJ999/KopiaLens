"""Write modes (core/writemode): the settings routes and the server-side gate every request passes."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..core import writemode

router = APIRouter()

_WRITES = {"POST", "PUT", "PATCH", "DELETE"}


async def gate(request: Request) -> None:
    """Refuse a write the repository's mode does not allow. Runs for every route (app dependency)."""
    if request.method not in _WRITES:
        return
    path = request.scope.get("path") or ""   # every gated route is a fixed path
    if (request.method, path) not in writemode.ROUTES:
        return
    body: dict = {}
    raw = await request.body()  # FastAPI has already read and cached it
    if raw:
        try:
            parsed = json.loads(raw)
            body = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            body = {}
    query = dict(request.query_params)
    need = writemode.route_need(request.method, path, body, query)
    if need is None:
        return
    repo = request.path_params.get("repo") or query.get("repo") or body.get("repo")
    if not writemode.allows(str(repo) if repo else None, need):
        raise HTTPException(status_code=403, detail=writemode.reason(str(repo) if repo else None, need))


@router.get("/write-mode")
def get_mode() -> dict:
    return writemode.state()


class ModeRequest(BaseModel):
    repo: str | None = None     # None = the app-wide mode
    mode: str | None = None     # a repository: None = follow the app-wide mode
    password: str | None = None


@router.post("/write-mode")
def set_mode(payload: ModeRequest) -> dict:
    import config
    if payload.repo is not None and (payload.repo not in config.REPOS or config.repo_kind(payload.repo) != "kopia"):
        raise HTTPException(status_code=404, detail=f"unknown repository: {payload.repo}")
    try:
        return writemode.set_mode(payload.repo, payload.mode, payload.password)
    except writemode.PasswordNeeded as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
