"""TreeSize-style browser endpoints."""

from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, HTTPException

from ..core.sources import sources_index
from ..index.schema import RepoOffline
from ..index.queries import (
    flat_files,
    tree_children,
    tree_children_all,
    tree_children_live,
    tree_children_paths,
    tree_children_snaps,
)

router = APIRouter(tags=["tree"])


def _parse_start(s: str) -> dt.datetime | None:
    """kopia's tz-aware ISO startTime; trims >6 fractional digits (fromisoformat caps at 6). A
    timestamp WITHOUT an offset would compare against the aware week cutoff and throw, so it is
    coerced to local time - kopia always includes one today, this is the guard."""
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(re.sub(r"\.(\d{1,6})\d*", r".\1", s))
    except ValueError:
        return None
    return t.astimezone()  # naive (no offset) is assumed local; aware converts to local


def _week_bounds(week: str) -> tuple[dt.datetime, dt.datetime]:
    """``week`` is the ISO date of the chosen week's LAST day. Returns that week's first moment
    and last moment in the local timezone (kopia's startTimes carry offsets)."""
    end_d = dt.date.fromisoformat(week)
    start_d = end_d - dt.timedelta(days=6)
    return (dt.datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0).astimezone(),
            dt.datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59).astimezone())


def _snapshot_ids_in_week(repo: str, week: str) -> list[str]:
    """Every snapshot, any source, that STARTED inside the chosen week (KL13 "Week" view).

    Bounded at BOTH ends on purpose. Taking the latest snapshot at-or-before the week's end had
    no lower bound, so a source last backed up in July answered a query about a week in August -
    the view named a week it was not showing. A source that did not back up that week now
    contributes nothing, which is the honest answer.

    All of the week's snapshots are included, not just the last: for a source backing up nightly,
    "what changed this week" is the union of that week's runs. A path changed twice in the week
    therefore contributes twice to the size totals, the same as the existing All-snapshots mode."""
    start, end = _week_bounds(week)
    ids = [s.get("id") for snaps in sources_index(repo).values() for s in snaps
           if (t := _parse_start(str(s.get("startTime") or ""))) and start <= t <= end]
    return [i for i in ids if i]


def _root_obj(repo: str, source: str, snapshot_id: str) -> str | None:
    for snap in sources_index(repo).get(source, []):
        if snap.get("id") == snapshot_id:
            return snap.get("rootEntry", {}).get("obj")
    return None


def _post_filter(children: list[dict], over_gb: bool, ext: str | None, ext_exclude: str | None) -> list[dict]:
    """File-level filters for the point-in-time tree modes (full/changed/reclaimable) where there
    is no SQL pass. Folders always pass through (so you can still navigate); only FILE rows are
    filtered by >=1 GB / extension include / extension exclude."""
    if not over_gb and not ext and not ext_exclude:
        return children
    inc = {e.lower().lstrip(".") for e in (ext or "").split(",") if e.strip()}
    exc = {e.lower().lstrip(".") for e in (ext_exclude or "").split(",") if e.strip()}
    out = []
    for c in children:
        if c.get("type") == "folder":
            out.append(c)
            continue
        name = (c.get("name") or "")
        dot = name.rfind(".")
        e = name[dot + 1:].lower() if dot > 0 else ""
        if over_gb and (c.get("size") or 0) < 1_000_000_000:
            continue
        if inc and e not in inc:
            continue
        if exc and e in exc:
            continue
        out.append(c)
    return out


def _dedupe_children(children: list[dict]) -> list[dict]:
    """Collapse file children sharing the same content (content_id) into one - content-aware view.
    Folders and content-less rows pass through unchanged."""
    out, seen = [], set()
    for c in children:
        cid = c.get("content_id")
        if c.get("type") == "file" and cid:
            if cid in seen:
                continue
            seen.add(cid)
        out.append(c)
    return out


