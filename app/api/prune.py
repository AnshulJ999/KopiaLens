"""Prune versions of a file or folder: plan (rows + what each frees on its own), price a set of
ticks, and the repo-wide list of versions that free the most. Execution is the
``prune_versions`` job. The numbers are free; the dry run and the removal are Pro."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..index import prune
from ..licensing import features
from .errors import PASS_THROUGH

router = APIRouter(tags=["prune"])


def _free(repo: str | None) -> bool:
    # licence check: do not remove or bypass (KopiaLens License)
    return not features.allowed("prune", repo)


def _strip_plan(out: dict) -> dict:
    """Free: sizes and dates, without the object and snapshot ids a remove command needs."""
    for r in out.get("rows") or []:
        r["obj"] = ""
        for s in r.get("snapshots") or []:
            s["id"] = ""
    return out


class PlanRequest(BaseModel):
    repo: str
    full_path: str
    source: str | None = None


class PriceRequest(BaseModel):
    token: str
    remove: list[int] = []


@router.post("/prune/plan")
def prune_plan(body: PlanRequest) -> dict:
    if not body.full_path:
        raise HTTPException(status_code=400, detail="full_path is required")
    try:
        out = prune.plan(body.repo, body.source or None, body.full_path)
        return _strip_plan(out) if _free(body.repo) else out
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/prune/price")
def prune_price(body: PriceRequest) -> dict:
    out = prune.price(body.token, body.remove)
    if out is None:
        raise HTTPException(status_code=410, detail="This plan is out of date. Open Prune versions again.")
    if _free(prune.plan_repo(body.token)):
        # Same length, so the Pro buttons still show; no ids leave the server.
        out["commands"] = [{"obj": "", "snapshot_ids": []} for _ in out.get("commands") or []]
    return out


@router.get("/prune/outliers")
def prune_outliers(repo: str, limit: int = 100) -> dict:
    try:
        out = prune.outliers(repo, max(1, min(limit, 500)))
        if _free(repo):
            for r in out.get("rows") or []:
                r["obj"] = ""
        return out
    except PASS_THROUGH:
        raise
