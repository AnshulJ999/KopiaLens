"""Restore planning and downloads.

POST /api/restore/plan      what each item restores, where it lands, and how that compares with
                            what is already on disk (new / identical / different / extra)
GET  /api/restore/download  a file streamed from the repository, or a folder as a .zip
POST /api/restore/open      open a restored path in Explorer

A file object restored by kopia is written AT the target path, so a folder target always gets
the item's name appended here.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import stat as statmod
import subprocess
import urllib.parse
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from config import DATA_DIR

from ..core import audit
from ..core import paths as pathrules
from ..core.cache import cache_db, get_children
from ..core.kopia import _base_command, check_readonly, open_in_explorer
from ..core.sources import sources_index
from ..index.holding import _segments, holding_snapshots
from ..index.queries import versions as file_versions

router = APIRouter(tags=["restore"])

LIST_CAP = 100_000        # entries walked for one preview
PATHS_SHOWN = 200         # different/extra paths returned per item
ARCHIVE_EXT = (".zip", ".tar", ".tar.gz", ".tgz")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RestoreItem(BaseModel):
    source: str
    full_path: str
    snapshot_id: str | None = None   # a specific snapshot; default the newest holding the path
    content_id: str | None = None    # a specific file version


class PlanBody(BaseModel):
    repo: str = "1"
    items: list[RestoreItem]
    target_mode: str = "folder"      # folder | original | beside
    target: str = ""
    options: dict = {}
    preview: bool = True


class OpenBody(BaseModel):
    path: str


def _name(source: str, full_path: str) -> str:
    return full_path.rstrip("\\/").rsplit(pathrules.sep(source), 1)[-1]


def _ts(value) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _source_id(source: str, full_path: str, snapshot_id: str) -> str:
    """``<snapshot id>/<path inside it>``: restores the entry with its own attributes (a bare
    object id carries none, so kopia writes a 1970 modified time)."""
    segs = _segments(source, full_path) or []
    return "/".join([snapshot_id, *segs])


def resolve_item(repo: str, it: RestoreItem) -> dict:
    """The repository object behind an item: ``{obj, source_id, type, size, files, mtime, snapshot_id, snap_time}``."""
    name = _name(it.source, it.full_path)
    snaps = sources_index(repo).get(it.source, [])
    if it.content_id:
        size, snap_id, snap_time = None, it.snapshot_id or "", ""
        rows = [v for v in file_versions(repo, it.source, it.full_path) if v.get("content_id") == it.content_id]
        # the same content can return after a change (A -> B -> A): prefer the snapshot the user picked
        pick = next((v for v in rows if it.snapshot_id and v.get("snapshot_id") == it.snapshot_id), rows[0] if rows else None)
        if pick:
            size, snap_id, snap_time = pick.get("size"), pick.get("snapshot_id") or snap_id, pick.get("start_time") or ""
        live = snap_id and any(s.get("id") == snap_id for s in snaps)
        return {"name": name, "obj": it.content_id, "type": "f", "size": int(size or 0), "files": 1,
                "mtime": "", "snapshot_id": snap_id, "snap_time": str(snap_time or ""),
                # the version's snapshot may have expired; the content object still restores
                "source_id": _source_id(it.source, it.full_path, snap_id) if live else it.content_id}
    if it.snapshot_id:
        snaps = [s for s in snaps if s.get("id") == it.snapshot_id]
    root = pathrules.source_root(it.source).rstrip("\\/")
    if it.full_path.rstrip("\\/").lower() == root.lower():
        if not snaps:
            raise HTTPException(status_code=404, detail="That snapshot no longer exists.")
        sn = max(snaps, key=lambda s: s.get("startTime", ""))
        entry = sn.get("rootEntry") or {}
        summ = entry.get("summ") or {}
        return {"name": name.rstrip(":") or root, "obj": entry.get("obj", ""), "type": "d",
                "size": int(summ.get("size", 0) or 0), "files": int(summ.get("files", 0) or 0),
                "mtime": entry.get("mtime", ""), "snapshot_id": sn.get("id"), "snap_time": sn.get("startTime", ""),
                "source_id": sn.get("id")}
    held = holding_snapshots(repo, it.source, [it.full_path], snaps=snaps).get(it.full_path) or []
    if not held:
        raise HTTPException(status_code=404, detail=f"{name} is not in any current snapshot.")
    h = held[0]
    return {"name": name, "obj": h["obj"], "type": h["type"], "size": h["size"],
            "files": h["files"] if h["type"] == "d" else 1, "mtime": h["mtime"],
            "snapshot_id": h["snapshot_id"], "snap_time": h["start_time"],
            "source_id": _source_id(it.source, it.full_path, h["snapshot_id"])}


def _beside(full_path: str, snap_time: str, sep: str) -> str:
    """A dated copy next to the original, split with the source's own separator."""
    folder, _, base = full_path.rstrip(sep).rpartition(sep)
    stem, ext = os.path.splitext(base)
    day = (snap_time or dt.date.today().isoformat())[:10]
    cand = f"{folder}{sep}{stem} ({day}){ext}"
    n = 2
    while os.path.exists(cand):
        cand = f"{folder}{sep}{stem} ({day} {n}){ext}"
        n += 1
    return cand


