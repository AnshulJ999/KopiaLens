"""Repository record maintenance.

Records are anchored on Kopia's own repository Unique ID (`repository status` -> `uniqueIDHex`,
64-char hex): a config path can be repointed at a different repository, and the id is what notices.
Capture is lazy and best-effort - a missing id is normal for a newly added repo, never an error, and
a mismatch WARNS rather than blocking, because re-creating a repository is a legitimate thing to do.
"""

from __future__ import annotations

import json

import config

from .applog import get_logger
from .kopia import kopia

log = get_logger("repos")


def unique_id(repo: str) -> str | None:
    """Live Unique ID for a kopia-backed repo, or None if it cannot be read (offline storage, a
    server connection - `repository status` fails over the server protocol)."""
    try:
        res = kopia(repo, ["repository", "status", "--json"])
        if res.returncode != 0:
            return None
        return (json.loads(res.stdout) or {}).get("uniqueIDHex") or None
    except Exception as exc:  # noqa: BLE001 - identity capture must never break anything
        log.warning("repo=%s unique id read failed: %s", repo, exc)
        return None


def ensure_unique_ids() -> None:
    """Fill in any record missing its Unique ID, and warn when a stored one no longer matches."""
    changed = False
    for rec in config.RECORDS.get("repos", []):
        rid = str(rec.get("id", ""))
        if rid not in config.kopia_repos():
            continue
        live = unique_id(rid)
        if not live:
            continue
        stored = rec.get("unique_id")
        if not stored:
            rec["unique_id"] = live
            changed = True
            log.info("repo=%s unique id recorded: %s", rid, live)
        elif stored != live:
            log.warning("repo=%s unique id changed (%s -> %s) - this config now points at a "
                        "different repository; the existing index describes the old one",
                        rid, stored, live)
    if changed:
        config.save_records(config.RECORDS)
