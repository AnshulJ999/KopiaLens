"""Setup endpoints: the kopia program, and creating/connecting/editing/removing repositories.

The screens for these live with onboarding () and the repo-management design session; this
router is the backend contract they sit on. Nothing here runs kopia on a polled request - a live
call only ever happens because the user asked for one (a probe, a create/connect, an unlock).
"""

from __future__ import annotations

import os
import sys

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..core import kopiabin, repoconnect, repos
from ..settings import mutate_settings

router = APIRouter(tags=["setup"])


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _duplicate(exc: repoconnect.DuplicateRepo) -> JSONResponse:
    """409 naming the repository it duplicates, so the screen can ask before adding it anyway."""
    return JSONResponse({"detail": str(exc), "duplicate_of": exc.repo, "duplicate_name": exc.name},
                        status_code=409)


class KopiaPathRequest(BaseModel):
    path: str = ""


class ProbeRequest(BaseModel):
    config_file: str | None = None
    repo: str | None = None


class ConnectRequest(BaseModel):
    provider: str
    fields: dict = {}
    password: str = ""
    name: str = ""
    short_name: str = ""
    counts_as_backup: bool = True
    allow_duplicate: bool = False   # the user confirmed adding a repository that is already here
    config_location: str = ""       # data | kopia | folder; empty = the default (repoconnect.config_location)
    config_folder: str = ""         # the folder, for "folder"


class CreateRequest(ConnectRequest):
    # `kopia repository create` format options (unchangeable later; absent = kopia's defaults)
    options: dict = {}


class AttachRequest(BaseModel):
    config_file: str | None = None
    name: str = ""
    short_name: str = ""
    counts_as_backup: bool = True
    allow_duplicate: bool = False


class UnlockRequest(BaseModel):
    password: str = ""


class RepoEditRequest(BaseModel):
    name: str | None = None
    short_name: str | None = None
    counts_as_backup: bool | None = None


@router.get("/setup/kopia")
def get_kopia() -> dict:
    """Which kopia program KopiaLens will run, where it was found, and the version (display only).

    `found` lists EVERY kopia on this machine with its version (one spawn each) - the data the
    Settings row and the one-time "a different kopia was found" notice need. The notice itself is
    the UI's to design."""
    info = kopiabin.info(with_candidates=True)
    info["found"] = kopiabin.inventory()
    return info


@router.get("/setup/data")
def get_data_status() -> dict:
    """The data folder's format version and what (if anything) is pending, plus each repo's index
    format - the `` state, for the Settings/diagnostics surfaces."""
    from ..core import dataupgrade
    from ..index import upgrade as index_upgrade

    out = dataupgrade.status()
    out["index"] = [index_upgrade.index_status(str(rec.get("id")))
                    for rec in repos.all_records() if rec.get("kind", "kopia") == "kopia"]
    return out


@router.post("/setup/kopia")
def set_kopia(payload: KopiaPathRequest) -> dict:
    """Save an explicit path (empty = back to automatic) after checking it really is kopia."""
    path = (payload.path or "").strip()
    if path:
        check = kopiabin.check(path)
        if not check["ok"]:
            raise HTTPException(status_code=400, detail=check["detail"] or "That is not kopia.")
        path = os.path.abspath(path)
    mutate_settings(lambda s: s.setdefault("kopialens", {}).update({"kopia_path": path}))
    kopiabin.invalidate()
    info = kopiabin.info()
    if not info["available"]:
        raise HTTPException(status_code=400, detail=info["detail"] or "kopia was not found.")
    info["version"] = kopiabin.version(force=True) or ""
    return info


@router.get("/setup/providers")
def get_providers() -> dict:
    """Every storage type kopia supports, with the field specs the connect form renders."""
    from ..core import host
    # In a container kopia's own folder isn't kept across a recreate, so it isn't offered there.
    return {"providers": repoconnect.provider_list(), "config_location": repoconnect.config_location(),
            "kopia_folder_ok": not host.in_docker(), "format_choices": repoconnect.FORMAT_CHOICES}


