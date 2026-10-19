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
import threading
import time

import config

from .applog import get_logger
from .kopia import kopia

log = get_logger("repos")

_ID_LOCK = threading.Lock()   # id allocation + the record-store read-modify-write

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


def reserve_id() -> str:
    """Allocate an id AND persist the high-water mark, BEFORE anything slow happens.

    The connect flow holds the id across a kopia call that takes seconds to minutes; allocating and
    only persisting at `add_record()` let two overlapping connects take the SAME id and the same
    managed config path, so the loser wrote the winner's file and one record could describe the
    other's storage. A failed connect hands its number back (``release_id``)."""
    with _ID_LOCK:
        rid = next_id()
        _advance_id_mark(rid)
        config.save_records(config.RECORDS, strict=True)
    return rid


def release_id(rid: str) -> None:
    """Give back a reserved id whose connect failed, when it is still the newest one reserved and
    nothing was recorded under it. Overlapping connects keep theirs: never two on one number."""
    with _ID_LOCK:
        mark = str(config.RECORDS.get("next_id") or "")
        if not str(rid).isdigit() or mark != str(int(rid) + 1) or exists(rid):
            return
        config.RECORDS["next_id"] = int(rid)
        config.save_records(config.RECORDS, strict=True)


def all_records() -> list[dict]:
    return config.RECORDS.get("repos", [])


def exists(repo: str) -> bool:
    return any(str(r.get("id")) == str(repo) for r in all_records())


def config_path_for(repo: str) -> str | None:
    """The kopia config file this repo uses, or None for kopia's own default connection."""
    rec = config.repo_record(repo)
    return rec.get("config_file") or None


_CONFIG_FACTS: dict[str, tuple[int, dict]] = {}


def connection_file(repo: str) -> str:
    """The kopia config file a repo uses, with kopia's default connection filled in."""
    path = config_path_for(repo)
    if not path:
        from .repoconnect import kopia_config_dir   # lazy: repoconnect imports this module
        path = os.path.join(kopia_config_dir(), "repository.config")
    return path


def _config_facts(repo: str) -> dict:
    """``server`` (an ``apiServer`` connection) and ``readonly`` (kopia's own read-only
    connection), read from the config file itself and cached per file mtime."""
    if config.repo_kind(repo) != "kopia" or config.is_demo(repo) or not exists(repo):
        return {}      # a demo repo has no kopia connection (its missing config is not the default)
    path = connection_file(repo)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    hit = _CONFIG_FACTS.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
        facts = {"server": bool(data.get("apiServer")), "readonly": bool(data.get("readonly")),
                 "server_url": str((data.get("apiServer") or {}).get("url") or ""),
                 "host": str(data.get("hostname") or ""), "user": str(data.get("username") or ""),
                 "actions": bool(data.get("enableActions"))}
    except (OSError, ValueError):
        facts = {}
    _CONFIG_FACTS[path] = (mtime, facts)
    return facts


def client_facts(repo: str) -> dict | None:
    """This computer's kopia identity for the repo, from the connection file (kopia keeps the client
    options at its top level). None when the file does not name both."""
    facts = _config_facts(repo)
    if not facts.get("user") or not facts.get("host"):
        return None
    return {"user": facts["user"], "host": facts["host"], "enable_actions": bool(facts.get("actions"))}


def via_server(repo: str) -> bool:
    """True when the repo is reached through a kopia repository server (an ``apiServer`` config).

    kopia refuses the direct-only commands the index is built from over such a connection
    (content/blob/index reads), so the repo is browse-only."""
    return bool(_config_facts(repo).get("server"))


def kopia_readonly(repo: str) -> bool:
    """True when kopia itself holds this connection read-only (``repository connect --readonly``
    or ``set-client --read-only``): every repository write fails in kopia."""
    return bool(_config_facts(repo).get("readonly"))


def client_host(repo: str) -> str:
    """This computer's name in the repo's snapshots, lowercased: the connection file's hostname,
    else kopia's default (the computer name)."""
    import socket
    return (str(_config_facts(repo).get("host") or "") or socket.gethostname()).lower()


# Storage-config keys that must NEVER leave the machine. A kopia config file holds credentials in
# the clear (the `kopia:"sensitive"` tag exists only so kopia's own `repository status` can scrub
# them for display), and KopiaLens both persists that block in repos.json and serves it to the
# browser - so it is scrubbed ON READ, everywhere. Sources: repo/blob/*/*_options.go tags, plus two
# untagged rclone fields that carry a remote's credentials.
SENSITIVE_CONFIG_KEYS = frozenset({
    "storagekey", "sastoken", "clientsecret", "clientcertificate",   # azureBlob
    "key",                                                            # b2
    "credentials",                                                    # gcs + gdrive service account
    "secretaccesskey", "sessiontoken",                                # s3
    "password", "keydata",                                            # sftp + webdav
    "embeddedconfig", "rcloneenv",                                    # rclone (untagged in kopia)
})
# Fallback for a provider whose shape we have not seen: a key that LOOKS like a credential is
# scrubbed rather than trusted - with an explicit exception list for fields whose NAME suggests a
# secret but whose VALUE is a path or an identifier (kopia's `credentialsFile` is the path TO the
# service-account JSON; `accessKeyID`/`keyID` are public identifiers).
NOT_SENSITIVE_KEYS = frozenset({"credentialsfile", "serviceaccountcredentialsfile", "keyfile",
                                "accesskeyid", "keyid"})
