"""The Notes widget (Settings > Experimental): a Notes button on every page for writing notes, with
a screenshot. Every build ships it as ``devnotes/`` beside the server (packaging/notes_bundle.py);
it runs only while "Experimental features" (Settings > Advanced) and "Notes" (Settings >
Experimental) are on, and nothing of it is served otherwise.

Notes are kept in ``<data>/notes`` by the widget's own store (Widgets/notes/notes_server). A copy can
send every note to another KopiaLens instead, so test copies share one list: ``<data>/devnotes.json``
``{"send_to": "http://<host>:7123", "token": "kl_...", "name": "installed"}`` or the variables
KOPIALENS_NOTES_SEND_TO / _TOKEN / _NAME, read at start. The token is an API token of the receiving
KopiaLens. A source run points KOPIALENS_DEVNOTES_BUNDLE at a staged ``web`` folder.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import basepath
from .applog import get_logger
from . import atomicfile

log = get_logger("devnotes")

FILES = ("notes-starter.js", "notes-widget.js", "notes-widget.css", "kopialens-notes.js")
_MEDIA = {".js": "application/javascript", ".css": "text/css"}
_loaded: dict = {}   # folder, stamp, name once load() found the bundle


def bundle_dir(root: Path) -> Path | None:
    folder = Path(os.environ.get("KOPIALENS_DEVNOTES_BUNDLE") or root / "devnotes")
    return folder if (folder / "BUNDLE.txt").is_file() else None


def active() -> bool:
    """Both switches on (Settings > Advanced, Settings > Experimental)."""
    from ..settings import load_settings
    kl = load_settings()["kopialens"]
    return kl.get("experimental") is True and kl.get("exp_notes") is True


def _gate() -> None:
    if not active():
        raise HTTPException(status_code=404, detail="Not found")


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
    atomicfile.replace(path + ".tmp", path)


def _notes_server():
    """The widget's store and router: compiled into a build, else from this repo's Widgets/notes."""
    try:
        from notes_server.fastapi_router import make_router
        from notes_server.notes_store import NotesStore
    except ImportError:
        from config import HERE
        sys.path.insert(0, str(Path(HERE).parents[1] / "Widgets" / "notes"))
        from notes_server.fastapi_router import make_router
        from notes_server.notes_store import NotesStore
    _real_clock(sys.modules[NotesStore.__module__])
    return make_router, NotesStore


def _real_clock(store_module) -> None:
    """A demo copy runs on a moved clock (config.DEMO_NOW); its notes keep the real time."""
    from config import DEMO_NOW
    if not DEMO_NOW:
        return
    try:
        import datetime as dt

        import time_machine
    except ImportError:
        return
    real = time_machine.escape_hatch.datetime.datetime
    store_module._now = lambda: real.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _LazyStore:
    """Makes the notes folder on first use, so a copy that never turns Notes on gets none."""

    def __init__(self, cls, folder: Path):
        self._cls, self._folder, self._store = cls, folder, None

    def __getattr__(self, name: str):
        if self._store is None:
            self._store = self._cls(self._folder, app="KopiaLens")
        return getattr(self._store, name)


def _add_forwarding(app, gate: list) -> None:
    """Send every note to another KopiaLens (devnotes.json / KOPIALENS_NOTES_*)."""

    @app.get("/api/devnotes/state", include_in_schema=False, dependencies=gate)
    def get_state():
        return {"enabled": destination()["enabled"]}

    @app.post("/api/devnotes/state", include_in_schema=False, dependencies=gate)
    async def set_state(request: Request):
        body = await request.json()
        _set_enabled(bool((body or {}).get("enabled")))
        return {"enabled": destination()["enabled"]}

    @app.api_route("/api/devnotes/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                   include_in_schema=False, dependencies=gate)
    async def forward(path: str, request: Request):
        dest = destination()
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


def load(app, root: Path) -> None:
    """Register the widget's routes (they answer only while Notes is on)."""
    folder = bundle_dir(root)
    if folder is None:
        return
    from config import DATA_DIR, PORT
    gate = [Depends(_gate)]

    @app.get("/devnotes/{name}", include_in_schema=False, dependencies=gate)
    def devnotes_file(name: str):
        if name not in FILES:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(folder / name, media_type=_MEDIA[Path(name).suffix],
                            headers={"Cache-Control": "no-cache"})

    dest = destination()
    if dest["send_to"]:
        _add_forwarding(app, gate)
        where = dest["send_to"]
    else:
        make_router, store_cls = _notes_server()
        app.include_router(make_router(_LazyStore(store_cls, Path(DATA_DIR) / "notes")), prefix="/api/devnotes",
                           dependencies=gate, include_in_schema=False)
        where = os.path.join(DATA_DIR, "notes")
    _loaded.update(folder=folder, stamp=int(max((folder / f).stat().st_mtime for f in FILES)),
                   name=dest["name"] or f"copy-{PORT}")
    log.info("Notes widget available (Settings > Experimental); notes go to %s", where)


def markup() -> str:
    """The page's script tags while Notes is on; the starter loads the widget itself."""
    if not _loaded or not active():
        return ""
    from config import APP_VERSION
    u, stamp = basepath.url, _loaded["stamp"]
    cfg = json.dumps({"instance": _loaded["name"], "build": APP_VERSION, "endpoint": u("/api/devnotes")})
    starter = json.dumps({"state": u("/api/devnotes/state"), "v": stamp,
                          "files": [u("/devnotes/notes-widget.css"), u("/devnotes/notes-widget.js"),
                                    u("/devnotes/kopialens-notes.js")]})
    script = u("/devnotes/notes-starter.js")
    return (f"<script>window.__KL_DEVNOTES = {cfg}; window.NotesStarterConfig = {starter};</script>"
            f'<script src="{script}?v={stamp}"></script>')