@router.get("/setup/detect-configs")
def detect_configs() -> dict:
    """kopia connections already on this machine (KopiaUI's, the CLI default) - the zero-typing
    path. Reads the config files only; a reachability check is a separate, explicit probe."""
    return {"dir": repoconnect.kopia_config_dir(), "configs": repoconnect.detect_configs()}


@router.post("/setup/probe")
def probe(payload: ProbeRequest) -> dict:
    """LIVE check of one connection (or of a repo already in the store): reachable, unique id,
    storage type, capacity. This is the "verify" step of the connect flow."""
    try:
        return repoconnect.probe(config_file=payload.config_file, repo=payload.repo)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


@router.post("/setup/connect")
def connect(payload: ConnectRequest) -> dict:
    """Connect a repository of any type kopia supports, and start tracking it."""
    try:
        record = repoconnect.connect(
            payload.provider, payload.fields, payload.password, payload.name,
            payload.short_name, payload.counts_as_backup, payload.allow_duplicate,
            payload.config_location, payload.config_folder)
    except repoconnect.DuplicateRepo as exc:
        return _duplicate(exc)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc
    return {"ok": True, "repo": repos.detail(record)}


@router.post("/setup/create")
def create(payload: CreateRequest) -> dict:
    """Create a NEW repository in the chosen storage and start tracking it.

    The storage location must be empty (kopia refuses otherwise) and the password becomes the
    repository's own password - it is handed to kopia, never stored here."""
    try:
        record = repoconnect.create(
            payload.provider, payload.fields, payload.password, payload.name,
            payload.short_name, payload.counts_as_backup, payload.options,
            payload.config_location, payload.config_folder)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc
    return {"ok": True, "repo": repos.detail(record)}


@router.post("/setup/attach")
def attach(payload: AttachRequest) -> dict:
    """Track an existing kopia connection (KopiaUI's list, the CLI default, a copied config)."""
    try:
        record = repoconnect.attach(payload.config_file, payload.name, payload.short_name,
                                    payload.counts_as_backup, payload.allow_duplicate)
    except repoconnect.DuplicateRepo as exc:
        return _duplicate(exc)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc
    return {"ok": True, "repo": repos.detail(record)}


@router.post("/setup/repos/{repo}/unlock")
def unlock(repo: str, payload: UnlockRequest) -> dict:
    """Give kopia the repository password once so it can remember it, for a connection whose
    password is not remembered (KopiaUI's "remember" unchecked, or a config copied here)."""
    try:
        return repoconnect.unlock(repo, payload.password)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


class ReconnectRequest(BaseModel):
    password: str = ""
    keys: dict[str, str] = {}     # storage keys KopiaLens never kept, entered again


@router.post("/setup/repos/{repo}/reconnect")
def reconnect(repo: str, payload: ReconnectRequest) -> dict:
    """Rebuild a repository's missing connection file in place (same id and index)."""
    try:
        return repoconnect.reconnect(repo, payload.password, payload.keys)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


@router.get("/setup/repos/{repo}/take-connection")
def take_connection_preview(repo: str) -> dict:
    """Moving or copying the connection file into KopiaLens's folder: from, to, who uses it."""
    try:
        return repoconnect.take_connection_preview(repo)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


class TakeConnectionRequest(BaseModel):
    mode: str = "move"            # move | copy
    password: str = ""


@router.post("/setup/repos/{repo}/take-connection")
def take_connection(repo: str, payload: TakeConnectionRequest) -> dict:
    try:
        return repoconnect.take_connection(repo, payload.mode, payload.password)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


class ReadonlyRequest(BaseModel):
    on: bool
    shared_ok: bool = False   # the user confirmed that another app's connection turns read-only too


@router.post("/setup/repos/{repo}/readonly")
def set_readonly(repo: str, payload: ReadonlyRequest):
    """kopia's own read-only connection on or off. 409 names the other app when the connection
    file is shared, so the screen can ask first."""
    try:
        return repoconnect.set_readonly(repo, payload.on, payload.shared_ok)
    except repoconnect.SharedConnection as exc:
        return JSONResponse({"detail": str(exc), "shared_with": exc.app}, status_code=409)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


