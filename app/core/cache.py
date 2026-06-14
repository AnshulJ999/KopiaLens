"""Shared sqlite cache for immutable Kopia directory metadata."""

from __future__ import annotations

import collections
import json
import os
import sqlite3

from config import CACHEDIR

from .kopia import kopia

BATCH = 800
DIR_MEMO_MAX = 400_000
DIR_MEMO: collections.OrderedDict[str, list[dict]] = collections.OrderedDict()


def cache_db(repo: str) -> sqlite3.Connection:
    os.makedirs(CACHEDIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(CACHEDIR, f"repo{repo}.db"), timeout=60)
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS dir_children (oid TEXT PRIMARY KEY, children TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS indirect (oid TEXT PRIMARY KEY, parts TEXT)")
    return db


def memo_put(oid: str, entries: list[dict]) -> None:
    DIR_MEMO[oid] = entries
    DIR_MEMO.move_to_end(oid)
    if len(DIR_MEMO) > DIR_MEMO_MAX:
        DIR_MEMO.popitem(last=False)


def get_children(repo: str, db: sqlite3.Connection, oids: list[str]) -> dict[str, list[dict]]:
    """Return directory entries, preserving the indirect-directory ``kopia show`` fix."""

    out: dict[str, list[dict]] = {}
    misses: list[str] = []
    for oid in oids:
        entries = DIR_MEMO.get(oid)
        if entries is not None:
            DIR_MEMO.move_to_end(oid)
            out[oid] = entries
            continue
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
            entries = json.loads(row)
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
                entries = json.loads(line).get("entries") or []
            except json.JSONDecodeError:
                entries = []
            out[oid] = entries
            memo_put(oid, entries)
            db.execute("INSERT OR REPLACE INTO dir_children VALUES (?,?)", (oid, json.dumps(entries)))
            wrote = True

    for oid in indirect_dirs:
        res = kopia(repo, ["show", oid])
        try:
            entries = json.loads(res.stdout).get("entries") or []
        except json.JSONDecodeError:
            entries = []
        out[oid] = entries
        memo_put(oid, entries)
        db.execute("INSERT OR REPLACE INTO dir_children VALUES (?,?)", (oid, json.dumps(entries)))
        wrote = True

    if wrote:
        db.commit()
    return out