_SENSITIVE_KEY_RE = re.compile(r"(secret|password|token|credential|keydata|^key$|storagekey|sas)",
                               re.I)
SCRUBBED = "***"


def is_sensitive_key(key: str) -> bool:
    text = str(key or "")
    if text.lower() in NOT_SENSITIVE_KEYS:
        return False
    return text.lower() in SENSITIVE_CONFIG_KEYS or bool(_SENSITIVE_KEY_RE.search(text))


def _scrub_list_item(value):
    if isinstance(value, dict):
        return scrub_storage_config(value)
    text = str(value)
    if "=" in text and is_sensitive_key(text.split("=", 1)[0]):
        return SCRUBBED           # e.g. rclone's ["SOME_TOKEN=..."] environment entries
    return value


def scrub_storage_config(config) -> dict:
    """A copy of a storage config with every credential replaced by "***".

    Recursive: nested blocks and a service-account JSON embedded as a string are covered, while the
    display fields the screens need (bucket, host, path, url, prefix, endpoint, container, remote
    path, region, port) pass through untouched."""
    if not isinstance(config, dict):
        return {}
    out: dict = {}
    for key, value in config.items():
        if is_sensitive_key(str(key)):
            out[key] = SCRUBBED
        elif isinstance(value, dict):
            out[key] = scrub_storage_config(value)
        elif isinstance(value, list):
            out[key] = [_scrub_list_item(v) for v in value]
        else:
            out[key] = value
    return out


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
    the polled `/api/repos` must not launch kopia ().

    The config is scrubbed AGAIN on the way out: a record written before the scrub existed (or by
    an older version) must not leak a credential into a response even once."""
    rec = config.repo_record(repo)
    storage = rec.get("storage") or {}
    if not storage.get("type") and via_server(repo):
        # kopia's status names no storage for a server connection: the connection file has its address
        storage = {"type": "server", "config": {"url": _config_facts(repo).get("server_url", "")}}
    return {
        "type": storage.get("type") or "",
        "kind": storage_kind(storage.get("type")),
        "config": scrub_storage_config(storage.get("config") or {}),
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
        "via_server": via_server(rid),
        "kopia_readonly": kopia_readonly(rid),
        "demo": bool(record.get("demo")),
        "sample": bool(record.get("sample")),
    }


def read_status(repo: str, timeout: float | None = None) -> dict:
    """LIVE `repository status --json` for a repo. Returns {} when it cannot be read (offline
    storage, a server connection, no kopia) - never raises, because every caller of this is either
    a background capture or an explicit probe that reports the failure itself."""
    try:
        res = kopia(repo, ["repository", "status", "--json"], timeout=timeout)
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
        # kopia's own status output is scrubbed, but KopiaLens must not depend on that: scrub
        # before it can reach repos.json.
        sconfig = scrub_storage_config(storage.get("config") or {})
        if (rec.get("storage") or {}).get("type") != stype or \
                (rec.get("storage") or {}).get("config") != sconfig:
            rec["storage"] = {"type": stype, "config": sconfig}
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


def scrub_stored_records() -> list[str]:
    """One-time cleanup: rewrite any record whose stored storage config still holds a credential
    (a record written before the scrub existed, or by an older version). Returns the repo ids that
    were cleaned."""
    cleaned = []
    for rec in config.RECORDS.get("repos", []):
        storage = rec.get("storage") or {}
        raw = storage.get("config")
        if not isinstance(raw, dict) or not raw:
            continue
        scrubbed = scrub_storage_config(raw)
        if scrubbed != raw:
            rec["storage"] = {**storage, "config": scrubbed}
            cleaned.append(str(rec.get("id")))
    if cleaned:
        config.save_records(config.RECORDS)
        log.warning("scrubbed credentials from %d stored repo record(s): %s", len(cleaned),
                    ", ".join(cleaned))
    return cleaned


def ensure_unique_ids() -> None:
    """Startup background pass: capture the live status of every kopia repo into its record - the
    unique id (as before) AND the storage type/capacity (repo-type detection), from the SAME spawn
    so detection costs nothing extra. Also scrubs any legacy record that still carries a
    credential."""
    try:
        scrub_stored_records()
    except Exception as exc:  # noqa: BLE001 - a cleanup must not stop the capture
        log.warning("stored-record scrub failed: %s", exc)
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


def default_short_name(rid: str, kind: str) -> str:
    """The chip label a new record gets when none is given; stored once, never changed afterwards."""
    return "BZ" if kind == "backblaze" else f"Repo {rid}"


def add_record(rec: dict) -> dict:
    """Append one validated record and persist. `id` may be blank (the allocator fills it; the
    connect flow reserves one up front with `reserve_id()`)."""
    rec = dict(rec)
    rid = str(rec.get("id") or "").strip() or reserve_id()
    if not valid_id(rid):
        raise ValueError(f"invalid repo id: {rid!r}")
    with _ID_LOCK:
        if any(str(r.get("id")) == rid for r in all_records()):
            raise ValueError(f"repo id {rid} is already in use")
        rec["id"] = rid
        rec.setdefault("kind", "kopia")
        rec.setdefault("name", rec.get("name") or f"Repo {rid}")
        rec["short_name"] = str(rec.get("short_name") or "").strip() or default_short_name(rid, rec["kind"])
        rec.setdefault("counts_as_backup", True)
        rec.setdefault("source", "connected")
        rec.setdefault("created", time.time())
        config.RECORDS.setdefault("repos", []).append(rec)
        _advance_id_mark(rid)
        try:
            config.save_records(config.RECORDS, strict=True)
        except OSError:
            config.RECORDS["repos"].remove(rec)
            raise
    config.reload_repos()
    log.info("repo=%s added (%s)", rid, rec.get("name"))
    return rec


BACKBLAZE_ID = "bz"


def add_backblaze() -> dict:
    """The Backblaze record (fixed id, read-only, no kopia connection). Adding it again is a no-op."""
    with _ID_LOCK:
        rec = next((r for r in all_records() if str(r.get("id")) == BACKBLAZE_ID), None)
        if rec is not None:
            return rec
        rec = {"id": BACKBLAZE_ID, "kind": "backblaze", "name": "Backblaze Personal Backup",
               "short_name": default_short_name(BACKBLAZE_ID, "backblaze"), "counts_as_backup": True, "source": "detected",
               "created": time.time()}
        config.RECORDS.setdefault("repos", []).append(rec)
        try:
            config.save_records(config.RECORDS, strict=True)
        except OSError:
            config.RECORDS["repos"].remove(rec)
            raise
    config.reload_repos()
    log.info("repo=bz added (Backblaze)")
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
    config.save_records(config.RECORDS, strict=True)
    config.reload_repos()
    return rec


ENGINE_FIELDS = frozenset({"engine", "engine_since", "engine_moved_from", "config_file"})


def set_engine_fields(repo: str, fields: dict) -> dict:
    """Who runs backups for the repo (core/engine.set_choice). An empty value removes the key."""
    bad = set(fields) - ENGINE_FIELDS
    if bad:
        raise ValueError(f"not an engine field: {', '.join(sorted(bad))}")
    rec = next((r for r in all_records() if str(r.get("id")) == str(repo)), None)
    if rec is None:
        raise ValueError(f"unknown repo: {repo}")
    for key, value in fields.items():
        if value in (None, "", 0):
            rec.pop(key, None)
        else:
            rec[key] = value
    config.save_records(config.RECORDS, strict=True)
    config.reload_repos()
    return rec


def set_write_mode(repo: str, mode: str | None) -> dict:
    """The repository's write-mode override (core/writemode); None follows the app-wide mode."""
    rec = next((r for r in all_records() if str(r.get("id")) == str(repo)), None)
    if rec is None:
        raise ValueError(f"unknown repo: {repo}")
    if mode:
        rec["write_mode"] = mode
    else:
        rec.pop("write_mode", None)
    config.save_records(config.RECORDS, strict=True)
    config.reload_repos()
    return rec


def remove_record(repo: str) -> dict:
    """Drop the record from the store. The caller deletes the app's derived files (data_paths);
    the kopia repository itself is never touched."""
    _require_free(repo)
    rec = next((r for r in all_records() if str(r.get("id")) == str(repo)), None)
    if rec is None:
        raise ValueError(f"unknown repo: {repo}")
    before = config.RECORDS.get("repos", [])
    config.RECORDS["repos"] = [r for r in before if str(r.get("id")) != str(repo)]
    try:
        config.save_records(config.RECORDS, strict=True)
    except OSError:
        config.RECORDS["repos"] = before
        raise
    config.reload_repos()
    log.warning("repo=%s removed from the record store", repo)
    return rec


def _drop_key(path: str, key: str, *, nested: tuple[str, ...] = ()) -> bool:
    """Remove one repo's entry from a small JSON state file. `nested` names the sub-dicts to look
    the key up in (bg_worker_state keeps one per concern). Unreadable/absent file = nothing to do."""
    from . import filecrypto        # these state files are encrypted at the Full level
    data = filecrypto.read_json(path)
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
    if not filecrypto.write_json(path, data):
        log.warning("could not clean %s for repo=%s", os.path.basename(path), key)
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
