"""Churn analysis + folder detail endpoints.

Two features that both answer "what is filling up my backup, and is it safe to remove":

  /api/churn         - what added new data in a window, by source / folder / file type
  /api/folder/*      - everything about one folder: sizes, which sources back it up (with their
                       schedule + retention), what is biggest inside it, and what deleting frees

The folder routes are split into a cheap part and an expensive part on purpose. ``/folder/summary``
is indexed lookups only, so the panel renders instantly; ``/folder/detail`` does the chunk walk
that produces freeable/shared and is fetched afterwards - the same lazy pattern the file detail
panel already uses.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

from config import repo_kind

from ..core.policy import effective_for_path, source_summaries, summarize
from ..index.churnview import churn_rollup
from ..index.physical import folder_detail
from ..index.queries import folder_sources, folder_summary, folder_top_children

router = APIRouter(tags=["insights"])


def _require_kopia(repo: str) -> None:
    if repo_kind(repo) != "kopia":
        raise HTTPException(status_code=400, detail=f"repo {repo!r} is not a kopia repo")


@router.get("/churn")
def churn(repo: str = "1", days: int = 30, depth: int = 4, limit: int = 400,
          refresh: bool = False) -> dict:
    """New backed-up data in the last ``days``, rolled up three ways from one computation."""
    _require_kopia(repo)
    try:
        return churn_rollup(repo, days=days, depth=depth, limit=limit, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/folder/summary")
def folder_summary_route(repo: str = "1", prefix: str = "", source: str | None = None) -> dict:
    """Instant folder facts + which sources back it up, each with its schedule and retention.
    Policy summaries come from the nightly-refreshed cache, so this stays a fast call."""
    _require_kopia(repo)
    if not prefix:
        raise HTTPException(status_code=400, detail="prefix is required")
    try:
        summary = folder_summary(repo, prefix, source=source)
        srcs = folder_sources(repo, prefix)
        try:
            pol = source_summaries(repo)
        except Exception:  # noqa: BLE001 - a policy read must never fail the whole panel
            pol = {}
        for row in srcs:
            row["policy"] = pol.get(row["source"])
        summary["sources"] = srcs
        summary["children"] = folder_top_children(repo, prefix, source=source)
        summary["multi_source"] = len(srcs) > 1
        return summary
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/folder/policy")
def folder_policy(repo: str = "1", source: str = "", prefix: str = "") -> dict:
    """The EFFECTIVE kopia policy for this exact folder - kopia resolves inheritance, so a folder
    with its own rules (or its own ignore list) reports them rather than the source's. One
    subprocess, cached; fetched lazily because it is the only part of the panel that shells out."""
    _require_kopia(repo)
    if not source or not prefix:
        raise HTTPException(status_code=400, detail="source and prefix are required")
    try:
        raw = effective_for_path(repo, source, prefix)
        return {"prefix": prefix, "source": source, "policy": summarize(raw), "found": bool(raw)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class FolderDetailRequest(BaseModel):
    repo: str = "1"
    prefix: str
    source: str | None = None  # None = every source covering the folder (the honest cross-source answer)


@router.post("/folder/detail")
def folder_detail_route(payload: FolderDetailRequest) -> dict:
    """The expensive half: deduped physical, freeable, and shared for the whole subtree."""
    _require_kopia(payload.repo)
    if not payload.prefix:
        raise HTTPException(status_code=400, detail="prefix is required")
    try:
        return folder_detail(payload.repo, payload.source or None, payload.prefix)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