def _writable_here(path: str) -> bool:
    """Whether a path from a backup can exist on this computer (Windows drive/UNC vs POSIX)."""
    if os.name == "nt":
        return bool(re.match(r"^([A-Za-z]:[\\/]|[\\/]{2}[^\\/])", path))
    return path.startswith("/")


def _flat_names(names: list[str]) -> list[str]:
    """Unique file names for a flat restore of several items into one folder."""
    seen: dict[str, int] = {}
    out = []
    for n in names:
        key = n.lower()
        if key not in seen:
            seen[key] = 1
            out.append(n)
            continue
        seen[key] += 1
        stem, ext = os.path.splitext(n)
        out.append(f"{stem} ({seen[key]}){ext}")
    return out


def _listing(repo: str, obj: str, kind: str, size: int, mtime: str):
    """(relative path, size, mtime) for every file the object restores, capped at LIST_CAP."""
    if kind != "d":
        yield "", size, mtime
        return
    db = cache_db(repo)
    level = [(obj, "")]
    count = 0
    while level:
        children = get_children(repo, db, sorted({o for o, _ in level}))
        nxt = []
        for oid, rel in level:
            for e in children.get(oid, []):
                path = os.path.join(rel, e.get("name", "")) if rel else e.get("name", "")
                if e.get("type") == "d" and e.get("obj"):
                    nxt.append((e["obj"], path))
                elif e.get("type") != "d":
                    count += 1
                    if count > LIST_CAP:
                        return
                    yield path, int(e.get("size", 0) or 0), e.get("mtime", "")
        level = nxt


def _compare(repo: str, r: dict, target: str, delete_extra: bool) -> dict:
    out = {"new": 0, "identical": 0, "different": 0, "extra": 0, "listed": 0,
           "different_paths": [], "extra_paths": [], "capped": False}
    if target.lower().endswith(ARCHIVE_EXT):
        out["archive"] = True
        return out
    wanted: set[str] = set()
    for rel, size, mtime in _listing(repo, r["obj"], r["type"], r["size"], r["mtime"]):
        out["listed"] += 1
        path = os.path.join(target, rel) if rel else target
        if delete_extra:
            wanted.add(os.path.normcase(path))
        try:
            st = os.stat(path)
        except OSError:
            out["new"] += 1
            continue
        want_t = _ts(mtime)
        if (not statmod.S_ISDIR(st.st_mode) and st.st_size == size
                and (want_t is None or abs(st.st_mtime - want_t) < 2)):
            out["identical"] += 1
        else:
            out["different"] += 1
            if len(out["different_paths"]) < PATHS_SHOWN:
                out["different_paths"].append(path)
    out["capped"] = out["listed"] >= LIST_CAP
    if delete_extra and r["type"] == "d" and os.path.isdir(target) and not out["capped"]:
        for root, _dirs, files in os.walk(target):
            for f in files:
                p = os.path.join(root, f)
                if os.path.normcase(p) not in wanted:
                    out["extra"] += 1
                    if len(out["extra_paths"]) < PATHS_SHOWN:
                        out["extra_paths"].append(p)
    return out


