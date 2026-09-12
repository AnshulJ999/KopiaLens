"""Policy backups (/ design section 4): KopiaLens-owned exports of every defined
policy, under ``data/policy-backups/repo<N>/`` - travels with the install, Docker users already
mount ``data/`` persistently.

The file content is the RAW ``kopia policy export`` JSON (one object {target: policy}, global
included) so every backup is directly importable by ``kopia policy import`` - no custom format
to outlive the app. Metadata (when, why) lives in the FILENAME: ``<YYYYMMDD_HHMMSS>_<trigger>.json``.

FAIL-CLOSED: every policy write produces and VERIFIES a pre-write backup first (re-read and parse
the exported file - a truncated export is worse than none). If the backup cannot be produced, the
write is refused. Restore also backs up the CURRENT policies first (pre-restore).

Retention: per trigger class - nightly/manual keep 15, pre-write/pre-restore keep 25 (the
safety nets of last resort are kept longer). Never pruned below a floor of 1 per class.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile

from config import DATA_DIR

from . import audit
from .applog import get_logger
from .kopia import kopia, policy_import_execute
from .policy import invalidate_after_write

log = get_logger("policybackup")

_NAME_RE = re.compile(r"^\d{8}_\d{6}_(pre-write|pre-restore|nightly|manual)\.json$")
_KEEP = {"pre-write": 25, "pre-restore": 25, "nightly": 15, "manual": 15}


class BackupError(RuntimeError):
    """A backup could not be produced/verified. Fail-closed: the write it guards is refused."""


def backup_dir(repo: str) -> str:
    return os.path.join(DATA_DIR, "policy-backups", f"repo{repo}")


def create_backup(repo: str, trigger: str) -> dict:
    """Export + verify + install one backup. Returns {"name", "path", "verified": True}."""
    if trigger not in _KEEP:
        raise ValueError(f"unknown backup trigger {trigger!r}")
    return create_backup_from_text(repo, _read_export(repo), trigger)


def nightly_backup(repo: str) -> dict:
    """The worker's nightly pass: export, verify, and keep only when the content changed vs the
    newest nightly backup (polices rarely change; identical files would churn the retention)."""
    text = _read_export(repo)
    for entry in list_backups(repo):
        if entry["trigger"] == "nightly":
            with open(entry["path"], encoding="utf-8") as fh:
                if _hash(fh.read()) == _hash(text):
                    return {"name": entry["name"], "changed": False, "verified": True}
            break
    return {**create_backup_from_text(repo, text, "nightly"), "changed": True}


def _read_export(repo: str) -> str:
    d = backup_dir(repo)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=d)
    os.close(fd)
    try:
        # kopia() takes ONLY the subcommand args - it adds the binary + repo args itself.
        # Passing a full command here double-prefixes it and the RO gate refuses it, which
        # fail-closed turned into "every policy save fails".
        res = kopia(repo, ["policy", "export", "--to-file", tmp, "--overwrite"])
        if res.returncode != 0:
            raise BackupError(f"kopia policy export failed rc={res.returncode}: "
                              f"{(res.stderr or '').strip()[:300]}")
        try:
            with open(tmp, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("export is not a JSON object")
        except Exception as exc:  # noqa: BLE001
            raise BackupError(f"export failed verification: {exc}") from exc
        with open(tmp, encoding="utf-8") as fh:
            return fh.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_backup_from_text(repo: str, text: str, trigger: str) -> dict:
    """Install an already-verified export text as a new backup (the nightly path)."""
    d = backup_dir(repo)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{stamp}_{trigger}.json"
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    size = os.path.getsize(path)
    log.info("policy backup repo=%s trigger=%s file=%s (%d bytes)", repo, trigger, name, size)
    audit.audit("executed", "policy-backup", repo=repo, summary={"trigger": trigger, "file": name}, rc=0)
    _prune(repo)
    return {"name": name, "path": path, "verified": True, "size": size}


def list_backups(repo: str) -> list[dict]:
    """All backups for a repo, newest first: {name, ts, trigger, size, path}."""
    d = backup_dir(repo)
    out: list[dict] = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        m = _NAME_RE.match(name)
        if not m:
            continue
        path = os.path.join(d, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        out.append({"name": name, "ts": f"{name[:4]}-{name[4:6]}-{name[6:8]} "
                                        f"{name[9:11]}:{name[11:13]}:{name[13:15]}",
                    "trigger": m.group(1), "size": size, "path": path})
    out.sort(key=lambda e: e["name"], reverse=True)
    return out


def read_backup(repo: str, name: str) -> str:
    """The raw export text of one backup. Raises FileNotFoundError for unknown names."""
    path = _validated_path(repo, name)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _validated_path(repo: str, name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise FileNotFoundError(f"unknown backup {name!r}")
    path = os.path.join(backup_dir(repo), name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"unknown backup {name!r}")
    return path


def _prune(repo: str) -> None:
    """Keep the newest _KEEP[trigger] backups per trigger class; never prune a class below 1."""
    by_class: dict[str, list[dict]] = {}
    for entry in list_backups(repo):
        by_class.setdefault(entry["trigger"], []).append(entry)
    for trigger, entries in by_class.items():
        cap = _KEEP.get(trigger, 15)
        for entry in entries[cap:]:
            try:
                os.remove(entry["path"])
                log.info("pruned old policy backup %s", entry["name"])
            except OSError as exc:  # noqa: BLE001 - a stuck file must not fail the backup
                log.warning("could not prune %s: %s", entry["name"], exc)


def restore_backup(repo: str, name: str, mirror: bool = False) -> dict:
    """Verify the backup, back up the CURRENT policies (pre-restore, fail-closed), then import.
    ``mirror=True`` is the full mirror-restore (--delete-other-policies). Returns the import
    result plus the pre-restore backup info."""
    text = read_backup(repo, name)  # raises for unknown names
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("backup is not a JSON object")
    except Exception as exc:  # noqa: BLE001 - refuse to import a file we cannot parse
        raise BackupError(f"backup {name} failed verification: {exc}") from exc

    pre = create_backup(repo, "pre-restore")  # fail-closed: no backup, no restore
    res = policy_import_execute(repo, _validated_path(repo, name), mirror=mirror)
    invalidate_after_write(repo, targets=None)
    log.warning("policy restore repo=%s file=%s mirror=%s rc=%d", repo, name, mirror,
                res.get("returncode"))
    audit.audit("executed", "policy-restore", repo=repo,
                summary={"file": name, "mirror": bool(mirror),
                         "pre_restore_backup": pre["name"]},
                rc=res.get("returncode"))
    return {**res, "pre_restore_backup": pre}
