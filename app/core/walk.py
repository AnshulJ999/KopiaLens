"""Validated Kopia object-tree walking logic."""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from .applog import get_logger
from .cache import BATCH, decode_json, encode_json, get_children
from .kopia import kopia
from .paths import WIN_SEP

log = get_logger("walk")

Visitor = Callable[[str, dict], None]


def expand_indirect(repo: str, db: sqlite3.Connection, indirect_objs: set[str],
                    fetch: bool = True, missing: list[str] | None = None) -> set[str]:
    """Chunks behind indirect objects. ``fetch=False`` never calls kopia: objects not in the walk
    cache are appended to ``missing`` and contribute only their index content."""
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
    if not fetch:
        if missing is not None:
            missing.extend(misses)
        return parts

    for i in range(0, len(misses), BATCH):
        group_objs = misses[i : i + BATCH]
        res = kopia(repo, ["content", "show"] + [index_ids[o] for o in group_objs])
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        # `content show` ABORTS the whole batch at the first unreadable content (verified against
        # kopia 2026-09-06: it prints the answers up to that point, then exits non-zero). So the
        # answers it DID print stay in request order and pairing by position is sound - but the
        # objects after it got no answer at all and must not be recorded as empty.
        if len(lines) < len(group_objs):
            log.warning("repo=%s content show answered %d of %d objects (rc=%d): %s - the rest are "
                        "left unresolved, not cached as empty", repo, len(lines), len(group_objs),
                        res.returncode, (res.stderr or "").strip()[:200])
        for obj, line in zip(group_objs, lines):
            try:
                entries = json.loads(line).get("entries") or []
            except json.JSONDecodeError:
                # Never cache an unreadable answer: the indirect rows are immutable, so an empty
                # chunk list written here would undercount this object's physical size forever.
                log.warning("repo=%s content show: unparsable answer for %s", repo, obj)
                continue
            chunk = [e["o"] for e in entries if e.get("o")]
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

    Dedup key: kopia stores identical directories ONCE, so one directory object can sit at
    several paths. With a visitor the key is therefore (object, path), so every location is
    visited; without one (content unions) it stays the object alone, which is both correct
    and cheaper. ``stats["dirs"]`` counts what was visited under that key.
    """

    track_paths = visitor is not None
    seen: set = set()
    content_ids: set[str] = set(roots)
    indirect: set[str] = set()
    files = 0
    frontier: list[tuple[str, str]] = [(root, "") for root in roots]

    while frontier:
        batch_paths: dict[str, list[str]] = {}
        for oid, path in frontier:
            key = (oid, path) if track_paths else oid
            if key in seen:
                continue
            seen.add(key)
            batch_paths.setdefault(oid, []).append(path)

        frontier = []
        for oid, entries in get_children(repo, db, list(batch_paths)).items():
            here = batch_paths[oid]
            for entry in entries:
                obj = entry.get("obj", "")
                entry_type = entry.get("type")
                # Content accounting is per OBJECT (a set), so it runs once however many paths
                # this directory was reached at.
                if entry_type == "d" or entry_type == "f":
                    if obj.startswith("I"):
                        indirect.add(obj)
                    else:
                        content_ids.add(obj)
                    if entry_type == "f":
                        files += 1
                # Path work is per LOCATION.
                for base in here:
                    if visitor:
                        visitor(base, entry)
                    if entry_type == "d" and obj:
                        child = base + sep + entry.get("name", "") if track_paths else ""
                        if ((obj, child) if track_paths else obj) not in seen:
                            frontier.append((obj, child))

    content_ids |= expand_indirect(repo, db, indirect)
    return content_ids, {"dirs": len(seen), "files": files, "indirect": len(indirect)}


def walk_new(repo: str, db: sqlite3.Connection, roots: list[str], seen_dirs: set[str]) -> set[str]:
    """Incremental walk used by churn and retention simulation.

    Content-only: the dedup key is the object alone, which is exactly right for a content
    union and must NOT be made path-aware (see ``walk``)."""

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


def walk_new_visit(repo: str, db: sqlite3.Connection, roots: list[str],
                   seen_dirs: set[tuple[str, str]], visitor: Visitor,
                   sep: str = WIN_SEP) -> None:
    """Delta walk for the file index: descend only directory LOCATIONS that are new
    (not in seen_dirs) and call visitor(parent_path, entry) for every FILE entry under them.
    Accumulating seen_dirs across a source's snapshots (newest-first) means each unique
    directory location is emitted once -> the index stays ~one row per file-version, not
    files x snapshots. Indirect dirs are resolved by get_children (kopia show).

    ``seen_dirs`` holds (object, path) pairs, NOT bare object ids. Kopia stores identical
    directories once, so an object-only key drops every location after the first: a directory
    present at two paths indexes only one of them, and a RENAMED directory (identical contents
    -> identical object) never indexes its earlier path at all. Keying on the pair keeps the
    delta optimization intact - an unchanged directory at an unchanged path still collapses to
    one visit across snapshots - while giving each distinct location its own rows.
    """

    frontier: list[tuple[str, str]] = [(root, "") for root in roots if (root, "") not in seen_dirs]
    while frontier:
        batch_paths: dict[str, list[str]] = {}
        for oid, path in frontier:
            if (oid, path) in seen_dirs:
                continue
            seen_dirs.add((oid, path))
            batch_paths.setdefault(oid, []).append(path)
        frontier = []
        # One fetch per unique object; the entries are then fanned out over every path that
        # object was reached at in this round.
        for oid, entries in get_children(repo, db, list(batch_paths)).items():
            for base in batch_paths[oid]:
                for entry in entries:
                    etype = entry.get("type")
                    if etype == "d":
                        obj = entry.get("obj", "")
                        if obj:
                            child = base + sep + entry.get("name", "")
                            if (obj, child) not in seen_dirs:
                                frontier.append((obj, child))
                    elif etype == "f":
                        visitor(base, entry)