@router.get("/setup/repos/{repo}/summary")
def repo_summary(repo: str) -> dict:
    """Sources, computers and snapshots in a repository (one live listing) - the "Connected" card."""
    try:
        return repoconnect.summary(repo)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


@router.patch("/setup/repos/{repo}")
def edit(repo: str, payload: RepoEditRequest) -> dict:
    """Rename a repo, set its short label, or take it out of the Excluded browser's coverage."""
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "counts_as_backup" in fields:
        fields["counts_as_backup"] = bool(fields["counts_as_backup"])
    try:
        record = repos.update_record(repo, fields)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc
    return {"ok": True, "repo": repos.detail(record)}


@router.get("/setup/repos/{repo}/remove-preview")
def remove_preview(repo: str) -> dict:
    """Exactly what removing this repo deletes (KopiaLens data only - the kopia repository is
    never touched), with sizes."""
    try:
        return repoconnect.remove_preview(repo)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


@router.delete("/setup/repos/{repo}")
def remove(repo: str, confirm: bool = False, delete_config: bool = False) -> dict:
    """Remove a repo: the record and every app-owned file derived from it. `confirm=true` is
    required - this is the second half of the preview-then-execute pair."""
    if not confirm:
        raise HTTPException(status_code=400, detail="Removing a repo needs confirm=true "
                                                    "(see remove-preview first).")
    try:
        return repoconnect.remove(repo, delete_config=delete_config)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc


@router.get("/setup/sample")
def sample_state() -> dict:
    """Progress of adding the sample data: idle, building, done (with the new repo) or error."""
    from ..core import sampledata
    return sampledata.install_state()


@router.post("/setup/sample")
def sample_add() -> dict:
    """Make the sample data on this computer (well under a minute here) and add it as a demo
    repository. It runs in the background; poll ``GET /setup/sample``."""
    from ..core import sampledata
    if any(r.get("sample") for r in repos.all_records()):
        raise HTTPException(status_code=400, detail="The sample data is already added.")
    return sampledata.start_install()


class BackblazeAddRequest(BaseModel):
    index: bool = True


@router.get("/setup/backblaze")
def backblaze_state() -> dict:
    """Backblaze on this computer: found, where, and whether KopiaLens already reads it."""
    from config import DEMO_INSTANCE
    from ..backblaze import paths as bz_paths
    path = bz_paths.bzdata_root()
    state = "missing" if DEMO_INSTANCE else bz_paths.access(path)
    found = state == "ok"
    return {"found": found, "path": path if found else "",
            "blocked": state == "blocked", "blocked_path": path if state == "blocked" else "",
            "host_mac": sys.platform == "darwin",
            "added": repos.exists(repos.BACKBLAZE_ID),
            "drives": len(bz_paths.volumes()) if found else 0,
            "default": bz_paths.default_bzdata() or "",
            "computer": bz_paths.computer_display() if found else "",
            "this_computer": bz_paths.is_this_computer() if found else True,
            "platform": bz_paths.platform() if found else ""}


class BackblazeFolderRequest(BaseModel):
    path: str


@router.post("/setup/backblaze/folder")
def backblaze_folder(payload: BackblazeFolderRequest) -> dict:
    """Read Backblaze from a folder: another computer's shared bzdata, or a non-default install."""
    from ..backblaze import paths as bz_paths
    from .backblaze import apply_folder_setting
    path = payload.path.strip().strip('"')
    if not path:
        raise HTTPException(status_code=400, detail="Enter the Backblaze data folder.")
    state = bz_paths.access(path)
    if state != "ok":
        sub = os.path.join(path, "bzdata")
        sub_state = bz_paths.access(sub)
        if sub_state == "ok":
            path = sub
        elif "blocked" in (state, sub_state):
            mac = f" On a Mac, also {bz_paths.BLOCKED_HELP_MAC}." if sys.platform == "darwin" else ""
            raise HTTPException(status_code=400, detail=(
                f"KopiaLens can't open that folder. Check that this account can read it.{mac}"))
        else:
            raise HTTPException(status_code=400, detail=(
                "That folder isn't Backblaze's data folder. Choose the bzdata folder, "
                "the one that holds bzfilelists."))

    def _apply(settings: dict) -> None:
        settings["backblaze"] = {**settings.get("backblaze", {}), "bzdata_path": path}

    apply_folder_setting(mutate_settings(_apply))
    return backblaze_state()


