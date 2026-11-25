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

import datetime as dt
import os
import socket

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

from config import REPOS, repo_kind

from ..core import paths as pathrules
from ..core.policy import effective_for_path, source_summaries, summarize
from .errors import PASS_THROUGH
from ..index.churnview import cached_folder_window, churn_rollup
from ..index.foldercache import compute as folder_reclaim
from ..index.queries import (
    folder_contents,
    folder_file_types,
    folder_sources,
    folder_summary,
    folder_top_children,
)

def _root_of(source_key: str) -> str:
    """The path a source is rooted at, no trailing separator, under its own case rule."""
    sep = pathrules.sep(source_key)
    return pathrules.ident(source_key, pathrules.source_root(source_key).rstrip(sep))


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
    folder, _sep = pathrules.folder_key(prefix)
    for row in rows:
        src = row["source"]
        sep = pathrules.sep(src)
        root = roots.get(src, _root_of(src))
        base = pathrules.ident(src, folder)
        row["relation"] = "covers" if (base == root or base.startswith(root + sep)) else "inside"
        row["overlap"] = 1 + sum(
            1 for other, oroot in roots.items()
            if other != src and pathrules.sep(other) == sep and root.startswith(oroot + sep)
        )


router = APIRouter(tags=["insights"])


def _require_kopia(repo: str) -> None:
    if repo not in REPOS:   # repo_kind() answers "kopia" for ids that do not exist
        raise HTTPException(status_code=400, detail=f"unknown repo {repo!r}")
    if repo_kind(repo) != "kopia":
        raise HTTPException(status_code=400, detail=f"repo {repo!r} is not a kopia repo")


def _with_source_physical(repo: str, out: dict) -> dict:
    """Each source's all-versions physical, read from the dashboard's cached table (never
    computed here). Missing when the dashboard has not been built yet."""
    from ..core.metrics import _read_cache
    dash = _read_cache(repo, None) or {}
    phys = {r["source"]: r.get("physical") for r in dash.get("rows") or []}
    if phys:
        out = {**out, "by_source": [{**r, "physical_all": phys.get(r["source"])}
                                    for r in out.get("by_source") or []]}
    return out


@router.get("/churn")
def churn(repo: str, days: int = 30, depth: int = 4, limit: int = 400,
          refresh: bool = False) -> dict:
    """New backed-up data in the last ``days``, rolled up three ways from one computation."""
    _require_kopia(repo)
    try:
        return _with_source_physical(repo, churn_rollup(repo, days=days, depth=depth, limit=limit,
                                                        refresh=refresh))
    except PASS_THROUGH:
        raise                       # the dedicated 503 handler explains this one properly
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/folder/summary")
def folder_summary_route(repo: str, prefix: str = "", source: str | None = None) -> dict:
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
    except PASS_THROUGH:
        raise                       # the dedicated 503 handler explains this one properly
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/folder/contents")
def folder_contents_route(repo: str, prefix: str = "", source: str | None = None,
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
    except PASS_THROUGH:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/path/disk")
def path_on_disk(source: str, path: str) -> dict:
    """The path on this computer's disk right now, for a source backed up from this computer.
    ``checked`` is False when the source is another computer's (nothing to compare)."""
    from ..index.holding import _segments
    who = source.split(":", 1)[0]
    host = who.split("@")[-1].lower() if "@" in who else ""
    if not host or host != socket.gethostname().lower() or _segments(source, path) is None:
        return {"checked": False}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {"checked": True, "exists": False}
    except OSError as exc:
        return {"checked": True, "exists": None, "error": str(exc)}
    return {"checked": True, "exists": True, "is_dir": os.path.isdir(path), "size": st.st_size,
            "mtime": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()}


@router.get("/folder/versions")
def folder_versions(repo: str, prefix: str = "", source: str | None = None) -> dict:
    """How many distinct states the folder has had: the different folder objects at this path
    across every snapshot of the sources that back the folder itself (one object per state, since
    kopia stores an unchanged folder once). Walks the tree, so it is fetched after the panel opens."""
    _require_kopia(repo)
    if not prefix:
        raise HTTPException(status_code=400, detail="prefix is required")
    from ..index.holding import folder_states
    try:
        st = folder_states(repo, [prefix], source=source)[prefix]
        return {"prefix": prefix, "folder_versions": st["versions"], "snapshots": st["snapshots"],
                "sources": st["sources"]}
    except PASS_THROUGH:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/folder/policy")
def folder_policy(repo: str, source: str = "", prefix: str = "") -> dict:
    """The EFFECTIVE kopia policy for this exact folder - kopia resolves inheritance, so a folder
    with its own rules (or its own ignore list) reports them rather than the source's. One
    subprocess, cached; fetched lazily because it is the only part of the panel that shells out."""
    _require_kopia(repo)
    if not source or not prefix:
        raise HTTPException(status_code=400, detail="source and prefix are required")
    try:
        raw = effective_for_path(repo, source, prefix)
        return {"prefix": prefix, "source": source, "policy": summarize(raw), "found": bool(raw)}
    except PASS_THROUGH:
        raise                       # the dedicated 503 handler explains this one properly
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class FolderDetailRequest(BaseModel):
    repo: str
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
    except PASS_THROUGH:
        raise                       # the dedicated 503 handler explains this one properly
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
