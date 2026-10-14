"""Review notes in testing builds: the Notes widget ships only in a build made with
``release.py --with-notes`` (or the Docker build arg), as ``devnotes/`` beside the server. Every
note goes to another KopiaLens that keeps them all in one place, so notes from every test copy
land in one list.

Where notes go: ``<data>/devnotes.json`` ``{"send_to": "http://<host>:7123", "token": "kl_...",
"name": "installed"}``, or the variables KOPIALENS_NOTES_SEND_TO / _TOKEN / _NAME. The token is an
API token of the receiving KopiaLens. Without a destination the widget stays off. A source run can
point KOPIALENS_DEVNOTES_BUNDLE at a staged folder (packaging/notes_bundle.py).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .applog import get_logger

log = get_logger("devnotes")

FILES = ("notes-starter.js", "notes-widget.js", "notes-widget.css", "kopialens-notes.js")
_MEDIA = {".js": "application/javascript", ".css": "text/css"}


def bundle_dir(root: Path) -> Path | None:
    folder = Path(os.environ.get("KOPIALENS_DEVNOTES_BUNDLE") or root / "devnotes")
    return folder if (folder / "BUNDLE.txt").is_file() else None


def _settings_file() -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, "devnotes.json")


def _read() -> dict:
    try:
        with open(_settings_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def destination() -> dict:
    saved = _read()
    send_to = os.environ.get("KOPIALENS_NOTES_SEND_TO") or saved.get("send_to") or ""
    return {"send_to": send_to.rstrip("/"),
            "token": os.environ.get("KOPIALENS_NOTES_TOKEN") or saved.get("token") or "",
            "name": os.environ.get("KOPIALENS_NOTES_NAME") or saved.get("name") or "",
            "enabled": bool(send_to) and saved.get("enabled", True) is not False}


def _set_enabled(on: bool) -> None:
    data = _read()
    data["enabled"] = bool(on)
    path = _settings_file()
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(path + ".tmp", path)


def load(app, root: Path, add_html) -> None:
    """Serve the widget and pass notes on, when this build carries the bundle."""
    folder = bundle_dir(root)
    if folder is None:
        return
    from config import APP_VERSION, PORT

    @app.get("/devnotes/{name}", include_in_schema=False)
    def devnotes_file(name: str):
        if name not in FILES:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(folder / name, media_type=_MEDIA[Path(name).suffix],
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/devnotes/state", include_in_schema=False)
    def get_state():
        return {"enabled": destination()["enabled"]}

    @app.post("/api/devnotes/state", include_in_schema=False)
    async def set_state(request: Request):
        body = await request.json()
        if not destination()["send_to"]:
            return JSONResponse({"error": "Notes have nowhere to go yet: set send_to in devnotes.json."},
                                status_code=409)
        _set_enabled(bool((body or {}).get("enabled")))
        return {"enabled": destination()["enabled"]}

    @app.api_route("/api/devnotes/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                   include_in_schema=False)
    async def forward(path: str, request: Request):
        dest = destination()
        if not dest["send_to"]:
            return JSONResponse({"error": "Notes have nowhere to go yet: set send_to in devnotes.json."},
                                status_code=503)
        headers = {"Authorization": f"Bearer {dest['token']}"} if dest["token"] else {}
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        url = f"{dest['send_to']}/api/devnotes/{path}"
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=30, verify=False) as client:
                res = await client.request(request.method, url, params=request.query_params,
                                           content=await request.body(), headers=headers)
        except httpx.HTTPError as exc:
            log.warning("notes: %s %s failed: %s", request.method, url, exc)
            return JSONResponse({"error": f"Couldn't reach {dest['send_to']}: {exc}"}, status_code=502)
        log.debug("notes: %s /api/devnotes/%s -> %s in %.2fs", request.method, path, res.status_code,
                  time.time() - started)
        return Response(res.content, status_code=res.status_code,
                        media_type=res.headers.get("content-type"))

    stamp = int(max((folder / f).stat().st_mtime for f in FILES))
    name = destination()["name"] or f"copy-{PORT}"
    cfg = json.dumps({"instance": name, "build": APP_VERSION})
    starter = json.dumps({"state": "/api/devnotes/state", "v": stamp,
                          "files": ["/devnotes/notes-widget.css", "/devnotes/notes-widget.js",
                                    "/devnotes/kopialens-notes.js"]})
    add_html(f"<script>window.__KL_DEVNOTES = {cfg}; window.NotesStarterConfig = {starter};</script>"
             f'<script src="/devnotes/notes-starter.js?v={stamp}"></script>')
    log.info("notes widget bundled (testing build); notes go to %s", destination()["send_to"] or "nowhere yet")
