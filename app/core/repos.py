"""Repository record maintenance.

Records are anchored on Kopia's own repository Unique ID (`repository status` -> `uniqueIDHex`,
64-char hex): a config path can be repointed at a different repository, and the id is what notices.
Capture is lazy and best-effort - a missing id is normal for a newly added repo, never an error, and
a mismatch WARNS rather than blocking, because re-creating a repository is a legitimate thing to do.

This module also owns the RECORD RULES every caller must use:

* id allocation and validation - the id is embedded in on-disk names (`data/repo<id>/`,
  `cache/*_repo<id>.*`), so a user-chosen id would reach the filesystem
* `short_name` / `counts_as_backup` record fields
* the cached per-repo storage facts read from ``repository status --json``
* the list of app-owned files for one repo (the remove-preview)

The connect/attach flows themselves live in ``core/repoconnect.py``.
"""

from __future__ import annotations

import json
import os
import re
import time

import config

from .applog import get_logger
from .kopia import kopia

log = get_logger("repos")

ID_RE = re.compile(r"^[a-z0-9]{1,12}$")
RESERVED_IDS = frozenset({"bz"})     # the Backblaze pseudo-repo: never allocated to a kopia repo

# kopia storage types (repo/blob/*) grouped for the UI. rclone/webdav/sftp can front anything, so
# they read as "network" - only a type that is definitely remote storage is called "cloud".
LOCAL_STORAGE = frozenset({"filesystem"})
NETWORK_STORAGE = frozenset({"sftp", "webdav", "rclone"})
CLOUD_STORAGE = frozenset({"s3", "b2", "gcs", "azureBlob", "gdrive"})


def valid_id(rid: str) -> bool:
    """Path-safe and allocatable: lowercase alphanumeric, bounded length, not reserved."""
    return bool(ID_RE.match(rid or "")) and rid not in RESERVED_IDS


def next_id() -> str:
    """The next free numeric id: the store's persisted high-water mark (`next_id`), never lower
    than one past the highest id actually in use.

    NOT "the lowest free number": ids are embedded in on-disk names AND in shared state keyed by
    id alone (`bg_worker_state.json`, `repo_size_history.json`, `reindex_pending.json`), so
    reusing a removed repo's id would hand a brand-new repo the old one's index history. Ids are
    allocated once and never come back. A store written before this field existed just falls back
    to the highest id it holds."""
    highest = 0
    for rec in config.RECORDS.get("repos", []):
        text = str(rec.get("id") or "")
        if text.isdigit():
            highest = max(highest, int(text))
    mark = str(config.RECORDS.get("next_id") or "")
    stored = int(mark) if mark.isdigit() else 0
    return str(max(stored, highest + 1, 1))


def _advance_id_mark(rid: str) -> None:
    """Record that this id has been used, so removing the record cannot release it again."""
    if not str(rid).isdigit():
        return
    mark = str(config.RECORDS.get("next_id") or "")
    stored = int(mark) if mark.isdigit() else 0
    config.RECORDS["next_id"] = max(stored, int(rid) + 1)


def all_records() -> list[dict]:
    return config.RECORDS.get("repos", [])


def exists(repo: str) -> bool:
    return any(str(r.get("id")) == str(repo) for r in all_records())


def config_path_for(repo: str) -> str | None:
    """The kopia config file this repo uses, or None for kopia's own default connection."""
    rec = config.repo_record(repo)
    return rec.get("config_file") or None


def storage_kind(storage_type: str) -> str:
    t = str(storage_type or "")
    if t in LOCAL_STORAGE:
        return "local"
    if t in NETWORK_STORAGE:
        return "network"
    if t in CLOUD_STORAGE:
        return "cloud"
    if t == "server":
        return "server"
    return "unknown"


def storage_info(repo: str) -> dict:
    """The CACHED storage facts for a repo (type, kind, capacity, when captured). Never spawns -
    the polled `/api/repos` must not launch kopia ()."""
    rec = config.repo_record(repo)
    storage = rec.get("storage") or {}
    return {
        "type": storage.get("type") or "",
        "kind": storage_kind(storage.get("type")),
        "config": storage.get("config") or {},
        "capacity": rec.get("capacity") or {},
        "captured_at": rec.get("status_at") or 0,
    }