@router.post("/restore/plan")
def restore_plan(body: PlanBody) -> dict:
    if not body.items:
        raise HTTPException(status_code=400, detail="nothing to restore")
    mode = body.target_mode
    if mode not in ("folder", "original", "beside"):
        raise HTTPException(status_code=400, detail="unknown target")
    if mode == "folder" and not body.target.strip():
        raise HTTPException(status_code=400, detail="Pick a destination folder.")
    resolved = [resolve_item(body.repo, it) for it in body.items]
    if mode == "beside" and any(r["type"] == "d" for r in resolved):
        raise HTTPException(status_code=400, detail="Next to the original works for files only.")
    archive = mode == "folder" and body.target.strip().lower().endswith(ARCHIVE_EXT)
    if archive and len(resolved) > 1:
        raise HTTPException(status_code=400, detail="An archive target takes one item.")
    names = _flat_names([r["name"] for r in resolved])
    items = []
    for it, r, name in zip(body.items, resolved, names):
        if mode in ("original", "beside") and not _writable_here(it.full_path):
            raise HTTPException(status_code=400, detail=(
                f"{it.full_path} is not a path on this computer. Restore to a folder instead."))
        if mode == "original":
            target = it.full_path
        elif mode == "beside":
            target = _beside(it.full_path, r["snap_time"], pathrules.sep(it.source))
        elif archive:
            target = body.target.strip()
        else:
            target = os.path.join(body.target.strip(), name)
        row = {**r, "source": it.source, "full_path": it.full_path,
               "target": target, "target_exists": os.path.exists(target)}
        if body.preview:
            row.update(_compare(body.repo, r, target, bool(body.options.get("delete_extra"))))
        items.append(row)
    totals = {k: sum(i.get(k, 0) for i in items) for k in ("files", "size", "new", "identical", "different", "extra")}
    return {"repo": body.repo, "target_mode": mode, "items": items, "totals": totals}


def _content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


@router.get("/restore/download")
def restore_download(repo: str, source: str, full_path: str, snapshot_id: str | None = None,
                     content_id: str | None = None):
    r = resolve_item(repo, RestoreItem(source=source, full_path=full_path, snapshot_id=snapshot_id,
                                       content_id=content_id))
    audit.audit("executed", "download", repo=repo,
                summary={"path": full_path, "snapshot_id": r["snapshot_id"], "type": r["type"]})
    if r["type"] != "d":
        args = ["show", r["obj"]]
        check_readonly(args)
        cmd = [a for a in _base_command(repo, args) if a != "--no-progress"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)

        def chunks():
            try:
                while True:
                    block = proc.stdout.read(1024 * 1024)
                    if not block:
                        break
                    yield block
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

        headers = {"Content-Disposition": _content_disposition(r["name"])}
        if r["size"]:
            headers["Content-Length"] = str(r["size"])
        return StreamingResponse(chunks(), media_type="application/octet-stream", headers=headers)

    work = os.path.join(DATA_DIR, "tmp", "downloads", uuid.uuid4().hex)
    os.makedirs(work, exist_ok=True)
    zip_name = f"{r['name']}.zip"
    zip_path = os.path.join(work, zip_name)
    cmd = _base_command(repo, ["restore", r["source_id"], zip_path])
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if res.returncode != 0 or not os.path.exists(zip_path):
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(status_code=500, detail=(res.stderr or "").strip()[-300:] or "could not build the zip")
    return FileResponse(zip_path, filename=zip_name, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))


@router.post("/restore/open")
def restore_open(body: OpenBody) -> dict:
    if not body.path or not os.path.exists(body.path):
        raise HTTPException(status_code=404, detail="That path does not exist.")
    open_in_explorer(body.path)
    return {"opening": body.path}
