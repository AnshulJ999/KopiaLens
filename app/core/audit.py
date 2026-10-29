"""Append-only audit trail: every mutation KopiaLens performs - or hands out - durably
recorded in ``data/audit.jsonl``.

The app log ROTATES (1 MB x 15), so it is not an audit trail. This file is never rotated and
never truncated, by design: a few lines per action, one JSON object per line, greppable forever.
Growth is tiny (kilobytes a year of normal use) and the Stats tab shows its size so it can never
surprise anyone; an archive/trim hatch is deliberately deferred until it would ever matter.

Best-effort by contract: a write failure logs a warning and NEVER raises into the caller - an
audit gap must not turn into a failed delete or a failed restore.

``actor`` is optional per record (decided 2026-09-03): cheap multi-user future-proofing. Nothing
populates it yet - there is no user system - but the field is part of the schema from the start
so records written today stay meaningful when a user identity exists to fill it.
"""

from __future__ import annotations

import datetime
import json
import os
import threading

from config import DATA_DIR

from . import filecrypto
from .applog import get_logger

log = get_logger("audit")

_PATH = os.path.join(DATA_DIR, "audit.jsonl")
_LOCK = threading.Lock()

# The actor for anything the app does on its own rather than because someone clicked. Auto-
# compaction is the only such action audited today; everything else is request-driven and gets
# auth.current_actor(). Two real values beat a column that always says the same thing.
SYSTEM = "system"


def _path() -> str:
    return _PATH


def audit(event: str, action: str, repo: str | None = None, summary: dict | None = None,
          rc: int | None = None, duration_s: float | None = None,
          log_file: str | None = None, actor: str | None = None) -> None:
    """Append ONE JSON line. Best-effort: never raises. ``actor`` defaults to the current identity
    (auth.current_actor) so every recorded action says who did it - the part that cannot be
    backfilled once real users exist."""
    if actor is None:
        try:
            from .auth import current_actor  # lazy: keeps this module import-light
            actor = current_actor()
        except Exception:  # noqa: BLE001 - an audit gap must not break the action
            actor = None
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": event,          # "executed" | "mount" | "unmount" | "generated"
        "action": action,        # e.g. "remove-files", "snapshot-delete", "restore"
        "repo": repo,
        "summary": summary or {},  # compact: counts, first ~5 ids/names, scope, flags
        "rc": rc,
        "duration_s": round(duration_s, 1) if duration_s is not None else None,
        "log_file": log_file,
        "actor": actor,
    }
    try:
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            ok = filecrypto.append_line(_path(), line)
        if not ok:
            log.warning("audit write failed (%s %s): %s", event, action, "not written")
    except Exception as exc:  # noqa: BLE001 - the audit trail must never break the action
        log.warning("audit write failed (%s %s): %s", event, action, exc)


# Automatic policy backups: not audited, the save or restore that made one names it.
AUTOMATIC_BACKUPS = ("nightly", "pre-write", "pre-restore")
# Snapshots someone started, previews and read-only checks: the Logs tab's Hide routine.
ROUTINE = frozenset({"snapshot-create", "remove-plan", "snapshot-estimate", "snapshot-verify", "blob-stats"})
_MAX_WINDOW = 100_000


def _retired(rec: dict) -> bool:
    """Kinds no longer written; their old rows are not shown."""
    action = rec.get("action")
    if action in ("engine-start", "engine-stop", "unmount"):
        return True
    return action == "policy-backup" and (rec.get("summary") or {}).get("trigger") in AUTOMATIC_BACKUPS


def routine(rec: dict) -> bool:
    return rec.get("action") in ROUTINE or (rec.get("summary") or {}).get("dry") is True


def read_view(limit: int = 200, hide_routine: bool = False) -> list[dict]:
    """The last ``limit`` rows the Logs tab shows, oldest first. Reads a growing tail window
    until enough rows pass the filter or the file runs out."""
    window = limit
    while True:
        tail = read_tail(window)
        rows = [r for r in tail if not _retired(r) and not (hide_routine and routine(r))]
        if len(rows) >= limit or len(tail) < window or window >= _MAX_WINDOW:
            return rows[-limit:]
        window *= 4


def size_bytes() -> int:
    try:
        return os.path.getsize(_path())
    except OSError:
        return 0


def read_tail(limit: int = 200) -> list[dict]:
    """The last ``limit`` records, oldest first. Reads a bounded tail window rather than the
    whole file - the file only ever grows."""
    try:
        with _LOCK:
            lines = filecrypto.tail_lines(_path(), limit)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001 - one damaged line must not hide the rest
            out.append({"ts": None, "event": "corrupt", "action": line[:80]})
    return out[-limit:]
