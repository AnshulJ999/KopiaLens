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

from config import REPOS, repo_kind

from ..core.policy import effective_for_path, source_summaries, summarize
from ..index.schema import RepoOffline
from ..index.churnview import cached_folder_window, churn_rollup
from ..index.foldercache import compute as folder_reclaim
from ..index.queries import (
    folder_contents,
    folder_file_types,
    folder_sources,
    folder_summary,
    folder_top_children,
)

_SEP = chr(92)


def _root_of(source_key: str) -> str:
    """The filesystem path a source is rooted at, lowercased, no trailing separator."""
    return source_key.split(":", 1)[-1].rstrip(_SEP).lower()


def classify_folder_sources(prefix: str, rows: list[dict], all_sources: list[str]) -> None:
    """Annotate each source row with how it actually relates to this folder.

    "24 sources hold paths under AppData" is true but misleading: only ONE of them (C:) backs up
    the folder itself - the other 23 are rooted INSIDE it, one per subfolder that has its own
    policy. Reporting a single count made a normal per-subfolder policy layout look like 24-way
    duplication. So each row says which it is:

      covers - the source root is at or above this folder, so it backs the whole folder up.
               More than one of these is the real "deleting from one leaves it in the other" case.
      inside - the source root is within this folder: a sub-policy covering part of it.

    ``overlap`` is the genuinely useful number: how many sources hold a file sitting at that
    source's root, counting the nested parents above it. On this repo that runs 2-4, not 24.
    """
    roots = {src: _root_of(src) for src in all_sources}
    base = prefix.rstrip(_SEP).lower()
    for row in rows:
        src = row["source"]
        root = roots.get(src, _root_of(src))
        row["relation"] = "covers" if (base == root or base.startswith(root + _SEP)) else "inside"
        row["overlap"] = 1 + sum(
            1 for other, oroot in roots.items()
            if other != src and oroot and root.startswith(oroot + _SEP)
        )


router = APIRouter(tags=["insights"])


def _require_kopia(repo: str) -> None:
    if repo not in REPOS:   # repo_kind() answers "kopia" for ids that do not exist
        raise HTTPException(status_code=400, detail=f"unknown repo {repo!r}")
    if repo_kind(repo) != "kopia":
        raise HTTPException(status_code=400, detail=f"repo {repo!r} is not a kopia repo")


@router.get("/churn")
def churn(repo: str = "1", days: int = 30, depth: int = 4, limit: int = 400,
          refresh: bool = False) -> dict:
    """New backed-up data in the last ``days``, rolled up three ways from one computation."""
    _require_kopia(repo)
    try:
        return churn_rollup(repo, days=days, depth=depth, limit=limit, refresh=refresh)
    except RepoOffline:
        raise                       # the dedicated 503 handler explains this one properly
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
        try:
            from ..core.sources import sources_index
            all_sources = list(sources_index(repo))
        except Exception:  # noqa: BLE001
            all_sources = [r["source"] for r in srcs]
        classify_folder_sources(prefix, srcs, all_sources)
        for row in srcs:
            row["policy"] = pol.get(row["source"])
        summary["sources"] = srcs
        summary["children"] = folder_top_children(repo, prefix, source=source)
        summary["file_types"] = folder_file_types(repo, prefix, source=source)
        try:
            summary["last_30_days"] = cached_folder_window(repo, prefix, days=30)
        except Exception:  # noqa: BLE001 - an optional row must never fail the panel
            summary["last_30_days"] = None
        covers = [r for r in srcs if r["relation"] == "covers"]
        summary["covers"] = len(covers)
        summary["sub_policies"] = len(srcs) - len(covers)
        summary["max_overlap"] = max((r["overlap"] for r in srcs), default=0)
        # A warning is only honest when the FOLDER ITSELF sits in more than one source. A folder
        # containing many separately-policied subfolders is not duplicated - it just has children
        # with their own rules, which is normal and not a delete hazard.
        summary["multi_source"] = len(covers) > 1
        return summary
    except HTTPException:
        raise
    except RepoOffline:
        raise                       # the dedicated 503 handler explains this one properly
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/folder/contents")
def folder_contents_route(repo: str = "1", prefix: str = "", source: str | None = None,
                          limit: int = 500) -> dict:
    """Everything directly inside a folder (the folder panel's Contents tab)."""
    _require_kopia(repo)
    if not prefix:
        raise HTTPException(status_code=400, detail="prefix is required")
    try:
        rows = folder_contents(repo, prefix, source=source, limit=max(1, min(limit, 2000)))
        for r in rows:
            r["last_seen"] = str(r["last_seen"]) if r.get("last_seen") else None
        return {"prefix": prefix, "rows": rows}
    except RepoOffline:
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
    except RepoOffline:
        raise                       # the dedicated 503 handler explains this one properly
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class FolderDetailRequest(BaseModel):
    repo: str = "1"
    prefix: str
    source: str | None = None  # None = every source covering the folder (the honest cross-source answer)


@router.post("/folder/detail")
def folder_detail_route(payload: FolderDetailRequest) -> dict:
    """Deduped physical, freeable and shared for the whole subtree, over EVERY version.

    Served from the nightly-prewarmed cache when possible. On a miss this computes synchronously -
    the frontend already fetches it asynchronously and patches the panel when it lands, so a slow
    folder shows "computing" rather than blocking anything."""
    _require_kopia(payload.repo)
    if not payload.prefix:
        raise HTTPException(status_code=400, detail="prefix is required")
    try:
        return folder_reclaim(payload.repo, payload.prefix, payload.source or None)
    except RepoOffline:
        raise                       # the dedicated 503 handler explains this one properly
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