def detail(record: dict) -> dict:
    """One repo in the shape `/api/repos` serves (one place, so the API and the setup routes
    cannot drift apart)."""
    rid = str(record.get("id"))
    info = storage_info(rid)
    return {
        "id": rid, "name": record.get("name") or rid,
        "short_name": record.get("short_name") or "",
        "kind": record.get("kind", "kopia"),
        "config_file": record.get("config_file") or "",
        "source": record.get("source") or "connected",
        "counts_as_backup": record.get("counts_as_backup", True),
        "unique_id": record.get("unique_id") or "",
        "storage": info,
    }


def read_status(repo: str) -> dict:
    """LIVE `repository status --json` for a repo. Returns {} when it cannot be read (offline
    storage, a server connection, no kopia) - never raises, because every caller of this is either
    a background capture or an explicit probe that reports the failure itself."""
    try:
        res = kopia(repo, ["repository", "status", "--json"])
        if res.returncode != 0:
            log.warning("repo=%s repository status failed: %s", repo, (res.stderr or "").strip()[:300])
            return {}
        out = json.loads(res.stdout) if (res.stdout or "").strip() else {}
        return out if isinstance(out, dict) else {}
    except Exception as exc:  # noqa: BLE001 - status capture must never break a caller
        log.warning("repo=%s repository status read failed: %s", repo, exc)
        return {}


def capture_status(repo: str, status: dict | None = None) -> dict:
    """Merge a live status reading into the record (unique id, storage, capacity). An empty reading
    changes nothing. Callers that already have a status dict pass it in; otherwise it is read."""
    data = status if status is not None else read_status(repo)
    if not data:
        return {}
    rec = next((r for r in config.RECORDS.get("repos", []) if str(r.get("id")) == str(repo)), None)
    if rec is None:
        return {}
    changed = False
    uid = data.get("uniqueIDHex")
    if uid and rec.get("unique_id") != uid:
        if rec.get("unique_id"):
            log.warning("repo=%s unique id changed (%s -> %s) - this config now points at a "
                        "different repository; the existing index describes the old one",
                        repo, rec.get("unique_id"), uid)
        rec["unique_id"] = uid
        changed = True
    storage = data.get("storage") or {}
    if storage:
        stype = storage.get("type") or ""
        if (rec.get("storage") or {}).get("type") != stype or \
                (rec.get("storage") or {}).get("config") != (storage.get("config") or {}):
            rec["storage"] = {"type": stype, "config": storage.get("config") or {}}
            changed = True
    volume = data.get("volume") or {}
    if volume:
        cap = {"capacity": volume.get("capacity"), "available": volume.get("available")}
        if rec.get("capacity") != cap:
            rec["capacity"] = cap
            changed = True
    if changed:
        rec["status_at"] = time.time()
        config.save_records(config.RECORDS)
    return data


def unique_id(repo: str) -> str | None:
    """Live Unique ID for a kopia-backed repo, or None if it cannot be read (offline storage, a
    server connection - `repository status` fails over the server protocol)."""
    return (read_status(repo) or {}).get("uniqueIDHex") or None


def ensure_unique_ids() -> None:
    """Startup background pass: capture the live status of every kopia repo into its record - the
    unique id (as before) AND the storage type/capacity (repo-type detection), from the SAME spawn
    so detection costs nothing extra."""
    for rec in list(config.RECORDS.get("repos", [])):
        rid = str(rec.get("id", ""))
        if rid not in config.kopia_repos():
            continue
        capture_status(rid)


def _belongs_to(name: str, rid: str) -> bool:
    """True when an on-disk name is this repo's own file (`repo1`, `churn_repo1_30d_d4.parquet`).
    Anchored so repo 1 never matches repo 12's files."""
    return bool(re.search(rf"(?:^|_)repo{re.escape(rid)}(?:$|[._])", name)) or name in (
        f"repo{rid}", f"duck_tmp_repo{rid}")


