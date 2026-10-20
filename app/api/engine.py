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

from ..core import approvals, audit, coexist, engine, jobs, pause as index_pause
from ..core.applog import get_logger

log = get_logger("api.engine")

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
            "alert_seq": max((a["id"] for a in engine.alerts()), default=0),
            "approvals_waiting": approvals.waiting_count()}


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


def _hours(h: float) -> float:
    try:
        return index_pause.check_hours(h)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/engine/pause")
def pause(payload: PauseRequest) -> dict:
    if payload.repo is not None:
        _need_repo(payload.repo)
    return engine.set_pause(payload.repo, _hours(payload.hours))


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


class SourcePause(BaseModel):
    repo: str
    source: str
    hours: float = 0


@router.post("/engine/source-pause")
def source_pause(payload: SourcePause) -> dict:
    _need_repo(payload.repo)
    if config.repo_record(payload.repo).get("engine") != "kopialens":
        raise HTTPException(status_code=409, detail="KopiaLens isn't running backups for this repository.")
    return engine.set_source_pause(payload.repo, payload.source, _hours(payload.hours))


@router.post("/engine/source-resume")
def source_resume(payload: SourcePause) -> dict:
    _need_repo(payload.repo)
    return engine.resume_source(payload.repo, payload.source)


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


class DescriptionRequest(BaseModel):
    repo: str
    snapshot_id: str
    description: str = ""
    password: str | None = None     # only when kopia's saved password wasn't found; never stored


@router.post("/engine/snapshot-description")
def snapshot_description(payload: DescriptionRequest) -> dict:
    from ..core import descedit
    _need_repo(payload.repo)
    if config.is_demo(payload.repo):
        raise HTTPException(status_code=409, detail="Not available in the demo.")
    try:
        engine.edit_description(payload.repo, payload.snapshot_id, payload.description.strip(), payload.password)
    except descedit.PasswordNeeded:
        return {"ok": False, "need_password": True}
    except Exception as exc:  # noqa: BLE001 - KopiaLens's own text; kopia's output may echo input
        log.warning("description edit failed repo=%s: %s", payload.repo, type(exc).__name__)
        msg = str(exc) if isinstance(exc, RuntimeError) else "kopia refused the change."
        raise HTTPException(status_code=502, detail=f"Couldn't change the description: {msg}") from exc
    from ..core.sources import invalidate
    invalidate(payload.repo)
    audit.audit("executed", "snapshot-description", repo=payload.repo, summary={"snapshot": payload.snapshot_id})
    return {"ok": True}


class IdentityRequest(BaseModel):
    repo: str
    identity: str               # the user@host the check suggested; must still be the suggestion


@router.post("/engine/identity")
def fix_identity(payload: IdentityRequest) -> dict:
    """Name this connection like the repository's snapshots (`kopia repository set-client`). Only
    the connection file changes; the maintenance owner is never moved (trap 55)."""
    _need_repo(payload.repo)
    check = coexist.identity_check(payload.repo)
    if not check or not check["connect_needed"] or check["suggested"] != payload.identity:
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
             "snapshot_estimate": "Estimate", "source_delete": "Delete source", "snapshot_verify": "Verify",
             "move_history": "Merge histories",
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
        "end": j.get("finished"), "error": j.get("error") or "", "progress": j.get("progress_text") or "",
        "counters": j.get("counters") or {}, "done": j.get("done"), "total": j.get("total"),
    }


_KOPIA_LOG_CHUNK = 2 * 1024 * 1024   # bytes of kopia's own log sent per request


def _kopia_log(log_path: str, after: int) -> tuple[list[str], int, bool]:
    """kopia's own log for a job, from byte offset ``after`` across its files: (lines, new offset,
    whether earlier lines were skipped). A first read of a big log starts at its last 2 MB."""
    files = jobs.kopia_log_files({"log_path": log_path}) if log_path else []
    sizes = [os.path.getsize(f) for f in files if os.path.isfile(f)]
    total = sum(sizes)
    skipped = False
    if after <= 0 and total > _KOPIA_LOG_CHUNK:
        after, skipped = total - _KOPIA_LOG_CHUNK, True
    out, pos, want = bytearray(), 0, _KOPIA_LOG_CHUNK
    for f, size in zip(files, sizes):
        if after < pos + size and want > 0:
            with open(f, "rb") as fh:
                fh.seek(max(0, after - pos))
                part = fh.read(want)
            out += part
            want -= len(part)
        pos += size
    text = out.decode("utf-8", errors="replace")
    end = text.rfind("\n")
    if end < 0:
        return [], after, skipped
    used = len(text[: end + 1].encode("utf-8", errors="replace"))
    lines = text[:end].split("\n")
    if skipped and lines:
        lines = lines[1:]          # the first line was cut in the middle
    return lines, after + used, skipped


@router.get("/tasks")
def tasks(repo: str = "all", limit: int = 1000) -> dict:
    by_id: dict[str, dict] = {}
    for t in engine.history(limit=limit * 2):
        by_id[t["id"]] = t
    for t in engine.live_tasks():
        by_id[t["id"]] = {**by_id.get(t["id"], {}), **t}
    # A job that followed a server task (Snapshot now) is listed as that task, not twice.
    for j in jobs.read_job_history(limit=limit):
        if not (j.get("detail") or {}).get("server_task"):
            by_id.setdefault(f"job:{j.get('job_id')}", _job_task(j))
    for j in jobs.list_jobs():
        if not j.get("server_task"):
            by_id[f"job:{j.get('job_id')}"] = _job_task(j)
    rows = [t for t in by_id.values() if repo == "all" or str(t.get("repo")) == str(repo)]
    rows.sort(key=lambda t: t.get("start") or 0, reverse=True)
    for t in rows:
        t["routine"] = engine.is_routine(t)
    return {"tasks": rows[:limit], "running": sum(1 for t in rows if t.get("status") == "running"),
            "history_days": engine.history_days(), "routine_seconds": engine.routine_seconds()}


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
def task_detail(id: str, after: int = 0, log: str = "") -> dict:
    """A task's details and log lines after cursor ``after``. ``log=kopia`` (KopiaLens's own
    command-line jobs): kopia's full log instead, where the cursor is a byte offset."""
    origin, repo, tag, tid = _split(id)
    if origin == "job":
        own_after = 10 ** 12 if log == "kopia" else after      # kopia's log asked: none of KopiaLens's lines
        s = jobs.job_status(tid, own_after) or jobs.history_status(tid, own_after)
        if s is None:
            raise HTTPException(status_code=404, detail="unknown task")
        out = _job_task(s)
        log_path = s.get("log_path") or ""
        out.update(lines=s.get("lines") or [], cursor=s.get("cursor") or 0, job_id=tid, commands=s.get("commands_text") or "",
                   log_path=log_path, kopia_log=bool(jobs.kopia_log_files({"log_path": log_path})) if log_path else False)
        if log == "kopia":
            lines, cursor, skipped = _kopia_log(log_path, after)
            out.update(lines=lines, cursor=cursor, skipped_earlier=skipped)
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

