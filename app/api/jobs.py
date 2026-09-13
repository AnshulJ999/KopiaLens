"""Job runner routes: start, follow (cursor-poll), stop, list.

The header chip reads /jobs/list; the job panel polls /jobs/status with a line cursor so
each poll only carries what is new. Destructive UI actions start their jobs HERE: the
remove kinds accept the same request bodies the legacy endpoints take (``form`` selects
"plan" = the chunked filter plan, "files" = the detail/batch file list) and resolution
reuses the legacy helpers verbatim, so a job deletes exactly the set the old synchronous
path would have. Legacy /actions/* endpoints stay - only the UI's call sites moved.
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
    params: dict = {}               # kind-specific; resolved below into core.jobs' shapes
    label: str | None = None        # overrides the default one-line description


class JobStopRequest(BaseModel):
    job_id: str


def _resolve_params(kind: str, repo: str, params: dict) -> dict:
    """Turn a caller's request-shaped params into the runner's resolved items/flag/source.
    Resolution mirrors the legacy endpoints exactly (same models + helpers), and runs in the
    request thread exactly where /actions/remove-execute resolves today - no new semantics."""
    from .actions import (RemoveFilesRequest, RemovePlanRequest, _remove_items,
                          _remove_scope, _remove_targets, _rf_resolve)
    if kind == "remove_files":
        dry = bool(params.get("dry"))
        form = params.get("form")
        if form == "plan":
            known = {k: v for k, v in params.items() if k not in ("form", "dry")}
            rp = RemovePlanRequest(**{**known, "repo": repo})
            t = _remove_targets(rp)
            items, flag = _remove_items(t, rp.by)
            if not items:
                raise jobs.JobRefused("no matching files")
            return {"items": items, "flag": flag, "dry": dry,
                    "source": _remove_scope(rp.everywhere, t["sources"])}
        known = {k: v for k, v in params.items() if k not in ("form", "dry")}
        rf = RemoveFilesRequest(**{**known, "repo": repo})
        oids, names, scope = _rf_resolve(rf)
        if rf.by == "filename":
            if not names:
                raise jobs.JobRefused("no matching files")
            return {"items": names, "flag": "--filename", "source": scope, "dry": dry}
        if not oids:
            raise jobs.JobRefused("no content objects found")
        return {"items": oids, "flag": "--object-id", "source": scope, "dry": dry}
    if kind == "snapshot_delete":
        ids = params.get("snapshot_ids") or []
        if not ids:
            raise jobs.JobRefused("snapshot_ids is empty")
        return {"snapshot_ids": ids, "dry": bool(params.get("dry"))}
    if kind == "restore":
        if not params.get("source_id") or not params.get("target"):
            raise jobs.JobRefused("restore needs source_id and target")
        return params
    return params  # maintenance and future kinds: the runner validates its own params


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
    if payload.kind not in jobs.KINDS:
        raise HTTPException(status_code=409, detail=f"unknown job kind: {payload.kind}")
    try:
        params = _resolve_params(payload.kind, payload.repo, payload.params)
        job = jobs.start_job(payload.kind, payload.repo, params, payload.label,
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
    """In-memory registry (running + the last 50 finished) merged with the on-disk history
    () so Activity survives a restart. Memory wins per job_id; newest first."""
    jobs_by_id = {j["job_id"]: j for j in jobs.list_jobs()}
    for rec in jobs.read_job_history(limit=300):
        jobs_by_id.setdefault(rec.get("job_id"), rec)
    merged = sorted(jobs_by_id.values(), key=lambda j: j.get("created") or 0, reverse=True)
    return {"jobs": merged[:300]}
