"""Write modes: how much KopiaLens may change in a kopia repository.

Read-only (no repository writes; browse, search, mount, download), Restores allowed (+ restore
to disk), Non-destructive (+ snapshots, pins, policies, maintenance, descriptions; no manual
deletes), Full Access. One app-wide mode plus an optional per-repository override. Enforced on
the server: every write route and job kind names the mode it needs (``ROUTES``, ``JOBS``), and
``api.writegate`` refuses a request the repository's mode does not allow.

Going stricter is always free; raising a mode to Full Access asks for the KopiaLens password when
an account exists.
"""

from __future__ import annotations

from ..settings import load_settings, mutate_settings
from .applog import get_logger

log = get_logger("writemode")

MODES = ("read_only", "restores", "non_destructive", "full")
LABELS = {"read_only": "Read-only", "restores": "Restores allowed",
          "non_destructive": "Non-destructive", "full": "Full Access"}
DEFAULT = "full"

# Write routes (method, path) -> the mode they need; None = no repository write (KopiaLens's own
# settings, previews, reads, mounts and downloads). A callable takes (body, query) for routes
# whose need depends on the request. The whole-app guard test requires every write route here.
_R, _N, _F = "restores", "non_destructive", "full"


def _job_need(body: dict, _query: dict) -> str | None:
    kind = body.get("kind") or ""
    params = body.get("params") or {}
    if params.get("dry"):
        return None
    if kind == "policy_write" and params.get("then"):
        return _F  # exclude-and-delete removes the excluded files from the snapshots
    return JOBS.get(kind, _F)


JOBS: dict[str, str | None] = {
    "remove_files": _F, "prune_versions": _F, "snapshot_delete": _F, "source_delete": _F,
    "maintenance": _N, "policy_write": _N, "snapshot_create": _N, "snapshot_pin": _N,
    "move_history": _N, "restore": _R,
    "snapshot_estimate": None, "snapshot_verify": None, "blob_stats": None,
    "encryption_level": None,  # KopiaLens's own files, never the repository
}

ROUTES: dict[tuple[str, str], object] = {
    ("POST", "/api/jobs/start"): _job_need,
    ("POST", "/api/actions/snapshot-delete"): _F,
    ("POST", "/api/actions/remove-files"): _F,
    ("POST", "/api/actions/remove-bat"): _F,
    ("POST", "/api/engine/choice"): _N,
    ("POST", "/api/engine/snapshot"): _N,
    ("POST", "/api/engine/cancel"): _N,
    ("POST", "/api/engine/snapshot-description"): _N,
    ("POST", "/api/engine/identity"): _N,
    ("POST", "/api/tasks/cancel"): _N,
    ("POST", "/api/snapshots/pin"): _N,
    ("POST", "/api/stats/description"): _N,
    ("POST", "/api/client/scripts"): _N,
    ("POST", "/api/policies/save"): _N,
    ("POST", "/api/policies/restore"): _N,
    ("POST", "/api/notify/profile"): _N,
    ("POST", "/api/notify/profile/delete"): _N,
}

# Write routes that never change a repository (listed so the guard test knows they were sorted).
NO_REPO_WRITE = {
    "/api/access", "/api/account/create", "/api/account/password", "/api/account/username",
    "/api/account/sign-out-all", "/api/account/enabled", "/api/account/encryption/split",
    "/api/account/encryption/join",
    "/api/actions/mount", "/api/actions/mount-open", "/api/actions/mount-browse", "/api/actions/unmount",
    "/api/actions/unmount-all", "/api/actions/remove-files-dry-run", "/api/actions/remove-plan",
    "/api/actions/remove-detail", "/api/actions/remove-list", "/api/actions/remove-plan-dry-run",
    "/api/actions/snapshot-delete-dry-run", "/api/actions/browse-folder", "/api/actions/folder-reclaim",
    "/api/actions/batch-detail", "/api/actions/compact",
    "/api/app/restart", "/api/index/pause", "/api/index/resume", "/api/config-backups",
    "/api/config-backups/{backup_id}/restore", "/api/config-backups/import",
    "/api/config-backups/import/apply", "/api/config-backups/notices/{notice_id}/dismiss",
    "/api/index/pending/run", "/api/index/build", "/api/settings", "/api/settings/table",
    "/api/index/backups/copy", "/api/index/notices/{notice_id}/dismiss",
    "/api/delete/plan", "/api/delete/stats",
    "/api/engine/pause", "/api/engine/resume", "/api/engine/source-pause", "/api/engine/source-resume",
    "/api/help/check-updates", "/api/help/install-update", "/api/folder/detail", "/api/jobs/stop",
    "/api/notify/profile/test", "/api/notify/webhook/test",
    "/api/policies/resolve", "/api/policies/multi-plan", "/api/policies/preview", "/api/policies/price",
    "/api/policies/exclusion-targets", "/api/policies/exclusion-variants", "/api/policies/diff",
    "/api/policies/backup",
    "/api/prune/plan", "/api/prune/price",
    "/api/restore/plan", "/api/restore/check", "/api/restore/check/{check_id}/stop", "/api/restore/open",
    "/api/search/folder-versions", "/api/search/folder-values", "/api/search/versions",
    "/api/simulate", "/api/simulate/all", "/api/snapshots/analyze", "/api/snapshots/analyze-all",
    "/api/snapshots/set-reclaim", "/api/tree/holders",
    "/api/write-mode", "/api/auth/login", "/api/auth/logout", "/api/auth/recover", "/api/index/stop",
    "/api/tokens", "/api/tokens/{token_id}", "/api/approvals/{approval_id}/approve",
    "/api/approvals/{approval_id}/deny",
}
# Prefixes whose write routes are KopiaLens's own (licence, onboarding, setup, encryption, shell,
# Backblaze): connecting, creating and removing repositories is allowed in every mode.
NO_REPO_WRITE_PREFIXES = ("/api/licence", "/api/onboarding/", "/api/setup/", "/api/encryption/",
                          "/api/shell/", "/api/backblaze/")


