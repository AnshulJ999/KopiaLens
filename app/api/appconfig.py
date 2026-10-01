"""App-level routes: About, restart through the tray, pausing indexing, and configuration backups
(copies, restore, export/import)."""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

import config

from ..core import audit, configbackup, pause

router = APIRouter(tags=["app"])


# ---------------------------------------------------------------------------- about + restart

@router.get("/app/about")
def about() -> dict:
    from ..core import auth, dataupgrade, kopiabin
    return {
        "version": config.APP_VERSION,
        "profile": auth.profile(),
        "data_dir": config.DATA_DIR,
        "config_dir": config.CONFIG_DIR,
        "data_format": dataupgrade.status().get("version"),
        "kopia": kopiabin.info(with_version=False),
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
        "compiled": config.COMPILED,
        "platform": sys.platform,
    }


def _tray(path: str, method: str = "GET", timeout: float = 2.0) -> dict | None:
    url = f"http://{config.TRAY_CONTROL_HOST}:{config.TRAY_CONTROL_PORT}{path}"
    try:
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


@router.post("/app/restart")
def restart() -> dict:
    """Ask the tray to restart KopiaLens. The tray answers only once the new server is up, so the
    call is made after this reply has gone out."""
    if _tray("/status") is None:
        return {"ok": False, "reason": "no-tray"}

    def _later() -> None:
        time.sleep(0.5)
        _tray("/restart", "POST", timeout=90)

    threading.Thread(target=_later, name="restart-request", daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------------------- pause indexing

class PauseRequest(BaseModel):
    hours: int = 0


@router.get("/index/pause")
def get_pause() -> dict:
    return pause.state()


@router.post("/index/pause")
def post_pause(body: PauseRequest) -> dict:
    try:
        return pause.pause(body.hours or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/index/resume")
def post_resume() -> dict:
    return pause.resume()


# ---------------------------------------------------------------------------- config backups

class GroupsRequest(BaseModel):
    groups: list[str] = list(configbackup.DEFAULT_RESTORE)


class ImportApply(BaseModel):
    token: str
    groups: list[str] = list(configbackup.DEFAULT_RESTORE)


def _groups(groups: list[str]) -> list[str]:
    bad = [g for g in groups if g not in configbackup.GROUPS]
    if bad or not groups:
        raise HTTPException(status_code=400, detail="choose what to restore")
    return groups


@router.get("/config-backups")
def list_backups() -> dict:
    return {"backups": configbackup.list_backups(), "notices": configbackup.notices(),
            "keep": configbackup.KEEP, "groups": list(configbackup.GROUPS)}


@router.post("/config-backups")
def back_up_now() -> dict:
    made = configbackup.create("manual")
    if made is None:
        raise HTTPException(status_code=500, detail="The backup could not be written; see the app log.")
    return made


@router.post("/config-backups/{backup_id}/restore")
def restore(backup_id: str, body: GroupsRequest) -> dict:
    try:
        out = configbackup.restore(backup_id, _groups(body.groups))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.audit("config", "restore", summary={"backup": backup_id, "files": out["restored"]})
    return out


@router.get("/config-backups/export")
def export(groups: str = "settings,repos") -> Response:
    chosen = _groups([g for g in groups.split(",") if g])
    data = configbackup.export_zip(chosen)
    name = f"kopialens-settings-{time.strftime('%Y-%m-%d')}.zip"
    audit.audit("config", "export", summary={"groups": chosen})
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/config-backups/import")
async def import_preview(request: Request) -> dict:
    data = await request.body()
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="That file is too large to be a settings export.")
    try:
        return configbackup.import_preview(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/config-backups/import/apply")
def import_apply(body: ImportApply) -> dict:
    try:
        out = configbackup.import_apply(body.token, _groups(body.groups))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.audit("config", "import", summary={"files": out["restored"]})
    return out


@router.post("/config-backups/notices/{notice_id}/dismiss")
def dismiss(notice_id: str) -> dict:
    configbackup.dismiss(notice_id)
    return {"ok": True}
