"""Retention simulation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.retention import simulate, validate_retention
from ..models import SimRequest

router = APIRouter(tags=["simulate"])


@router.post("/simulate")
def post_simulate(payload: SimRequest) -> dict:
    try:
        return simulate(payload.repo, payload.source, payload.policy.model_dump(), mode=payload.mode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/validate")
def validate(repo: str = "1", source: str | None = None) -> dict:
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    try:
        return validate_retention(repo, source)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