def _valid(mode) -> str | None:
    return mode if mode in MODES else None


def global_mode() -> str:
    return _valid((load_settings().get("kopialens") or {}).get("write_mode")) or DEFAULT


def repo_override(repo: str) -> str | None:
    import config
    return _valid(config.repo_record(str(repo)).get("write_mode"))


def mode(repo: str | None) -> str:
    """The mode that applies to ``repo``: its override, else the app-wide mode."""
    if repo is None:
        return global_mode()
    return repo_override(repo) or global_mode()


def allows(repo: str | None, need: str | None) -> bool:
    return need is None or MODES.index(mode(repo)) >= MODES.index(need)


def reason(repo: str | None, need: str | None) -> str:
    """Why an action is refused, for the screens and the 403."""
    return (f"This repository is in {LABELS[mode(repo)]} mode, which doesn't allow this. "
            f"It needs {LABELS[need]}. Change it in Settings > Permissions.")


def backups_allowed(repo: str) -> bool:
    """Read-only and Restores allowed stop KopiaLens's own backup server for the repository."""
    return allows(repo, "non_destructive")


def route_need(method: str, path: str, body: dict, query: dict) -> str | None:
    need = ROUTES.get((method, path))
    return need(body, query) if callable(need) else need


class PasswordNeeded(Exception):
    pass


def _password_ok(password: str | None) -> bool:
    """The KopiaLens password: the signed-in account's, or (sign-in off) any account's. A request
    running on an approval whose approver entered the password counts as having it."""
    from . import approvals
    if approvals.password_confirmed():
        return True
    from . import auth, users
    accounts = users.accounts()
    if not accounts:
        return True
    if not password:
        return False
    actor = users.find(auth.current_actor())
    pool = [actor] if actor else accounts
    return any(auth.verify_password(password, u.get("password_hash") or "") for u in pool)


def set_mode(repo: str | None, new: str | None, password: str | None = None) -> dict:
    """Set the app-wide mode (``repo`` None) or a repository's override (``new`` None = follow the
    app-wide mode). Raising the effective mode to Full Access needs the KopiaLens password."""
    if repo is None and new not in MODES:
        raise ValueError("unknown write mode")
    if repo is not None and new is not None and new not in MODES:
        raise ValueError("unknown write mode")
    before = mode(repo)
    after = (new if repo is None else (new or global_mode()))
    if after == "full" and before != "full" and not _password_ok(password):
        raise PasswordNeeded("Enter your KopiaLens password to allow Full Access.")
    if repo is None:
        mutate_settings(lambda s: s.setdefault("kopialens", {}).__setitem__("write_mode", new))
    else:
        from . import repos
        repos.set_write_mode(repo, new)
    log.warning("write mode %s: %s -> %s", f"repo={repo}" if repo else "app-wide", before, mode(repo))
    try:
        from . import audit
        audit.audit("executed", "write-mode", repo=repo or "", summary={"from": before, "to": mode(repo)})
    except Exception:  # noqa: BLE001 - the audit line is best-effort
        pass
    try:
        from . import engine
        engine.wake()
    except Exception:  # noqa: BLE001 - the next reconcile applies it anyway
        pass
    return state()


def state() -> dict:
    """Everything the screens need: modes, labels, the app-wide mode, each repo's override and
    effective mode, and whether raising to Full Access asks for a password."""
    import config
    from . import users
    repos_out = {}
    for rid in config.REPOS:
        if config.repo_kind(rid) != "kopia":
            continue
        repos_out[str(rid)] = {"override": repo_override(rid), "mode": mode(rid)}
    return {"modes": list(MODES), "labels": LABELS, "global": global_mode(), "repos": repos_out,
            "password": users.has_accounts(), "jobs": JOBS,
            "routes": {f"{m} {p}": (n if not callable(n) else "by kind") for (m, p), n in ROUTES.items()}}