@router.post("/setup/backblaze")
def backblaze_add(payload: BackblazeAddRequest) -> dict:
    """Start reading Backblaze. ``index`` builds its index right away (setup starts it itself)."""
    state = backblaze_state()
    if not state["found"] and not state["added"]:
        raise HTTPException(status_code=400, detail="Backblaze wasn't found on this computer.")
    try:
        rec = repos.add_backblaze()
    except OSError as exc:
        raise _bad(ValueError(f"Couldn't save the repository list: {exc}")) from exc
    started = False
    if payload.index:
        from ..core import reindexq
        reindexq.request(repos.BACKBLAZE_ID, wait=0, reason="first")
        started = bool(reindexq.run_now(repos.BACKBLAZE_ID).get("started"))
    return {"ok": True, "repo": repos.detail(rec), "drives": state["drives"], "started": started}


class IndexRebuildRequest(BaseModel):
    repo: str
    confirm: bool = False


@router.get("/setup/index/rebuild-preview")
def index_rebuild_preview(repo: str) -> dict:
    """What an explicit "rebuild this index with this version" would do, and its cost.

    The case it exists for: an index written by a NEWER KopiaLens (someone went back to an older
    version). Nothing can migrate it forward safely, and updating again is not always an option -
    so the app offers to set the old generation aside (never delete it) and rebuild from kopia."""
    from ..core import nightly
    from ..index import upgrade as index_upgrade

    if not repos.exists(repo):
        raise _bad(ValueError(f"unknown repo: {repo}"))
    status = index_upgrade.index_status(repo)
    started = nightly.is_running(repo)
    return {
        "repo": repo, "state": status["state"], "found": status["found"],
        "expected": status["expected"],
        "mode": (index_upgrade.migration_mode(status["found"])
                 if status["found"] is not None else ""),
        "can_rebuild": status["state"] in ("newer", "rebuild", "migratable") and not started,
        "blocked": ("this repo is being indexed right now - the rebuild starts after that pass"
                    if started else ""),
        "cost": ("one full index pass: 10-40 min per repo on a large one, and the sticky "
                 "first-seen dates are lost (they come from snapshots that have since expired)"),
        "keeps": "the old generation is moved aside, never deleted",
    }


@router.post("/setup/index/rebuild")
def index_rebuild(payload: IndexRebuildRequest) -> dict:
    """Rebuild one repo's index with THIS version: set the current generation aside and start an
    index pass now. `confirm=true` is required - the old generation is kept, but the rebuild is the
    expensive path (see rebuild-preview)."""
    import time

    from ..core import nightly
    from ..index import store as index_store
    from ..index import upgrade as index_upgrade

    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Rebuilding an index needs confirm=true "
                                                    "(see index/rebuild-preview first).")
    if not repos.exists(payload.repo):
        raise _bad(ValueError(f"unknown repo: {payload.repo}"))
    if nightly.is_running(payload.repo):
        raise _bad(ValueError(f"repo {payload.repo} is being indexed right now - try again after "
                              f"that pass"))
    status = index_upgrade.index_status(payload.repo)
    if status["state"] in ("current", "absent"):
        raise _bad(ValueError(f"repo {payload.repo} has no index to rebuild"
                              + (" yet" if status["state"] == "absent" else "")))
    closed = index_store.close_repo(payload.repo)
    root = index_store.repo_dir(payload.repo)
    archived = ""
    if os.path.isdir(root):
        archived = f"{root}.v{status['found']}-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.rename(root, archived)
        except OSError as exc:
            raise _bad(ValueError(f"could not set the old generation aside ({exc}) - close other "
                                  f"KopiaLens tabs and try again")) from exc
    out = index_upgrade.request_rebuild(payload.repo, trigger="explicit-rebuild")
    return {"ok": True, "repo": payload.repo, "archived_to": archived,
            "started": out["started"], "queued": out["queued"], "generations": closed,
            "note": "the old generation is kept beside the data folder; the pass rebuilds from kopia"}
