"""Validated Kopia object-tree walking logic."""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from .cache import BATCH, decode_json, encode_json, get_children
from .kopia import kopia
from .paths import WIN_SEP

Visitor = Callable[[str, dict], None]


def expand_indirect(repo: str, db: sqlite3.Connection, indirect_objs: set[str]) -> set[str]:
    parts: set[str] = set()
    misses: list[str] = []
    cur = db.cursor()
    index_ids = {obj: obj[1:] for obj in indirect_objs}
    for obj, index_cid in index_ids.items():
        parts.add(index_cid)
        row = cur.execute("SELECT parts FROM indirect WHERE oid=?", (obj,)).fetchone()
        if row:
            parts.update(decode_json(row[0]))
        else:
            misses.append(obj)

    missing_index_ids = [index_ids[obj] for obj in misses]
    for i in range(0, len(missing_index_ids), BATCH):
        group_objs = misses[i : i + BATCH]
        res = kopia(repo, ["content", "show"] + missing_index_ids[i : i + BATCH])
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        for obj, line in zip(group_objs, lines):
            chunk: list[str] = []
            try:
                for entry in json.loads(line).get("entries") or []:
                    if entry.get("o"):
                        chunk.append(entry["o"])
            except json.JSONDecodeError:
                pass
            parts.update(chunk)
            db.execute("INSERT OR REPLACE INTO indirect VALUES (?,?)", (obj, encode_json(chunk)))
    db.commit()
    return parts


def walk(repo: str, roots: list[str], db: sqlite3.Connection, visitor: Visitor | None = None,
         sep: str = WIN_SEP) -> tuple[set[str], dict]:
    """Return all content IDs referenced by roots.

    The visitor hook receives every directory entry with its parent path; the
    DuckDB file index uses this without changing the physical-size math.

    ``sep`` is the SOURCE's path separator (see core.paths) - never the host's.
    """

    seen: set[str] = set()
    content_ids: set[str] = set(roots)
    indirect: set[str] = set()
    files = 0
    track_paths = visitor is not None
    frontier: list = [(root, "") for root in roots] if track_paths else list(roots)

    while frontier:
        if track_paths:
            batch: list[str] = []
            pathmap: dict[str, str] = {}
            for oid, path in frontier:
                if oid in seen:
                    continue
                seen.add(oid)
                batch.append(oid)
                pathmap[oid] = path
        else:
            batch = [oid for oid in frontier if oid not in seen]
            for oid in batch:
                seen.add(oid)
            pathmap = {}

        frontier = []
        for oid, entries in get_children(repo, db, batch).items():
            base = pathmap.get(oid, "") if track_paths else ""
            for entry in entries:
                obj = entry.get("obj", "")
                entry_type = entry.get("type")
                name = entry.get("name", "")
                if visitor:
                    visitor(base, entry)
                if entry_type == "d":
                    if obj.startswith("I"):
                        indirect.add(obj)
                    else:
                        content_ids.add(obj)
                    if obj not in seen:
                        frontier.append((obj, base + sep + name) if track_paths else obj)
                elif entry_type == "f":
                    files += 1
                    if obj.startswith("I"):
                        indirect.add(obj)
                    else:
                        content_ids.add(obj)

    content_ids |= expand_indirect(repo, db, indirect)
    return content_ids, {"dirs": len(seen), "files": files, "indirect": len(indirect)}


def walk_new(repo: str, db: sqlite3.Connection, roots: list[str], seen_dirs: set[str]) -> set[str]:
    """Incremental walk used by churn and retention simulation."""

    found: set[str] = set(roots)
    indirect: set[str] = set()
    frontier = [root for root in roots if root not in seen_dirs]
    while frontier:
        batch: list[str] = []
        for oid in frontier:
            if oid in seen_dirs:
                continue
            seen_dirs.add(oid)
            batch.append(oid)
        frontier = []
        for _oid, entries in get_children(repo, db, batch).items():
            for entry in entries:
                obj = entry.get("obj", "")
                entry_type = entry.get("type")
                if entry_type == "d":
                    if obj.startswith("I"):
                        indirect.add(obj)
                    else:
                        found.add(obj)
                    if obj not in seen_dirs:
                        frontier.append(obj)
                elif entry_type == "f":
                    if obj.startswith("I"):
                        indirect.add(obj)
                    else:
                        found.add(obj)

    found |= expand_indirect(repo, db, indirect)
    return found


def walk_new_visit(repo: str, db: sqlite3.Connection, roots: list[str], seen_dirs: set[str],
                   visitor: Visitor, sep: str = WIN_SEP) -> None:
    """Delta walk for the file index: descend only directories whose OID is NEW
    (not in seen_dirs), and call visitor(parent_path, entry) for every FILE entry under
    them. Accumulating seen_dirs across a source's snapshots (newest-first) means each
    unique directory is emitted once -> the index stays ~one row per file-version, not
    files x snapshots. Indirect dirs are resolved by get_children (kopia show)."""

    frontier: list = [(root, "") for root in roots if root not in seen_dirs]
    while frontier:
        batch: list[str] = []
        pathmap: dict[str, str] = {}
        for oid, path in frontier:
            if oid in seen_dirs:
                continue
            seen_dirs.add(oid)
            batch.append(oid)
            pathmap[oid] = path
        frontier = []
        for oid, entries in get_children(repo, db, batch).items():
            base = pathmap.get(oid, "")
            for entry in entries:
                etype = entry.get("type")
                if etype == "d":
                    obj = entry.get("obj", "")
                    if obj and obj not in seen_dirs:
                        frontier.append((obj, base + sep + entry.get("name", "")))
                elif etype == "f":
                    visitor(base, entry)
