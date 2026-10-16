"""Job runner routes: start, follow (cursor-poll), stop, list.

The header chip reads /jobs/list; the job panel polls /jobs/status with a line cursor so
each poll only carries what is new. Destructive UI actions start their jobs HERE: the
remove kinds accept the same request bodies the legacy endpoints take (``form`` selects
"plan" = the chunked filter plan, "files" = the detail/batch file list) and resolution
reuses the legacy helpers verbatim, so a job deletes exactly the set the old synchronous
path would have. Legacy /actions/* endpoints stay - only the UI's call sites moved.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import jobs, writemode
from ..core.sources import invalidate as invalidate_sources
from ..licensing import features
from . import freeids

router = APIRouter(tags=["jobs"])


class JobStartRequest(BaseModel):
    kind: str                       # remove_files | snapshot_delete | maintenance | restore
    repo: str
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
            from .actions import index_generation, previewed_plan

            known = {k: v for k, v in params.items() if k not in ("form", "dry")}
            token = str(known.pop("plan_token", "") or "")
            plan = previewed_plan(token)
            if plan is not None and plan.get("repo") != repo:
                plan = None          # a ticket from another repo: never run it here
            if plan is not None:
                # exactly the list the preview showed - never re-derived, so a reindex that
                # lands between preview and execute cannot add files nobody approved.
                out = {"items": plan["items"], "flag": plan["flag"], "dry": dry,
                       "source": plan["source"], "resolved": "preview"}
                moved = (plan.get("generation") is not None
                         and plan["generation"] != index_generation(repo))
                if moved:
                    out["index_changed"] = True
                    out["note"] = ("The index changed since your preview. Removing exactly the "
                                   "files you approved - nothing else.")
                return out
            # option (a): a MUTATING plan delete never re-derives silently - an expired or
            # absent ticket refuses, and the panel re-previews so the user approves the fresh list.
            # Dry runs mutate nothing, so they keep the old filter fallback.
            if not dry:
                raise jobs.JobRefused("That preview is out of date. The list has been refreshed, "
                                      "so review it and delete again.")
            rp = RemovePlanRequest(**{**known, "repo": repo})
            t = _remove_targets(rp)
            items, flag = _remove_items(t, rp.by)
            if not items:
                raise jobs.JobRefused("no matching files")
            return {"items": items, "flag": flag, "dry": dry,
                    "source": _remove_scope(rp.everywhere, t["sources"]),
                    "resolved": "filter"}
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
        items = params.get("items")
        if items is not None:
            if not items or not all(i.get("source_id") and i.get("target") for i in items):
                raise jobs.JobRefused("restore needs a source and a target for every item")
            for i in items:
                i["source_id"] = freeids.release(i["source_id"])
                if not i["source_id"]:
                    raise jobs.JobRefused("This restore preview has expired. Preview it again.")
        elif not params.get("source_id") or not params.get("target"):
            raise jobs.JobRefused("restore needs source_id and target")
        # kopia creates a missing folder target, but not the parent folder of a single file
        for it in items or [{"target": params["target"]}]:
            parent = os.path.dirname(it["target"].rstrip("\\/"))
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as exc:
                    raise jobs.JobRefused(f"cannot create {parent}: {exc}") from exc
        return params
    if kind == "maintenance":
        out = {"full": bool(params.get("full", True)),
               "safety": "none" if params.get("safety") == "none" else "full"}
        path = _local_storage_path(repo)
        size = jobs.folder_size(path) if path else None
        if size is not None:
            out.update(measure_path=path, size_before=size)
        return out
    if kind == "snapshot_create":
        source = params.get("source") or ""
        from ..core import policyresolve
        from ..core.paths import source_root
        from .policies import client_options
        user, host, _path = policyresolve.parse_key(source)
        client = client_options(repo)
        if not source or not client["host"]:
            raise jobs.JobRefused("this repository's client identity could not be read")
        if user.lower() != client["user"].lower() or host.lower() != client["host"].lower():
            raise jobs.JobRefused(f"{user}@{host} is another computer; snapshots run on the computer they back up")
        path = source_root(source)
        if not os.path.exists(path):
            raise jobs.JobRefused(f"{path} is not on this computer")
        return {"path": path, "description": (params.get("description") or "").strip()}
    if kind == "snapshot_estimate":
        path = (params.get("path") or "").strip()
        if not path or not os.path.exists(path):
            raise jobs.JobRefused(f"{path or 'That path'} is not on this computer")
        return {"path": path}
    if kind == "source_delete":
        from ..core.sources import sources_index
        source = params.get("source") or ""
        if source not in sources_index(repo):
            raise jobs.JobRefused("That source has no snapshots in this repository.")
        return {"source": source, "delete_policy": bool(params.get("delete_policy")),
                "dry": bool(params.get("dry"))}
    if kind == "move_history":
        from ..core import coexist
        check = coexist.identity_check(repo)
        if not check or not check["merge_from"] or params.get("src") != check["merge_from"] or params.get("dst") != check["suggested"]:
            raise jobs.JobRefused("Nothing to merge: the snapshot names match now.")
        return {"src": check["merge_from"], "dst": check["suggested"], "dry": bool(params.get("dry"))}
    if kind == "snapshot_verify":
        sources = [s for s in (params.get("sources") or []) if s]
        try:
            percent = float(params.get("percent") or 0)
        except (TypeError, ValueError):
            raise jobs.JobRefused("percent must be a number") from None
        if not 0 <= percent <= 100:
            raise jobs.JobRefused("percent must be between 0 and 100")
        if any(j["kind"] == "snapshot_verify" and j["repo"] == repo and j["state"] == "running"
               for j in jobs.list_jobs()):
            raise jobs.JobRefused("a verify is already running for this repository")
        return {"sources": sources, "percent": percent}
    if kind == "blob_stats":
        if any(j["kind"] == "blob_stats" and j["repo"] == repo and j["state"] == "running"
               for j in jobs.list_jobs()):
            raise jobs.JobRefused("blob stats is already running for this repository")
        return {}
    if kind == "snapshot_pin":
        ids = [i for i in (params.get("snapshot_ids") or []) if i]
        if not ids:
            raise jobs.JobRefused("no snapshots selected")
        name = (params.get("pin_name") or "do-not-delete").strip()
        old = (params.get("rename_from") or "").strip()
        if not name or len(name) > 100 or any(c in name + old for c in (chr(10), chr(13), ",")):
            raise jobs.JobRefused("Pin names can't be empty or contain commas.")
        return {"snapshot_ids": ids, "remove": bool(params.get("remove")), "pin_name": name,
                "rename_from": old if old != name else ""}
    if kind == "policy_write":
        writes = [w for w in (params.get("writes") or []) if w.get("target")]
        if not writes:
            raise jobs.JobRefused("no changes to save")
        then = params.get("then")
        out = {"steps": [_write_step(repo, w) for w in writes], "then": None}
        if then:
            if then.get("kind") not in ("remove_files", "snapshot_delete"):
                raise jobs.JobRefused("unknown delete step")
            out["then"] = {"kind": then["kind"],
                           "params": _resolve_params(then["kind"], repo, then.get("params") or {})}
        return out
    return params  # maintenance and future kinds: the runner validates its own params


def _write_step(repo: str, w: dict):
    """One policy write as a job step: backup, write, re-read, and the before/after lists."""
    def run(job: dict, lf) -> tuple[int, str]:
        from fastapi import HTTPException as _HTTPException

        from .policies import Changes, write_rules
        target = w["target"]
        label = "Global" if target == "global" else target
        try:
            r = write_rules(repo, target, Changes(add=w.get("add") or [], remove=w.get("remove") or [],
                                                  delete_policy=bool(w.get("delete_policy")),
                                                  fields=w.get("fields") or {}),
                            log=lambda text: jobs.say(job, lf, text))
        except _HTTPException as exc:
            jobs.say(job, lf, f"Not added to {label}: {exc.detail}")
            job["results"].append({"target": target, "ok": False, "error": str(exc.detail)})
            return 1, str(exc.detail)
        diff = r["diff"]
        job["results"].append({
            "target": target, "ok": r["ok"], "before": diff["current_rules"],
            "after": diff["resulting_rules"], "stored": r["stored_rules"],
            "fields": diff.get("field_changes") or [],
            "matches": r["stored_matches"], "backup": (r.get("backup") or {}).get("name", ""),
            "error": "" if r["ok"] else r.get("output_tail", "")[-300:],
        })
        if not r["ok"]:
            jobs.say(job, lf, f"kopia exited {r['returncode']}: {r.get('output_tail', '')[-300:]}")
            return int(r["returncode"] or 1), r.get("output_tail", "")[-280:]
        jobs.say(job, lf, f"Saved to {label}" if r["stored_matches"]
                 else f"Kopia stored a different list for {label} than expected")
        return 0, ""
    return run


def _local_storage_path(repo: str) -> str | None:
    """The repository folder, for a filesystem repo only."""
    from .stats import _json
    status = _json(repo, ["repository", "status", "--json"]) or {}
    storage = status.get("storage") or {}
    if storage.get("type") != "filesystem":
        return None
    return (storage.get("config") or {}).get("path") or None


def _on_done(job: dict) -> None:
    """A completed remove/delete refreshes the source listing and queues the incremental
    reindex so the UI reflects reality. A maintenance run refreshes the Stats figures. Any write
    tells KopiaLens's kopia server (when it runs this repository's backups) to re-read the repo."""
    if job.get("state") == "failed" and not job.get("dry"):
        from ..core import notify
        notify.send_later(f"{job.get('kind_label') or 'A KopiaLens job'} failed", str(job.get("error") or job.get("label") or "")[:300], "error")
    if (job.get("mutating") and not job.get("dry")) or job.get("kind") in ("snapshot_create", "snapshot_pin", "policy_write"):
        try:
            from ..core import engine
            engine.refresh(job["repo"])
        except Exception:  # noqa: BLE001 - the server refreshes on its own interval anyway
            pass
    if job.get("kind") == "maintenance" and not job.get("dry"):
        try:
            from .stats import refresh_stats
            refresh_stats(job["repo"])
        except Exception:  # noqa: BLE001
            pass
        return
    if job.get("kind") == "blob_stats":
        if job.get("state") == "done":
            from .stats import set_blob_stats
            lines = jobs.job_lines(job)
            set_blob_stats(job["repo"], "\n".join(lines) or "(empty)")
        return
    if job.get("kind") in ("snapshot_create", "snapshot_pin") and job.get("state") == "done":
        invalidate_sources(job["repo"])  # the snapshot list reads kopia's own listing
        return
    if job.get("dry") or not job.get("mutating") or job.get("held"):
        return
    kind = job.get("kind")
    if kind == "policy_write":
        kind = (job.get("detail") or {}).get("then")
    if kind not in ("remove_files", "snapshot_delete", "prune_versions", "source_delete", "move_history"):
        return
    invalidate_sources(job["repo"])
    try:
        from ..core import reindexq
        reindexq.request(job["repo"])
    except Exception:  # noqa: BLE001 - reindex is best-effort; the UI Refresh also recovers
        pass


def _require_pro(kind: str, repo: str, params: dict) -> None:
    """The Pro job kinds (features.PRO). Dry runs too: their output lists the plan."""
    if kind == "remove_files":
        features.require("remove", repo)  # licence check: do not remove or bypass (KopiaLens License)
    elif kind == "prune_versions":
        features.require("prune", repo)  # licence check: do not remove or bypass (KopiaLens License)
    elif kind == "policy_write":
        if params.get("then"):
            features.require("exclude_delete", repo)  # licence check: do not remove or bypass (KopiaLens License)
        if params.get("shape") in features.SMART_SHAPES:
            features.require("exclude_smart", repo)  # licence check: do not remove or bypass (KopiaLens License)


@router.post("/jobs/start")
def start(payload: JobStartRequest) -> dict:
    if payload.kind not in jobs.KINDS:
        raise HTTPException(status_code=409, detail=f"unknown job kind: {payload.kind}")
    if (payload.params or {}).get("dry") and payload.kind not in writemode.DRY_KINDS:
        raise HTTPException(status_code=409, detail=f"{payload.kind} has no dry run.")
    _require_pro(payload.kind, payload.repo, payload.params)
    try:
        params = _resolve_params(payload.kind, payload.repo, payload.params)
        job = jobs.start_job(payload.kind, payload.repo, params, payload.label,
                             on_done=_on_done)
    except jobs.JobRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return jobs.job_status(job["job_id"], after=0)


@router.get("/jobs/status")
def status(job_id: str, after: int = 0) -> dict:
    s = jobs.job_status(job_id, after) or jobs.history_status(job_id, after)
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
