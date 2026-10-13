"""Write modes (core/writemode): the settings routes and the server-side gate every request passes."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..core import tokens, writemode

router = APIRouter()

_WRITES = {"POST", "PUT", "PATCH", "DELETE"}


_TEMPLATES: list = []


def _template(request: Request, path: str) -> str:
    """The full route template (``/api/setup/repos/{repo}``). FastAPI 0.142 keeps an included
    router's routes without their prefix, so ``scope['route'].path`` is only the tail."""
    tail = getattr(request.scope.get("route"), "path", None)
    if not tail:
        return path
    if not _TEMPLATES:
        from starlette.routing import compile_path
        for full in request.app.openapi()["paths"]:
            _TEMPLATES.append((full, compile_path(full)[0]))
    for full, regex in _TEMPLATES:
        if full.endswith(tail) and regex.fullmatch(path):
            return full
    return path


async def gate(request: Request) -> None:
    """Refuse what an API token may not do, then a write the repository's mode does not allow.
    Runs for every route (app dependency)."""
    tok = tokens.current()
    if request.method not in _WRITES and tok is None:
        return
    path = request.scope.get("path") or ""   # every gated route is a fixed path
    gated = (request.method, path) in writemode.ROUTES
    if not gated and tok is None:
        return
    body: dict = {}
    raw = await request.body() if request.method in _WRITES else b""  # already read and cached
    if raw:
        try:
            parsed = json.loads(raw)
            body = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            body = {}
    query = dict(request.query_params)
    if tok is not None:
        refused = tokens.check(tok, request.method, _template(request, path), path, body, query)
        if refused:
            raise HTTPException(status_code=403, detail=refused[1], headers={"X-KopiaLens-Needs": refused[0]})
    if not gated:
        return
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
