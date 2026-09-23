"""In-place index migration registry (, index level).

The manifest's ``schema_version`` used to be a rebuild trigger: a mismatch meant "walk kopia again"
- 10-40 minutes per repo, and it loses the sticky first-seen dates (they come from snapshots that
have since expired, which a rebuild can no longer see).

Rule: **the index migrates by default.** A format change that keeps the same
DATA is a step here: it rewrites the generation in place and returns the new manifest. Only a change
that genuinely needs new data from kopia declares ``needs_rebuild=True``, and the build path then
rebuilds - loudly, because that is the expensive case.

A step looks like:

    def _add_column(root: str, manifest: dict) -> dict | None:
        # rewrite the generation's files in place, atomically (see core.dataupgrade.atomic_bytes),
        # then return the manifest with schema_version = MANIFEST_VERSION and any artifact list
        # changes. Return None to say "cannot migrate, rebuild instead".
        ...

    register(from_version=3, fn=_add_column, name="add-foo-column", description="...")

The version this app writes lives here (one authority, imported by artifacts and store).
"""

from __future__ import annotations

import os
import threading
import time

from ..core.applog import get_logger

log = get_logger("indexupgrade")

# The generation format THIS app writes. Bump it in the release that changes the manifest or the
# artifact layout, together with a registered step that migrates the previous version.
MANIFEST_VERSION = 3

_LOCK = threading.Lock()
_MIGRATIONS: dict[int, dict] = {}


class IndexVersionMismatch(RuntimeError):
    """A generation written by a different KopiaLens than this one. Refusing to read it is the
    point: an old layout read by new code misreads silently, and a NEWER layout must never be
    touched by an older app."""

    def __init__(self, repo: str, found: int, expected: int) -> None:
        self.repo = repo
        self.found = int(found)
        self.expected = int(expected)
        if self.found > self.expected:
            detail = (f"Repo {repo}: this index was written by a newer KopiaLens "
                      f"(format {self.found}; this app reads {self.expected}). Update KopiaLens, "
                      f"or point it at a data folder that matches.")
        else:
            detail = (f"Repo {repo}: this index is in an older format ({self.found}; this app "
                      f"reads {self.expected}) and needs a one-time upgrade - the next index pass "
                      f"migrates it in place.")
        super().__init__(detail)


def register(from_version: int, fn, name: str, description: str = "",
             needs_rebuild: bool = False) -> None:
    """Register the migration for a generation written at `from_version`."""
    with _LOCK:
        _MIGRATIONS[int(from_version)] = {"fn": fn, "name": name, "description": description,
                                          "needs_rebuild": bool(needs_rebuild)}


def migrations() -> list[dict]:
    with _LOCK:
        return [{"from": v, "to": MANIFEST_VERSION, "name": s["name"],
                 "description": s["description"], "needs_rebuild": s["needs_rebuild"]}
                for v, s in sorted(_MIGRATIONS.items())]


def _step_for(found: int) -> dict | None:
    with _LOCK:
        return _MIGRATIONS.get(int(found))


def migrate(root: str, manifest: dict) -> dict | None:
    """Migrate one generation in place. Returns the migrated manifest, or None when there is no
    (or no safe) in-place path - the caller then rebuilds. Never raises."""
    found = int(manifest.get("schema_version") or 0)
    step = _step_for(found)
    if step is None:
        return None
    if step["needs_rebuild"]:
        log.warning("index format %s -> %s needs data from kopia (`%s`) - the generation will be "
                    "rebuilt", found, MANIFEST_VERSION, step["name"])
        return None
    t0 = time.time()
    try:
        out = step["fn"](root, manifest)
    except Exception as exc:  # noqa: BLE001 - a failed migration falls back to a rebuild
        log.error("index migration %r (format %s -> %s) failed on %s: %s", step["name"], found,
                  MANIFEST_VERSION, root, exc, exc_info=True)
        return None
    if not isinstance(out, dict) or int(out.get("schema_version") or 0) != MANIFEST_VERSION:
        log.error("index migration %r returned no usable manifest for %s - rebuilding", step["name"],
                  root)
        return None
    log.warning("index migrated in place: %s (format %s -> %s, %.2fs)", root, found,
                MANIFEST_VERSION, time.time() - t0)
    return out


def index_status(repo: str) -> dict:
    """One repo's index format vs this app's, without spawning anything."""
    from .store import manifest_path

    path = manifest_path(repo)
    try:
        import json
        with open(path, encoding="utf-8") as fh:
            found = int((json.load(fh) or {}).get("schema_version") or 0)
    except Exception:  # noqa: BLE001 - absent or unreadable manifest: nothing to report
        return {"repo": repo, "found": None, "expected": MANIFEST_VERSION, "state": "absent"}
    if found == MANIFEST_VERSION:
        state = "current"
    elif found > MANIFEST_VERSION:
        state = "newer"                # refuse: written by a newer KopiaLens
    elif _step_for(found) is not None:
        state = "migratable"
    else:
        state = "rebuild"
    return {"repo": repo, "found": found, "expected": MANIFEST_VERSION, "state": state}


def migrate_all_repos(repos: list) -> dict:
    """Startup pass: migrate every repo whose generation is in an OLDER format than this app.
    A generation from a NEWER app is refused (never written), and one with no migration registered
    is left for the next index pass to rebuild. Returns a small report; never raises."""
    from .store import repo_dir

    migrated, refused, rebuild = [], [], []
    for repo in repos:
        status = index_status(repo)
        if status["state"] == "current" or status["state"] == "absent":
            continue
        if status["state"] == "newer":
            refused.append(repo)
            log.error("repo=%s index format %s is NEWER than this app (%s) - refusing to touch it; "
                      "update KopiaLens", repo, status["found"], MANIFEST_VERSION)
            continue
        if status["state"] == "rebuild":
            rebuild.append(repo)
            log.warning("repo=%s index format %s has no in-place migration - the next index pass "
                        "rebuilds it (10-40 min; sticky first-seen dates are lost)", repo,
                        status["found"])
            continue
        import json
        root = repo_dir(repo)
        try:
            with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
                manifest = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            log.warning("repo=%s manifest unreadable for migration: %s", repo, exc)
            continue
        out = migrate(root, manifest)
        if out is not None:
            try:
                from . import publish
                publish.write_manifest(root, out)
                migrated.append(repo)
            except Exception as exc:  # noqa: BLE001 - the migration itself already succeeded on disk
                log.error("repo=%s migrated manifest could not be published: %s", repo, exc)
    return {"migrated": migrated, "refused_newer": refused, "needs_rebuild": rebuild}
