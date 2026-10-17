"""Delete panel data: exactly what each delete option touches, before anything runs.

POST /api/delete/plan
  {repo, files: [{source, full_path}], method: "remove"|"snapshots", by: "object-id"|"filename",
   sources: [..] | null, copies: bool}

``remove`` = kopia snapshot fix remove-files (snapshots stay). ``by`` picks the match:
object-id removes that content at every path inside the scoped sources; filename removes every
file with that name inside them. ``snapshots`` = delete every current snapshot holding the
selected paths (plus identical copies at other paths when ``copies``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import paths as pathrules
from ..core.kopia import remove_files_chunked_commands, snapshot_delete_commands
from ..core.sources import sources_index
from ..index.holding import holding_snapshots
from ..index.physical import batch_detail, copies_outside, price_objects
from ..index.queries import (holders_for_paths, paths_for_contents, paths_named,
                             sources_for_content, versions_many)
from ..licensing import features
from .actions import FULL_LIST_CAP, _fmt_cmd

router = APIRouter(tags=["delete"])

LIST_CAP = 500
COPIES_CAP = 20000   # copy paths resolved for a snapshot delete; beyond this the plan refuses to run
PRICE_CAP = 200_000  # same-named files priced for a by-name removal


class PlanFile(BaseModel):
    source: str
    full_path: str


class PlanBody(BaseModel):
    repo: str
    files: list[PlanFile]
    method: str = "remove"
    by: str = "object-id"
    sources: list[str] | None = None
    copies: bool = False
    list_cap: int | None = None   # "Show all": list up to this many matches instead of LIST_CAP


def _basename(source: str, full_path: str) -> str:
    sep = pathrules.sep(source)
    return full_path.rstrip(sep).rsplit(sep, 1)[-1]


def _machine(source: str) -> str:
    """The ``user@host`` part of a source key."""
    return source.split(":", 1)[0]


def selection_facts(repo: str, files: list[tuple[str, str]]) -> dict:
    """Holders, every version's content id, and names for a selection.

    Holders are the sources on the selected file's own machine that hold the same path: another
    machine's file at an identical path is a different file."""
    by_path = holders_for_paths(repo, [fp for _, fp in files])
    holders: list[list[str]] = []
    for src, fp in files:
        same = [h for h in by_path.get(fp) or [] if _machine(h) == _machine(src)]
        holders.append(same or [src])
    oids: list[str] = []
    seen: set[str] = set()
    versions = 0
    # one batched lookup for the whole selection - the per-file-per-holder shape scanned
    # pathx once per pair and is what made DEL2's panel take minutes on a 500-file selection.
    vers = versions_many(repo, [(h, fp) for (_src, fp), hs in zip(files, holders) for h in hs])
    for (src, fp), hs in zip(files, holders):
        for h in hs:
            for v in vers.get((h, fp)) or []:
                versions += 1
                oid = v.get("content_id")
                if oid and oid not in seen:
                    seen.add(oid)
                    oids.append(oid)
    names = list(dict.fromkeys(_basename(s, fp) for s, fp in files))
    holder_union = sorted({h for hs in holders for h in hs} | {s for s, _ in files})
    return {"holders": holders, "holder_union": holder_union, "oids": oids,
            "names": names, "versions": versions}


def resolve_scope(repo: str, by: str, sources: list[str] | None, facts: dict) -> list[str]:
    """The sources a removal touches. Explicit ticks win; otherwise content mode covers every
    source holding the content and name mode only the sources holding the selected paths."""
    if sources is not None:
        return sorted(set(sources))
    if by == "filename":
        return facts["holder_union"]
    return sources_for_content(repo, facts["oids"]) or facts["holder_union"]


