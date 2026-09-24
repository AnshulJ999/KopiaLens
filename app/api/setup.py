"""Setup endpoints: the kopia program, and creating/connecting/editing/removing repositories.

The screens for these live with onboarding () and the repo-management design session; this
router is the backend contract they sit on. Nothing here runs kopia on a polled request - a live
call only ever happens because the user asked for one (a probe, a create/connect, an unlock).
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import kopiabin, repoconnect, repos
from ..settings import mutate_settings

router = APIRouter(tags=["setup"])


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


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


class CreateRequest(ConnectRequest):
    # `kopia repository create` format options (unchangeable later; absent = kopia's defaults)
    options: dict = {}


class AttachRequest(BaseModel):
    config_file: str | None = None
    name: str = ""
    short_name: str = ""
    counts_as_backup: bool = True


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
    return {"providers": repoconnect.provider_list()}


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
            payload.short_name, payload.counts_as_backup)
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
            payload.short_name, payload.counts_as_backup, payload.options)
    except (ValueError, OSError) as exc:
        raise _bad(exc) from exc
    return {"ok": True, "repo": repos.detail(record)}


@router.post("/setup/attach")
def attach(payload: AttachRequest) -> dict:
    """Track an existing kopia connection (KopiaUI's list, the CLI default, a copied config)."""
    try:
        record = repoconnect.attach(payload.config_file, payload.name, payload.short_name,
                                    payload.counts_as_backup)
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
