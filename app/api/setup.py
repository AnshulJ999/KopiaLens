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
    """Which kopia program KopiaLens will run, where it was found, and the version (display only)."""
    return kopiabin.info(with_candidates=True)


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
    except ValueError as exc:
        raise _bad(exc) from exc


@router.post("/setup/connect")
def connect(payload: ConnectRequest) -> dict:
    """Connect a repository of any type kopia supports, and start tracking it."""
    try:
        record = repoconnect.connect(
            payload.provider, payload.fields, payload.password, payload.name,
            payload.short_name, payload.counts_as_backup)
    except ValueError as exc:
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
    except ValueError as exc:
        raise _bad(exc) from exc
    return {"ok": True, "repo": repos.detail(record)}


@router.post("/setup/attach")
def attach(payload: AttachRequest) -> dict:
    """Track an existing kopia connection (KopiaUI's list, the CLI default, a copied config)."""
    try:
        record = repoconnect.attach(payload.config_file, payload.name, payload.short_name,
                                    payload.counts_as_backup)
    except ValueError as exc:
        raise _bad(exc) from exc
    return {"ok": True, "repo": repos.detail(record)}


@router.post("/setup/repos/{repo}/unlock")
def unlock(repo: str, payload: UnlockRequest) -> dict:
    """Give kopia the repository password once so it can remember it, for a connection whose
    password is not remembered (KopiaUI's "remember" unchecked, or a config copied here)."""
    try:
        return repoconnect.unlock(repo, payload.password)
    except ValueError as exc:
        raise _bad(exc) from exc


@router.patch("/setup/repos/{repo}")
def edit(repo: str, payload: RepoEditRequest) -> dict:
    """Rename a repo, set its short label, or take it out of the Excluded browser's coverage."""
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "counts_as_backup" in fields:
        fields["counts_as_backup"] = bool(fields["counts_as_backup"])
    try:
        record = repos.update_record(repo, fields)
    except ValueError as exc:
        raise _bad(exc) from exc
    return {"ok": True, "repo": repos.detail(record)}


@router.get("/setup/repos/{repo}/remove-preview")
def remove_preview(repo: str) -> dict:
    """Exactly what removing this repo deletes (KopiaLens data only - the kopia repository is
    never touched), with sizes."""
    try:
        return repoconnect.remove_preview(repo)
    except ValueError as exc:
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
    except ValueError as exc:
        raise _bad(exc) from exc