@router.post("/delete/plan")
def delete_plan(body: PlanBody) -> dict:
    files = [(f.source, f.full_path) for f in body.files if f.source and f.full_path]
    if not files:
        raise HTTPException(status_code=400, detail="no files specified")
    if body.method not in ("remove", "snapshots") or body.by not in ("object-id", "filename"):
        raise HTTPException(status_code=400, detail="unknown method or match")
    if body.sources is not None and not body.sources:
        raise HTTPException(status_code=400, detail="pick at least one backup")
    repo = body.repo
    facts = selection_facts(repo, files)
    selected = {(pathrules.host(s), fp) for s, fp in files}   # one file = computer + path
    content_sources = sources_for_content(repo, facts["oids"])
    out: dict = {
        "repo": repo, "method": body.method, "by": body.by,
        "files": [{"source": s, "full_path": fp, "holders": hs}
                  for (s, fp), hs in zip(files, facts["holders"])],
        "holders": facts["holder_union"],
        "content_sources": content_sources,
        # backups the user can tick: remove mode reaches every content holder; snapshot mode the
        # path's holders, plus content holders when identical copies are included
        "candidates": (sorted(set(facts["holder_union"]) | set(content_sources))
                       if body.method == "remove" or body.copies else facts["holder_union"]),
        "versions": facts["versions"], "object_count": len(facts["oids"]), "names": facts["names"],
    }

    if body.method == "remove":
        scope = resolve_scope(repo, body.by, body.sources, facts)
        cap = min(max(body.list_cap or LIST_CAP, 1), FULL_LIST_CAP)
        if body.by == "filename":
            found = paths_named(repo, scope, facts["names"], cap=cap, mark=files)
            items, flag = facts["names"], "--filename"
        else:
            found = paths_for_contents(repo, facts["oids"], sources=scope, cap=cap,
                                       mark=files)
            items, flag = facts["oids"], "--object-id"
        rows = [{**r, "selected": (pathrules.host(r["source"]), r["full_path"]) in selected} for r in found["rows"]]
        # Free installs get the full preview, never the command that runs it.
        pro = features.allowed("remove", repo)  # licence check: do not remove or bypass (KopiaLens License)
        cmds = remove_files_chunked_commands(repo, items, source=scope, commit=True,
                                             max_chars=8000, flag=flag) if items and pro else []
        out.update({
            "scope": scope,
            "matches": rows, "match_total": found["total"],
            "other_count": max(0, found["total"] - found["marked"]),
            "capped": found["total"] > len(rows),
            "command": "\n".join(_fmt_cmd(c) for c in cmds),
        })
        return out

    # method == "snapshots"
    scope = sorted(set(body.sources)) if body.sources is not None else out["candidates"]
    pairs: dict[str, set[str]] = {}
    for (s, fp), hs in zip(files, facts["holders"]):
        for h in hs:
            if h in scope:
                pairs.setdefault(h, set()).add(fp)
    copies_capped = False
    if body.copies and facts["oids"]:
        found = paths_for_contents(repo, facts["oids"], sources=scope, cap=COPIES_CAP)
        copies_capped = found["total"] > len(found["rows"])
        for r in found["rows"]:
            pairs.setdefault(r["source"], set()).add(r["full_path"])
    index = sources_index(repo)
    by_id: dict[str, dict] = {}
    for src, fps in pairs.items():
        held = holding_snapshots(repo, src, sorted(fps), snaps=index.get(src, []))
        for fp, rows in held.items():
            for r in rows:
                if (pathrules.host(src), fp) not in selected and r["obj"] not in facts["oids"]:
                    continue
                snap = by_id.setdefault(r["snapshot_id"], {
                    "snapshot_id": r["snapshot_id"], "source": src, "start_time": r["start_time"],
                    "pinned": r["pinned"], "retention": r["retention"], "paths": []})
                if fp not in snap["paths"]:
                    snap["paths"].append(fp)
    snaps = sorted(by_id.values(), key=lambda s: s["start_time"], reverse=True)
    ids = [s["snapshot_id"] for s in snaps]
    # licence check: do not remove or bypass (KopiaLens License)
    if not features.allowed("purge", repo):   # Free: which snapshots and when, without their ids
        snaps = [{**s, "snapshot_id": ""} for s in snaps]
        ids = []
    cmds = snapshot_delete_commands(repo, ids, batch=True) if ids else []
    out.update({
        "scope": scope,
        "snapshots": snaps,
        "pinned_count": sum(1 for s in snaps if s["pinned"]),
        "retained_count": sum(1 for s in snaps if s["retention"]),
        "capped": copies_capped,
        "command": "\n".join(" ".join(c) for c in cmds),
    })
    return out


def _removal_price(repo: str, by: str, scope: list[str], facts: dict) -> dict:
    """What a removal frees. By content: the selected files' objects leave every path holding them
    in ``scope`` and nothing else goes. By file name: exactly the same-named files in ``scope``
    go, every version; an identical file under another name keeps its data ()."""
    if by == "filename":
        found = paths_named(repo, scope, facts["names"], cap=PRICE_CAP)
        pairs = [(s, r["full_path"]) for r in found["rows"] for s in r["sources"]]
        if not pairs:
            return {"freeable": 0, "physical": 0, "kept": 0, "kept_files": [], "kept_count": 0,
                    "capped": False}
        d = batch_detail(repo, [{"source": s, "full_path": fp} for s, fp in pairs],
                         all_versions=True, keep_copies=True)
        kept = copies_outside(repo, pairs, cap=LIST_CAP)
        return {"freeable": d.get("freeable", 0), "physical": d.get("physical", 0),
                "kept": d.get("kept", 0), "kept_files": kept["rows"], "kept_count": kept["total"],
                "capped": found["total"] > len(found["rows"])}
    d = price_objects(repo, facts["oids"], sources=scope)
    return {"freeable": d["freeable"], "physical": d["physical"], "capped": False}


@router.post("/delete/stats")
def delete_stats(body: PlanBody) -> dict:
    """Reclaimable for a removal from the delete panel, priced over every match (the panel's list
    stops at LIST_CAP), plus the other match method's figure for the side-by-side."""
    files = [(f.source, f.full_path) for f in body.files if f.source and f.full_path]
    if not files:
        raise HTTPException(status_code=400, detail="no files specified")
    if body.by not in ("object-id", "filename"):
        raise HTTPException(status_code=400, detail="unknown match")
    facts = selection_facts(body.repo, files)
    out = _removal_price(body.repo, body.by, resolve_scope(body.repo, body.by, body.sources, facts), facts)
    other = "filename" if body.by == "object-id" else "object-id"
    alt = _removal_price(body.repo, other, resolve_scope(body.repo, other, body.sources, facts), facts)
    out["alt_freeable"] = alt["freeable"]
    return out
