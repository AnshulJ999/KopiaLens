"""The kopia command behind a screen ("Show command", like KopiaUI's CLI equivalent)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

import config

from ..core.kopia import _quote_arg, _repo_args

router = APIRouter(tags=["cli"])

_COMMANDS = {
    "sources": [["snapshot", "list", "--all"]],
    "repository": [["repository", "status"], ["content", "stats"], ["maintenance", "info"]],
    "policies": [["policy", "list"]],
    "maintenance": [["maintenance", "info"]],
}


@router.get("/cli/equivalent")
def equivalent(repo: str, what: str, target: str = "") -> dict:
    if repo not in config.REPOS or config.repo_kind(repo) != "kopia":
        raise HTTPException(status_code=404, detail=f"unknown repository: {repo}")
    if what == "snapshots":
        subs = [["snapshot", "list", target] if target and target != "*ALL*" else ["snapshot", "list", "--all"]]
    elif what == "policy":
        subs = [["policy", "show", "--global"] if target in ("", "global") else ["policy", "show", target]]
    elif what in _COMMANDS:
        subs = _COMMANDS[what]
    else:
        raise HTTPException(status_code=400, detail=f"unknown view: {what}")
    # Shown to the user, never run: the plain program name reads like kopia's own docs.
    base = _repo_args(repo)
    return {"commands": ["kopia " + " ".join(_quote_arg(a) for a in base + sub) for sub in subs]}
