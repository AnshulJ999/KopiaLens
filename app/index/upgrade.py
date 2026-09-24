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
    touched by an older app.

    The message says which way the mismatch runs AND what actually happens next - claiming "migrates
    in place" when the fallback is a 10-40 minute rebuild (or the reverse) is a lie the UI repeats
    to the user."""

    def __init__(self, repo: str, found: int, expected: int) -> None:
        self.repo = repo
        self.found = int(found)
        self.expected = int(expected)
        self.mode = migration_mode(self.found)      # "newer" | "migrate" | "rebuild"
        if self.mode == "newer":
            detail = (f"Repo {repo}: this index was written by a newer KopiaLens (format "
                      f"{self.found}; this app reads {self.expected}). Update KopiaLens, or rebuild "
                      f"this repo's index with this version (10-40 min; the sticky first-seen dates "
                      f"are lost).")
        elif self.mode == "migrate":
            detail = (f"Repo {repo}: this index is in an older format ({self.found}; this app reads "
                      f"{self.expected}). It is upgraded in place and becomes readable when that "
                      f"finishes.")
        else:
            detail = (f"Repo {repo}: this index is in an older format ({self.found}; this app reads "
                      f"{self.expected}) and has no in-place upgrade. An index pass rebuilds it "
                      f"(10-40 min; the sticky first-seen dates are lost).")
        super().__init__(detail)


def migration_mode(found: int) -> str:
    """What happens to a generation written at `found`: "newer" (refused), "migrate" (an in-place
    step exists) or "rebuild" (no step, or a step that needs kopia data)."""
    found = int(found)
    if found > MANIFEST_VERSION:
        return "newer"
    step = _step_for(found)
    if step is not None and not step["needs_rebuild"]:
        return "migrate"
    return "rebuild"


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


_REBUILD_QUEUE: dict[str, float] = {}     # repo -> when the rebuild was asked for
_QUEUE_LOCK = threading.Lock()


def _start_pass(repo: str, trigger: str) -> dict:
    """Ask the scheduler for an immediate index pass, the same way the nightly does."""
    try:
        from ..core import nightly
        return nightly.start_index_pass_async(repo, trigger=trigger)
    except Exception as exc:  # noqa: BLE001 - a scheduler problem must not break the caller
        log.error("could not start an index pass for repo=%s (%s): %s", repo, trigger, exc)
        return {"started": False, "reason": str(exc)}


def request_rebuild(repo: str, trigger: str = "upgrade") -> dict:
    """An unreadable old-format index must not wait for the nightly: start a pass NOW, and queue it
    if another pass is running (one heavy pass at a time is the scheduler's rule, not ours to
    break). Returns {"started": bool, "queued": bool}."""
    started = _start_pass(repo, trigger)
    if started.get("started"):
        return {"started": True, "queued": False}
    with _QUEUE_LOCK:
        _REBUILD_QUEUE.setdefault(repo, time.time())
    log.warning("repo=%s needs an index rebuild; a pass is busy (%s) so it is queued",
                repo, started.get("reason"))
    return {"started": False, "queued": True}


def queued_rebuilds() -> list[str]:
    with _QUEUE_LOCK:
        return sorted(_REBUILD_QUEUE)


def drain_queue() -> list[str]:
    """Called by the scheduler on its tick: start ONE queued rebuild as soon as the slot is free.
    Anything still queued stays queued (the next tick tries again)."""
    with _QUEUE_LOCK:
        pending = sorted(_REBUILD_QUEUE, key=lambda r: _REBUILD_QUEUE[r])
    started = []
    for repo in pending[:1]:
        out = _start_pass(repo, "upgrade")
        if not out.get("started"):
            break                     # the slot is still busy - leave it queued
        with _QUEUE_LOCK:
            _REBUILD_QUEUE.pop(repo, None)
        started.append(repo)
        log.warning("queued index rebuild for repo=%s started", repo)
    return started


def migrate_all_repos(repos: list) -> dict:
    """Startup pass: migrate every repo whose generation is in an OLDER format than this app.

    A generation from a NEWER app is refused (never written). One with no migration registered - or
    whose migration failed - gets an index pass started IMMEDIATELY (queued if another pass is
    running): an index the user cannot read must not sit broken until the nightly.

    A repo whose worker is still running is skipped and retried next start: the app deliberately
    takes over live workers (), and rewriting files under one either fails (Windows) or
    publishes a manifest built from the pre-migration snapshot (POSIX). Returns a report."""
    from ..core import nightly
    from . import publish
    from .store import repo_dir

    migrated, refused, rebuild, skipped = [], [], [], []
    for repo in repos:
        status = index_status(repo)
        if status["state"] in ("current", "absent"):
            continue
        if status["state"] == "newer":
            refused.append(repo)
            log.error("repo=%s index format %s is NEWER than this app (%s) - refusing to touch it; "
                      "update KopiaLens or use the explicit rebuild", repo, status["found"],
                      MANIFEST_VERSION)
            continue
        if status["state"] == "rebuild":
            rebuild.append(repo)
            log.warning("repo=%s index format %s has no in-place migration - starting an index pass "
                        "now (10-40 min; sticky first-seen dates are lost)", repo, status["found"])
            request_rebuild(repo)
            continue
        if nightly.is_running(repo):
            skipped.append(repo)
            log.warning("repo=%s has an older index format but its worker is running - the migration "
                        "waits for the next start", repo)
            continue
        import json
        root = repo_dir(repo)
        try:
            with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
                manifest = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            log.warning("repo=%s manifest unreadable for migration: %s", repo, exc)
            continue
        try:
            with publish.WriterLock(repo):     # never migrate under a worker that starts mid-way
                out = migrate(root, manifest)
                if out is not None:
                    publish.write_manifest(root, out)
        except RuntimeError as exc:            # another writer holds the repo right now
            skipped.append(repo)
            log.warning("repo=%s migration skipped: %s", repo, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - the migration itself already logged its cause
            rebuild.append(repo)
            log.error("repo=%s migration could not be applied (%s) - starting an index pass now",
                      repo, exc)
            request_rebuild(repo)
            continue
        if out is not None:
            migrated.append(repo)
        else:
            # `migrate()` returns None for a broken step, a step that needs kopia, or no step at
            # all - every one of them means the same thing here: rebuild now, do not wait.
            rebuild.append(repo)
            log.warning("repo=%s could not be migrated in place - starting an index pass now", repo)
            request_rebuild(repo)
    return {"migrated": migrated, "refused_newer": refused, "needs_rebuild": rebuild,
            "skipped_worker_running": skipped}
