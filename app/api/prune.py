"""Prune versions of a file or folder: plan (rows + what each frees on its own), price a set of
ticks, and the repo-wide list of versions that free the most. Execution is the
``prune_versions`` job. The numbers are free; the dry run and the removal are Pro."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..index import prune
from .errors import PASS_THROUGH

router = APIRouter(tags=["prune"])


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
        return prune.plan(body.repo, body.source or None, body.full_path)
    except PASS_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/prune/price")
def prune_price(body: PriceRequest) -> dict:
    out = prune.price(body.token, body.remove)
    if out is None:
        raise HTTPException(status_code=410, detail="This plan is out of date. Open Prune versions again.")
    return out


@router.get("/prune/outliers")
def prune_outliers(repo: str, limit: int = 100) -> dict:
    try:
        return prune.outliers(repo, max(1, min(limit, 500)))
    except PASS_THROUGH:
        raise
