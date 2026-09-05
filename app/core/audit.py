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

from .applog import get_logger

log = get_logger("audit")

_PATH = os.path.join(DATA_DIR, "audit.jsonl")
_LOCK = threading.Lock()


def _path() -> str:
    return _PATH


def audit(event: str, action: str, repo: str | None = None, summary: dict | None = None,
          rc: int | None = None, duration_s: float | None = None,
          log_file: str | None = None, actor: str | None = None) -> None:
    """Append ONE JSON line. Best-effort: never raises."""
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
            os.makedirs(os.path.dirname(_path()), exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - the audit trail must never break the action
        log.warning("audit write failed (%s %s): %s", event, action, exc)


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
            size = os.path.getsize(_path())
            with open(_path(), "rb") as fh:
                if size > 262_144:           # 256 KB is far more than 200 lines
                    fh.seek(-262_144, os.SEEK_END)
                    fh.readline()            # discard the partial line the seek landed in
                tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001 - one damaged line must not hide the rest
            out.append({"ts": None, "event": "corrupt", "action": line[:80]})
    return out[-limit:]
