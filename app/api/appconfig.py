"""App-level routes: About, restart through the tray, pausing indexing, and configuration backups
(copies, restore, export/import)."""

from __future__ import annotations

import os
import platform
import signal
import sys
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import config

from ..core import audit, configbackup, exportname, pause
from ..core.applog import get_logger

log = get_logger("app")
router = APIRouter(tags=["app"])


# ---------------------------------------------------------------------------- about + restart

def _notices_path() -> str | None:
    """THIRD-PARTY-NOTICES.txt, which release builds put beside the server program."""
    path = os.path.join(os.path.dirname(config.SELF_EXE), "THIRD-PARTY-NOTICES.txt")
    return path if config.COMPILED and os.path.isfile(path) else None


@router.get("/app/notices")
def notices() -> FileResponse:
    path = _notices_path()
    if not path:
        raise HTTPException(status_code=404, detail="This copy has no third-party notices file.")
    return FileResponse(path, filename="KopiaLens-third-party-notices.txt", media_type="text/plain; charset=utf-8")


@router.get("/app/about")
def about() -> dict:
    from ..core import auth, dataupgrade, kopiabin
    return {
        "notices": _notices_path() is not None,
        "version": config.APP_VERSION,
        "commit": config.APP_COMMIT,
        "profile": auth.profile(),
        "data_dir": config.DATA_DIR,
        "config_dir": config.CONFIG_DIR,
        "data_format": dataupgrade.status().get("version"),
        "kopia": kopiabin.info(),        # one quick --version read per process, then cached
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
        "compiled": config.COMPILED,
        "platform": sys.platform,
    }


_restart_requested = False


def restart_requested() -> bool:
    return _restart_requested


@router.get("/app/host")
def host_info(request: Request) -> dict:
    """What this machine can do for the person using it (open folders, picker, mounts)."""
    from ..core import host
    return host.info(request.client.host if request.client else None)


@router.post("/app/restart")
def restart() -> dict:
    """Restart KopiaLens. Under the desktop app the request is held for its status poll (every few
    seconds), which restarts this server; the control port is not needed. In Docker the server
    exits and the container's restart rule starts it again."""
    global _restart_requested
    from ..core import host
    kind = host.restart_kind()
    if kind == "desktop":
        _restart_requested = True
        log.info("restart requested: waiting for the desktop app to pick it up")
        return {"ok": True}
    if not kind:
        return {"ok": False, "reason": "no-tray"}

    def _exit() -> None:
        time.sleep(0.5)
        log.info("restart requested: exiting for the container to restart")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_exit, name="restart-request", daemon=True).start()
    return {"ok": True, "how": "container"}


class AutostartRequest(BaseModel):
    on: bool


@router.get("/app/autostart")
def autostart() -> dict:
    """Start at logon, as the desktop app last reported it (``available`` false elsewhere)."""
    from . import shell
    return shell.autostart_state()


@router.post("/app/autostart")
def set_autostart(body: AutostartRequest) -> dict:
    """Turn Start at logon on or off: the desktop app applies it on its next status poll."""
    from . import shell
    if not os.environ.get("KOPIALENS_SHELL_TOKEN"):
        raise HTTPException(status_code=409, detail="Only the desktop app starts at logon.")
    shell.request_autostart(body.on)
    return shell.autostart_state()


# ---------------------------------------------------------------------------- pause indexing

class PauseRequest(BaseModel):
    hours: float = 0


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
    keep: list[str] = []          # restore-preview item ids to leave as they are now


class ImportApply(BaseModel):
    token: str
    groups: list[str] = list(configbackup.DEFAULT_RESTORE)
    keep: list[str] = []


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


@router.get("/config-backups/{backup_id}/preview")
def restore_preview(backup_id: str, groups: str = "settings,repos,accounts,keys") -> dict:
    """What restoring this backup would change, item by item. Nothing is written."""
    try:
        return configbackup.restore_plan(backup_id, [g for g in groups.split(",") if g in configbackup.GROUPS])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/config-backups/{backup_id}/restore")
def restore(backup_id: str, body: GroupsRequest) -> dict:
    try:
        out = configbackup.restore(backup_id, _groups(body.groups), body.keep)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.audit("config", "restore", summary={"backup": backup_id, "files": out["restored"], "kept": body.keep})
    return out


@router.get("/config-backups/export")
def export(groups: str = "settings,repos") -> Response:
    chosen = _groups([g for g in groups.split(",") if g])
    data = configbackup.export_zip(chosen)
    name = exportname.export_name("config-backup", ext="zip")
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
        out = configbackup.import_apply(body.token, _groups(body.groups), body.keep)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.audit("config", "import", summary={"files": out["restored"], "kept": body.keep})
    return out


@router.post("/config-backups/notices/{notice_id}/dismiss")
def dismiss(notice_id: str) -> dict:
    configbackup.dismiss(notice_id)
    return {"ok": True}
