"""Shared sqlite cache for immutable Kopia directory metadata."""

from __future__ import annotations

import collections
import os
import sqlite3
import sys
import threading

from config import CACHEDIR

from .kopia import kopia

try:  # orjson is ~3x faster at parsing the children JSON (the warm-walk hot path)
    import orjson

    _loads = orjson.loads  # direct alias - avoids a wrapper call per dir (hot path)

    def _dumps(obj) -> str:
        return orjson.dumps(obj).decode()
except ImportError:  # pragma: no cover - orjson is a declared dependency
    import json

    _loads = json.loads

    def _dumps(obj) -> str:
        return json.dumps(obj)

BATCH = 800
# In-process LRU over directory entries, on TOP of the durable mmap'd SQLite dir cache. The SQLite
# cache is the source of truth; this memo only avoids SQLite round-trips + JSON re-parsing on the
# hot walk path, so a smaller budget just means a few more (fast) SQLite hits, never a kopia re-walk.
# Budgeted by BYTES, not item count - directory sizes here range from ~2 to 50,000+ children, so a
# fixed item cap is not a RAM budget (measured 200MB-7GB for the same 50k-item cap in production).
DIR_MEMO_MAX_BYTES = 1_500 * 1024 * 1024  # 1.5 GB default; overridden from settings at startup
DIR_MEMO: collections.OrderedDict[str, tuple[int, list[dict]]] = collections.OrderedDict()
_MEMO_BYTES = 0  # running total, kept in lockstep with DIR_MEMO under _MEMO_LOCK
_MEMO_LOCK = threading.Lock()  # guards the shared LRU across the request threadpool + build thread


def _entry_bytes(entries: list[dict]) -> int:
    """Shallow-ish size of one DIR_MEMO value (a list of child dicts): list + each dict + its
    str keys/values. Good enough for a RAM budget, not a precise heap walk."""
    total = sys.getsizeof(entries)
    for d in entries:
        total += sys.getsizeof(d)
        if isinstance(d, dict):
            for k, v in d.items():
                total += sys.getsizeof(k) + sys.getsizeof(v)
    return total


def memo_bytes() -> int:
    """Current exact DIR_MEMO footprint, for logging/diagnostics."""
    with _MEMO_LOCK:
        return _MEMO_BYTES


def cache_db(repo: str) -> sqlite3.Connection:
    os.makedirs(CACHEDIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(CACHEDIR, f"repo{repo}.db"), timeout=60)
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-262144")    # 256 MB page cache
    db.execute("PRAGMA mmap_size=2147483648")  # 2 GB memory-map cap (mapped pages count as RSS)
    db.execute("CREATE TABLE IF NOT EXISTS dir_children (oid TEXT PRIMARY KEY, children TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS indirect (oid TEXT PRIMARY KEY, parts TEXT)")
    return db


def memo_put(oid: str, entries: list[dict]) -> None:
    global _MEMO_BYTES
    size = _entry_bytes(entries)
    with _MEMO_LOCK:
        old = DIR_MEMO.get(oid)
        if old is not None:
            _MEMO_BYTES -= old[0]
        DIR_MEMO[oid] = (size, entries)
        DIR_MEMO.move_to_end(oid)
        _MEMO_BYTES += size
        while _MEMO_BYTES > DIR_MEMO_MAX_BYTES and DIR_MEMO:
            _, (evicted_size, _) = DIR_MEMO.popitem(last=False)
            _MEMO_BYTES -= evicted_size


def get_children(repo: str, db: sqlite3.Connection, oids: list[str]) -> dict[str, list[dict]]:
    """Return directory entries, preserving the indirect-directory ``kopia show`` fix."""

    out: dict[str, list[dict]] = {}
    misses: list[str] = []
    with _MEMO_LOCK:
        for oid in oids:
            cached = DIR_MEMO.get(oid)
            if cached is not None:
                DIR_MEMO.move_to_end(oid)
                out[oid] = cached[1]
            else:
                misses.append(oid)

    cur = db.cursor()
    still: list[str] = []
    for i in range(0, len(misses), 900):
        group = misses[i : i + 900]
        if not group:
            continue
        found = dict(
            cur.execute(
                "SELECT oid, children FROM dir_children WHERE oid IN (%s)" % ",".join("?" * len(group)),
                group,
            ).fetchall()
        )
        for oid in group:
            row = found.get(oid)
            if row is None:
                still.append(oid)
                continue
            entries = _loads(row)
            out[oid] = entries
            memo_put(oid, entries)

    wrote = False
    normal = [oid for oid in still if not oid.startswith("I")]
    indirect_dirs = [oid for oid in still if oid.startswith("I")]

    for i in range(0, len(normal), BATCH):
        group = normal[i : i + BATCH]
        res = kopia(repo, ["content", "show"] + group)
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        for oid, line in zip(group, lines):
            try:
                entries = _loads(line).get("entries") or []
            # ValueError, NOT json.JSONDecodeError: `json` is imported only in the orjson-missing
            # branch above, so with orjson installed - the normal case here - this handler raised
            # NameError on the first piece of malformed kopia output, turning a recoverable parse
            # failure into a crash. Both libraries' decode errors derive from ValueError.
            except ValueError:
                entries = []
            out[oid] = entries
            memo_put(oid, entries)
            db.execute("INSERT OR REPLACE INTO dir_children VALUES (?,?)", (oid, _dumps(entries)))
            wrote = True

    for oid in indirect_dirs:
        res = kopia(repo, ["show", oid])
        try:
            entries = _loads(res.stdout).get("entries") or []
        except ValueError:  # see the note above - `json` is undefined when orjson is installed
            entries = []
        out[oid] = entries
        memo_put(oid, entries)
        db.execute("INSERT OR REPLACE INTO dir_children VALUES (?,?)", (oid, _dumps(entries)))
        wrote = True

    if wrote:
        db.commit()
    return out
