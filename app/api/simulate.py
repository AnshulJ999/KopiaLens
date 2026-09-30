"""Retention simulation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.retention import simulate, simulate_all, validate_retention
from ..models import SimRequest
from .errors import PASS_THROUGH

router = APIRouter(tags=["simulate"])


@router.post("/simulate")
def post_simulate(payload: SimRequest) -> dict:
    """Single-source simulate when ``source`` is given; otherwise the combined all-sources pass."""
    try:
        if payload.source:
            return simulate(payload.repo, payload.source, payload.policy.model_dump(), mode=payload.mode)
        return simulate_all(payload.repo, payload.policy.model_dump(), mode=payload.mode, refresh=payload.refresh)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/simulate/all")
def post_simulate_all(payload: SimRequest) -> dict:
    try:
        return simulate_all(payload.repo, payload.policy.model_dump(), mode=payload.mode, refresh=payload.refresh)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/validate")
def validate(repo: str, source: str | None = None) -> dict:
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    try:
        return validate_retention(repo, source)
    except PASS_THROUGH:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