def data_paths(repo: str) -> list[str]:
    """Every app-owned file/folder for one repo: what a remove would delete. Derived from the id
    rather than a hand-kept list, so a new per-repo cache is covered the day it is written."""
    rid = str(repo)
    out: list[str] = []

    def scan(base: str) -> None:
        if not base or not os.path.isdir(base):
            return
        for name in os.listdir(base):
            if _belongs_to(name, rid):
                out.append(os.path.join(base, name))

    for base in (config.DATA_DIR, config.CACHEDIR, os.path.join(config.DATA_DIR, "policy-backups"),
                 os.path.join(config.DATA_DIR, "runs")):
        scan(base)
    return sorted(set(out))


def _require_free(repo: str) -> None:
    """Refuse to touch the store while this repo is being written or read hard enough that
    deleting its files would be unsafe."""
    from . import nightly
    if nightly.is_running(repo):
        raise ValueError(f"repo {repo} is being indexed right now - wait for the pass to finish")


def add_record(rec: dict) -> dict:
    """Append one validated record and persist. `id` may be blank (the allocator fills it)."""
    rec = dict(rec)
    rid = str(rec.get("id") or "").strip() or next_id()
    if not valid_id(rid):
        raise ValueError(f"invalid repo id: {rid!r}")
    if any(str(r.get("id")) == rid for r in all_records()):
        raise ValueError(f"repo id {rid} is already in use")
    rec["id"] = rid
    rec.setdefault("kind", "kopia")
    rec.setdefault("name", rec.get("name") or f"Repo {rid}")
    rec.setdefault("short_name", "")
    rec.setdefault("counts_as_backup", True)
    rec.setdefault("source", "connected")
    rec.setdefault("created", time.time())
    config.RECORDS.setdefault("repos", []).append(rec)
    _advance_id_mark(rid)
    config.save_records(config.RECORDS)
    config.reload_repos()
    log.info("repo=%s added (%s)", rid, rec.get("name"))
    return rec


def update_record(repo: str, fields: dict) -> dict:
    """Edit the editable fields in place. Storage facts and the unique id are NOT editable here -
    they come from a live status reading (core/repoconnect.probe)."""
    rec = next((r for r in all_records() if str(r.get("id")) == str(repo)), None)
    if rec is None:
        raise ValueError(f"unknown repo: {repo}")
    for key in ("name", "short_name", "counts_as_backup"):
        if key in fields:
            rec[key] = fields[key]
    config.save_records(config.RECORDS)
    config.reload_repos()
    return rec


def remove_record(repo: str) -> dict:
    """Drop the record from the store. The caller deletes the app's derived files (data_paths);
    the kopia repository itself is never touched."""
    _require_free(repo)
    rec = next((r for r in all_records() if str(r.get("id")) == str(repo)), None)
    if rec is None:
        raise ValueError(f"unknown repo: {repo}")
    config.RECORDS["repos"] = [r for r in all_records() if str(r.get("id")) != str(repo)]
    config.save_records(config.RECORDS)
    config.reload_repos()
    log.warning("repo=%s removed from the record store", repo)
    return rec


def _drop_key(path: str, key: str, *, nested: tuple[str, ...] = ()) -> bool:
    """Remove one repo's entry from a small JSON state file. `nested` names the sub-dicts to look
    the key up in (bg_worker_state keeps one per concern). Unreadable/absent file = nothing to do."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    changed = False
    targets = [data] + [data.get(part) for part in nested]
    for target in targets:
        if isinstance(target, dict) and key in target:
            target.pop(key, None)
            changed = True
    if not changed:
        return False
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not clean %s for repo=%s: %s", os.path.basename(path), key, exc)
        return False
    return True


def forget_state(repo: str) -> list[str]:
    """Drop every record of this id from the shared, id-keyed state files (the index schedule,
    the size history, the queued index update). data_paths() covers files that carry the id in
    their NAME; these carry it as a KEY, and a stale entry would follow the id into a future repo."""
    rid = str(repo)
    cleaned: list[str] = []
    for name, nested in (("bg_worker_state.json", ("nightly", "attempts", "success", "errors")),
                         ("repo_size_history.json", ()),
                         ("reindex_pending.json", ())):
        path = os.path.join(config.DATA_DIR, name)
        if _drop_key(path, rid, nested=nested):
            cleaned.append(name)
    try:
        from . import reindexq
        reindexq.forget(rid)
    except Exception as exc:  # noqa: BLE001 - in-memory queue cleanup is best-effort
        log.warning("reindex queue cleanup for repo=%s failed: %s", rid, exc)
    return cleaned
