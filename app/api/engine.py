"""Backups run by KopiaLens (core/engine.py) and the Tasks tab.

/engine/*: who runs each repository's backups, pause and resume, Snapshot now and Cancel through
KopiaLens's kopia server, alerts, and the connection-name fix for BlinkDisk vaults.
/tasks*: one list of every task, from KopiaLens's jobs and from its kopia servers.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config

from ..core import audit, coexist, engine, jobs

router = APIRouter(tags=["engine"])


def _kopia_repos() -> list[str]:
    return [r for r in config.REPOS if config.repo_kind(r) == "kopia" and not config.is_demo(r)]


def _need_repo(repo: str) -> None:
    if repo not in _kopia_repos():
        raise HTTPException(status_code=404, detail=f"unknown repository: {repo}")


@router.get("/engine/status")
def status() -> dict:
    return {"pause": engine.pause_state(None),
            "repos": [engine.repo_status(r) for r in _kopia_repos()],
            "running_tasks": sum(1 for t in engine.live_tasks() if t["status"] == "running"),
            "alert_seq": max((a["id"] for a in engine.alerts()), default=0)}


@router.get("/engine/repo")
def repo_detail(repo: str) -> dict:
    _need_repo(repo)
    return {"status": engine.repo_status(repo), "view": coexist.repo_view(repo, with_owner=True),
            "health": engine.health(repo), "hints": engine.hints(repo),
            "identity": coexist.identity_check(repo), "sources": engine.source_status(repo)}


@router.get("/engine/sources")
def sources(repo: str) -> dict:
    _need_repo(repo)
    return {"running": engine.running(repo) is not None, "sources": engine.source_status(repo)}


@router.get("/engine/alerts")
def alert_list(after: int = 0) -> dict:
    return {"alerts": engine.alerts(after)}


class ChoiceRequest(BaseModel):
    repo: str
    choice: str                 # kopialens | external
    how: str = "share"          # share | move
    password: str = ""


@router.post("/engine/choice")
def choice(payload: ChoiceRequest) -> dict:
    _need_repo(payload.repo)
    try:
        return engine.set_choice(payload.repo, payload.choice, payload.how, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class PauseRequest(BaseModel):
    repo: str | None = None
    hours: float = 0


@router.post("/engine/pause")
def pause(payload: PauseRequest) -> dict:
    if payload.repo is not None:
        _need_repo(payload.repo)
    return engine.set_pause(payload.repo, payload.hours)


class RepoRequest(BaseModel):
    repo: str | None = None


@router.post("/engine/resume")
def resume(payload: RepoRequest) -> dict:
    if payload.repo is not None:
        _need_repo(payload.repo)
    return engine.resume(payload.repo)


class SourceRequest(BaseModel):
    repo: str
    source: str


@router.post("/engine/snapshot")
def snapshot(payload: SourceRequest) -> dict:
    _need_repo(payload.repo)
    try:
        if not engine.trigger_snapshot(payload.repo, payload.source):
            raise HTTPException(status_code=409, detail="KopiaLens isn't running backups for this repository.")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't start the snapshot: {exc}") from exc
    audit.audit("executed", "snapshot-create", repo=payload.repo, summary={"source": payload.source, "via": "engine"})
    return {"ok": True}


@router.post("/engine/cancel")
def cancel(payload: SourceRequest) -> dict:
    _need_repo(payload.repo)
    try:
        ok = engine.cancel_snapshot(payload.repo, payload.source)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't cancel the snapshot: {exc}") from exc
    return {"ok": ok}


class IdentityRequest(BaseModel):
    repo: str
    identity: str               # the user@host the check suggested; must still be the suggestion


@router.post("/engine/identity")
def fix_identity(payload: IdentityRequest) -> dict:
    """Name this connection like the repository's snapshots (`kopia repository set-client`). Only
    the connection file changes; the maintenance owner is never moved (trap 55)."""
    _need_repo(payload.repo)
    check = coexist.identity_check(payload.repo)
    if not check or check["suggested"] != payload.identity:
        raise HTTPException(status_code=409, detail="Nothing to fix: the names match now.")
    from ..core import kopiabin, repoconnect
    cfg = coexist.config_path(payload.repo)
    res = repoconnect._run([kopiabin.exe(), "--config-file", cfg, "repository", "set-client",
                            f"--hostname={check['host']}", f"--username={check['user']}"], None, timeout=120.0)
    audit.audit("executed", "repo-set-client", repo=payload.repo,
                summary={"from": check["current"], "to": check["suggested"]}, rc=res["returncode"])
    if res["returncode"] != 0:
        raise HTTPException(status_code=502, detail=repoconnect._last_meaningful_line(res.get("stderr") or "")
                            or "kopia couldn't change the connection name.")
    from .policies import _CLIENT_CACHE
    _CLIENT_CACHE.pop(payload.repo, None)
    return {"ok": True, "identity": check["suggested"]}


# ---- Tasks tab --------------------------------------------------------------------------------

_JOB_KIND = {"snapshot_create": "Snapshot", "maintenance": "Maintenance", "restore": "Restore",
             "snapshot_delete": "Delete snapshots", "remove_files": "Remove files",
             "prune_versions": "Prune versions", "policy_write": "Policies", "snapshot_pin": "Pins",
             "encryption_level": "Encryption", "blob_stats": "Blob stats"}
_JOB_STATUS = {"running": "running", "done": "success", "failed": "failed", "stopped": "canceled"}


def _job_task(j: dict) -> dict:
    state = j.get("state") or ""
    status = _JOB_STATUS.get(state, state)
    if status == "success" and j.get("errors"):
        status = "warning"
    return {
        "id": f"job:{j.get('job_id')}", "repo": str(j.get("repo") or ""), "origin": "kopialens",
        "kind": _JOB_KIND.get(j.get("kind") or "", j.get("kind_label") or ""),
        "description": (j.get("label") or "") + (" (dry run)" if j.get("dry") else ""),
        "source": "", "status": status, "start": j.get("started") or j.get("created"),
        "end": j.get("finished"), "error": j.get("error") or "", "progress": "",
        "counters": {}, "done": j.get("done"), "total": j.get("total"),
    }


@router.get("/tasks")
def tasks(repo: str = "all", limit: int = 1000) -> dict:
    by_id: dict[str, dict] = {}
    for t in engine.history(limit=limit * 2):
        by_id[t["id"]] = t
    for t in engine.live_tasks():
        by_id[t["id"]] = {**by_id.get(t["id"], {}), **t}
    for j in jobs.read_job_history(limit=limit):
        by_id.setdefault(f"job:{j.get('job_id')}", _job_task(j))
    for j in jobs.list_jobs():
        by_id[f"job:{j.get('job_id')}"] = _job_task(j)
    rows = [t for t in by_id.values() if repo == "all" or str(t.get("repo")) == str(repo)]
    rows.sort(key=lambda t: t.get("start") or 0, reverse=True)
    return {"tasks": rows[:limit], "running": sum(1 for t in rows if t.get("status") == "running"),
            "history_days": engine.history_days()}


def _split(task_id: str) -> tuple[str, str, str, str]:
    """``engine:<repo>:<server run>.<id>`` or ``job:<id>`` -> (origin, repo, server run, id)."""
    parts = task_id.split(":", 2)
    if parts[0] == "engine" and len(parts) == 3 and "." in parts[2]:
        tag, _, tid = parts[2].partition(".")
        return "engine", parts[1], tag, tid
    if parts[0] == "job" and len(parts) == 2:
        return "job", "", "", parts[1]
    raise HTTPException(status_code=404, detail=f"unknown task: {task_id}")


def _live(repo: str, tag: str, tid: str):
    """The running server's task, when the id belongs to the server run that is still going."""
    srv = engine.running(repo)
    return srv.tasks.get(tid) if srv and srv.tag == tag else None


