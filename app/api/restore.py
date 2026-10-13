"""Restore planning and downloads.

POST /api/restore/plan      what each item restores, where it lands, and how that compares with
                            what is already on disk (new / identical / different / extra)
POST /api/restore/check     the same comparison over every file (no cap), in the background;
                            GET /api/restore/check/{id} polls it, POST .../stop stops it
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
import threading
import time
import urllib.parse
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from config import DATA_DIR, is_demo

from ..core import audit
from ..core import paths as pathrules
from ..core.kopia import _base_command, check_readonly, open_in_explorer
from ..core.sources import sources_index
from ..index import entries
from ..index.holding import _segments, holding_snapshots
from ..index.queries import versions as file_versions
from . import freeids
from .actions import FULL_LIST_CAP

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
    version: bool = False            # the file version that snapshot_id holds (no id needed)


class PlanBody(BaseModel):
    repo: str
    items: list[RestoreItem]
    target_mode: str = "folder"      # folder | original | beside
    target: str = ""
    options: dict = {}
    preview: bool = True
    paths_shown: int = PATHS_SHOWN   # different/extra/bad paths listed per item ("Show all" asks for more)


class OpenBody(BaseModel):
    path: str


def _name(source: str, full_path: str) -> str:
    sep = pathrules.sep(source)
    return full_path.rstrip(sep).rsplit(sep, 1)[-1]


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
    if it.version and not it.content_id and it.snapshot_id:
        # The version the index lists at that snapshot: the same pick as passing its content id.
        row = next((v for v in file_versions(repo, it.source, it.full_path)
                    if v.get("snapshot_id") == it.snapshot_id), None)
        if row and row.get("content_id"):
            it = it.model_copy(update={"content_id": row["content_id"]})
    if it.content_id:
        size, snap_id, snap_time = None, it.snapshot_id or "", ""
        rows = [v for v in file_versions(repo, it.source, it.full_path) if v.get("content_id") == it.content_id]
        # the same content can return after a change (A -> B -> A): prefer the snapshot the user picked
        pick = next((v for v in rows if it.snapshot_id and v.get("snapshot_id") == it.snapshot_id), rows[0] if rows else None)
        if pick:
            size, snap_id, snap_time = pick.get("size"), pick.get("snapshot_id") or snap_id, pick.get("start_time") or ""
        live = snap_id and any(s.get("id") == snap_id for s in snaps)
        if not live:
            # The index can list a version whose snapshot kopia has since expired. A live snapshot
            # still holding the same content restores it with its attributes; only when none does
            # is the bare object restored (no modified time).
            held = holding_snapshots(repo, it.source, [it.full_path], snaps=snaps).get(it.full_path) or []
            same = next((h for h in held if h.get("obj") == it.content_id), None)
            if same:
                live, snap_id, snap_time = True, same["snapshot_id"], same["start_time"]
        return {"name": name, "obj": it.content_id, "type": "f", "size": int(size or 0), "files": 1,
                "mtime": "", "snapshot_id": snap_id, "snap_time": str(snap_time or ""),
                "expired": not live,
                "source_id": _source_id(it.source, it.full_path, snap_id) if live else it.content_id}
    if it.snapshot_id:
        snaps = [s for s in snaps if s.get("id") == it.snapshot_id]
    sep = pathrules.sep(it.source)
    root = pathrules.source_root(it.source).rstrip(sep)
    if pathrules.ident(it.source, it.full_path.rstrip(sep)) == pathrules.ident(it.source, root):
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


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def _bad_here(name: str) -> bool:
    """Whether this computer cannot create a file or folder with this name (a backup from another
    OS can hold names Windows refuses; a backslash in one would turn into a subfolder)."""
    if os.name != "nt":
        return "/" in name or "\x00" in name
    return (any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name) or name.endswith((" ", "."))
            or name.split(".", 1)[0].upper() in _WIN_RESERVED)


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


class _Stopped(Exception):
    """A full check was stopped by the user."""


def _listing(repo: str, obj: str, kind: str, size: int, mtime: str, bad: list | None = None,
             cap: int | None = LIST_CAP):
    """(relative path, size, mtime) for every file the object restores, stopping after ``cap``
    files (None = every file). ``bad`` collects the relative paths of entries whose own name this
    computer cannot create."""
    if kind != "d":
        yield "", size, mtime
        return
    level = [(obj, "")]
    count = 0
    while level:
        children = entries.children_of(repo, sorted({o for o, _ in level}))
        nxt = []
        for oid, rel in level:
            for e in children.get(oid, []):
                path = os.path.join(rel, e.get("name", "")) if rel else e.get("name", "")
                if bad is not None and _bad_here(e.get("name", "")):
                    bad.append(path)
                if e.get("type") == "d" and e.get("obj"):
                    nxt.append((e["obj"], path))
                elif e.get("type") != "d":
                    count += 1
                    if cap is not None and count > cap:
                        return
                    yield path, int(e.get("size", 0) or 0), e.get("mtime", "")
        level = nxt


def _compare(repo: str, r: dict, target: str, delete_extra: bool, cap: int | None = LIST_CAP,
             progress: dict | None = None, shown: int = PATHS_SHOWN) -> dict:
    """How the item compares with the destination. Above ``cap`` files the comparison covers the
    first ``cap`` (``capped``) and extra files are not checked (``extra`` None, never 0).
    ``progress`` (a full check) counts listed files and carries the stop flag."""
    out = {"new": 0, "identical": 0, "different": 0, "extra": 0, "listed": 0, "bad_names": 0,
           "different_paths": [], "extra_paths": [], "bad_paths": [], "capped": False}
    if target.lower().endswith(ARCHIVE_EXT):
        out["archive"] = True
        return out
    bad: list[str] = [target] if _bad_here(r.get("name") or "") else []
    wanted: set[str] = set()
    for rel, size, mtime in _listing(repo, r["obj"], r["type"], r["size"], r["mtime"], bad, cap=cap):
        out["listed"] += 1
        if progress is not None:
            progress["listed"] += 1
            if progress["stop"]:
                raise _Stopped()
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
            if len(out["different_paths"]) < shown:
                out["different_paths"].append(path)
    out["capped"] = cap is not None and out["listed"] >= cap
    out["bad_names"] = len(bad)
    out["bad_paths"] = bad[:shown]
    if delete_extra and out["capped"]:
        out["extra"] = None             # not checked: the listing stopped at the cap
    elif delete_extra and r["type"] == "d" and os.path.isdir(target):
        for root, _dirs, files in os.walk(target):
            if progress is not None and progress["stop"]:
                raise _Stopped()
            for f in files:
                p = os.path.join(root, f)
                if os.path.normcase(p) not in wanted:
                    out["extra"] += 1
                    if len(out["extra_paths"]) < shown:
                        out["extra_paths"].append(p)
    return out


@router.post("/restore/plan")
def restore_plan(body: PlanBody) -> dict:
    return _plan(body)


def _plan(body: PlanBody, cap: int | None = LIST_CAP, progress: dict | None = None) -> dict:
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
            row.update(_compare(body.repo, r, target, bool(body.options.get("delete_extra")),
                                cap=cap, progress=progress, shown=min(max(body.paths_shown, 1), FULL_LIST_CAP)))
        if freeids.free(body.repo):
            if row.get("source_id") and row["source_id"] == row.get("obj"):
                row["source_id"] = freeids.hold(row["obj"])   # a bare object: its snapshot expired
            row.pop("obj", None)
        items.append(row)
    totals = {k: sum(i.get(k) or 0 for i in items)
              for k in ("files", "size", "new", "identical", "different", "extra", "bad_names")}
    totals["capped"] = any(i.get("capped") for i in items)
    totals["extra_unchecked"] = any("extra" in i and i["extra"] is None for i in items)
    return {"repo": body.repo, "target_mode": mode, "items": items, "totals": totals,
            "full_check": cap is None}


# ---- full check: the same comparison over every file, in the background ----

_CHECKS: dict[str, dict] = {}
_CHECKS_LOCK = threading.Lock()
_CHECKS_KEPT = 10


@router.post("/restore/check")
def restore_check_start(body: PlanBody) -> dict:
    """Start comparing every file with the destination (the preview stops at LIST_CAP)."""
    check_id = uuid.uuid4().hex[:12]
    st = {"state": "running", "listed": 0, "stop": False, "plan": None, "error": "",
          "started": time.time()}
    with _CHECKS_LOCK:
        _CHECKS[check_id] = st
        for old in sorted(_CHECKS, key=lambda k: _CHECKS[k]["started"])[:-_CHECKS_KEPT]:
            _CHECKS.pop(old, None)

    def run() -> None:
        try:
            st["plan"] = _plan(body.model_copy(update={"preview": True}), cap=None, progress=st)
            st["state"] = "done"
        except _Stopped:
            st["state"] = "stopped"
        except HTTPException as exc:
            st["state"], st["error"] = "failed", str(exc.detail)
        except Exception as exc:  # noqa: BLE001 - reported to the panel, never raised in a thread
            st["state"], st["error"] = "failed", str(exc)[:300]
    threading.Thread(target=run, daemon=True).start()
    return {"check_id": check_id}


@router.get("/restore/check/{check_id}")
def restore_check_status(check_id: str) -> dict:
    st = _CHECKS.get(check_id)
    if st is None:
        raise HTTPException(status_code=404, detail="That check has ended. Start it again.")
    return {"state": st["state"], "listed": st["listed"], "error": st["error"],
            "plan": st["plan"] if st["state"] == "done" else None}


@router.post("/restore/check/{check_id}/stop")
def restore_check_stop(check_id: str) -> dict:
    st = _CHECKS.get(check_id)
    if st is not None:
        st["stop"] = True
    return {"ok": True}


def _content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


@router.get("/restore/download")
def restore_download(repo: str, source: str, full_path: str, snapshot_id: str | None = None,
                     content_id: str | None = None, version: bool = False):
    if is_demo(repo):
        from ..core.demoreplies import UNAVAILABLE
        raise HTTPException(status_code=400, detail=UNAVAILABLE)
    r = resolve_item(repo, RestoreItem(source=source, full_path=full_path, snapshot_id=snapshot_id,
                                       content_id=content_id, version=version))
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