@router.get("/tree")
def tree(
    repo: str = "1",
    source: str | None = None,
    snapshot_id: str | None = None,
    prefix: str = "",
    mode: str = "full",
    dedupe: bool = False,
    deleted_only: bool = False,
    over_gb: bool = False,
    ext: str | None = None,
    ext_exclude: str | None = None,
    flat: bool = False,
    week: str | None = None,
) -> dict:
    """mode=full (complete point-in-time tree, default) | changed (only what changed) |
    reclaimable (files freed if this snapshot is deleted) | all (every snapshot, source-wide).
    ``dedupe`` collapses same-content files into one row (content-aware). Filters: deleted_only
    (all mode only) / over_gb / ext / ext_exclude. ``flat`` = recursive flat file list under the
    prefix (collapse folders) - paths-based, works in any mode. ``week`` (All-Sources only) =
    ISO date of a week's last day; lists what the snapshots taken INSIDE that week recorded as
    changed (the delta index, not a complete point-in-time tree)."""
    if not source:
        raise HTTPException(status_code=400, detail="source is required")
    try:
        if source == "*ALL*":  # repo-wide aggregate across every source (parity with Snapshots *ALL*)
            min_size = 1_000_000_000 if over_gb else 0
            if week and mode != "reclaimable":
                # Week view: the snapshots taken inside that week, through the existing
                # snapshot-set query. files is a DELTA index - this lists what those snapshots
                # recorded as changed, NOT a full point-in-time tree (that needs a dir-cache
                # reconstruction). Flat/reclaimable keep their latest-only semantics.
                ids = _snapshot_ids_in_week(repo, week)
                children = _post_filter(tree_children_snaps(repo, ids, prefix), over_gb, ext, ext_exclude)
            elif flat:
                children = flat_files(repo, None, prefix, deleted_only=deleted_only,
                                      min_size=min_size, ext=ext, ext_exclude=ext_exclude)
            elif mode == "changed":  # union of each source's LATEST-snapshot delta
                latest = [max(s, key=lambda x: x.get("startTime", "")).get("id")
                          for s in sources_index(repo).values() if s]
                children = _post_filter(tree_children_snaps(repo, [i for i in latest if i], prefix),
                                        over_gb, ext, ext_exclude)
            elif mode == "reclaimable":  # option (b): only ALREADY-CACHED reclaimable (never walks)
                from ..index.snapanalysis import cached_reclaimable_files
                b = prefix.rstrip("\\")
                acc: list[dict] = []
                for snaps in sources_index(repo).values():
                    if not snaps:
                        continue
                    sid = max(snaps, key=lambda x: x.get("startTime", "")).get("id")
                    for f in (cached_reclaimable_files(repo, sid) or []):
                        fp = f["full_path"]
                        if (not b) or fp.startswith(b):
                            acc.append({"path": fp, "name": fp.split("\\")[-1].split("/")[-1],
                                        "size": f["size"], "physical": f["size"], "files": 1,
                                        "modified": "", "type": "file"})
                acc.sort(key=lambda x: x["size"], reverse=True)
                children = acc[:1000]
            else:  # full = latest snapshot of each source (A#2); all = all-snapshots aggregate (A#1)
                children = tree_children_all(repo, prefix, latest_only=(mode != "all"),
                                             deleted_only=deleted_only, min_size=min_size,
                                             ext=ext, ext_exclude=ext_exclude)
            if dedupe:
                children = _dedupe_children(children)
            return {"children": children, "mode": mode}
        if flat:
            children = flat_files(
                repo, source, prefix, deleted_only=deleted_only,
                min_size=1_000_000_000 if over_gb else 0, ext=ext, ext_exclude=ext_exclude,
            )
            return {"children": children, "mode": "flat"}
        if mode == "all":
            children = tree_children_paths(
                repo, source, prefix, deleted_only=deleted_only,
                min_size=1_000_000_000 if over_gb else 0, ext=ext, ext_exclude=ext_exclude,
            )
        elif mode == "reclaimable":
            if not snapshot_id:
                raise HTTPException(status_code=400, detail="snapshot_id is required")
            from ..index.snapanalysis import reclaimable_files  # accurate exclusive-content list (flat)
            rfs = reclaimable_files(repo, source, snapshot_id, limit=500)
            children = [{"path": f["full_path"], "name": f["full_path"].split("\\")[-1].split("/")[-1],
                         "size": f["size"], "physical": f["size"], "files": 1, "modified": "", "type": "file"}
                        for f in rfs]
        else:
            if not snapshot_id:
                raise HTTPException(status_code=400, detail="snapshot_id is required")
            if mode == "changed":
                children = tree_children(repo, source, snapshot_id, prefix)
            else:
                root = _root_obj(repo, source, snapshot_id)
                if not root:
                    raise HTTPException(status_code=404, detail="snapshot root not found")
                children = tree_children_live(repo, source, root, prefix)
        if mode != "all":  # 'all' filtered in SQL; the point-in-time modes filter in Python
            children = _post_filter(children, over_gb, ext, ext_exclude)
        if dedupe:
            children = _dedupe_children(children)
        return {"children": children, "mode": mode}
    except HTTPException:
        raise
    except RepoOffline:
        raise                       # let the dedicated 503 handler explain it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