@router.get("/tasks/detail")
def task_detail(id: str, after: int = 0) -> dict:
    origin, repo, tag, tid = _split(id)
    if origin == "job":
        s = jobs.job_status(tid, after) or jobs.history_status(tid, after)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown task")
        out = _job_task(s)
        out.update(lines=s.get("lines") or [], cursor=s.get("cursor") or 0, job_id=tid, commands=s.get("commands_text") or "",
                   log_path=s.get("log_path") or "")
        return out
    live = _live(repo, tag, tid)
    rec = engine.task_record(repo, live, tag) if live else next(
        (h for h in engine.history() if h.get("id") == id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown task")
    if live:
        lines = engine.task_logs(repo, tid)
    else:
        from ..core import filecrypto
        path = rec.get("log_path") or ""
        lines = filecrypto.read_lines(path) if path and os.path.isfile(path) else []
    return {**rec, "lines": lines[max(0, int(after)):], "cursor": len(lines)}


class TaskCancel(BaseModel):
    id: str


@router.post("/tasks/cancel")
def task_cancel(payload: TaskCancel) -> dict:
    origin, repo, tag, tid = _split(payload.id)
    try:
        if origin == "job":
            return jobs.stop_job(tid)
        if _live(repo, tag, tid) is None:
            return {"ok": False}
        return {"ok": engine.cancel_task(repo, tid)}
    except jobs.JobRefused as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't stop the task: {exc}") from exc

