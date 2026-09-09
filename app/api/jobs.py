"""Job runner routes: start, follow (cursor-poll), stop, list.

The header chip reads /jobs/list; the job panel polls /jobs/status with a line cursor so
each poll only carries what is new. Legacy /actions/remove-execute + _REMOVE_JOBS stay
untouched - the old remove panel keeps working; new callers point here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import jobs
from ..core.sources import invalidate as invalidate_sources

router = APIRouter(tags=["jobs"])


class JobStartRequest(BaseModel):
    kind: str                       # remove_files | snapshot_delete | maintenance | restore
    repo: str = "1"
    params: dict = {}               # kind-specific; see core.jobs._build_commands
    label: str | None = None        # overrides the default one-line description


class JobStopRequest(BaseModel):
    job_id: str


def _on_done(job: dict) -> None:
    """Mirror the legacy executors: a completed mutating remove/delete refreshes the source
    listing and triggers the incremental reindex so the UI reflects reality."""
    if job.get("dry") or not job.get("mutating"):
        return
    if job.get("kind") not in ("remove_files", "snapshot_delete"):
        return
    invalidate_sources(job["repo"])
    try:
        from .actions import _start_index_run  # same worker pass the Index button uses
        _start_index_run(job["repo"])
    except Exception:  # noqa: BLE001 - reindex is best-effort; the UI Refresh also recovers
        pass


@router.post("/jobs/start")
def start(payload: JobStartRequest) -> dict:
    try:
        job = jobs.start_job(payload.kind, payload.repo, payload.params, payload.label,
                             on_done=_on_done)
    except jobs.JobRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return jobs.job_status(job["job_id"], after=0)


@router.get("/jobs/status")
def status(job_id: str, after: int = 0) -> dict:
    s = jobs.job_status(job_id, after)
    if s is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return s


@router.post("/jobs/stop")
def stop(payload: JobStopRequest) -> dict:
    try:
        return jobs.stop_job(payload.job_id)
    except jobs.JobRefused as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/list")
def list_all() -> dict:
    return {"jobs": jobs.list_jobs()}
