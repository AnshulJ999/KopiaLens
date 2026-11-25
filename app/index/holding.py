"""Which current snapshots hold a path.

The index is a delta: a file-version row sits on the snapshot where it changed, so it cannot
answer "which snapshots contain this file today". This resolves the path inside every snapshot
kopia currently lists for the source, through the generation's tree - with the shared kopia
fallback for folders of a snapshot newer than the last sync (see `entries.children_of`).
"""

from __future__ import annotations

from ..core import paths as pathrules
from ..core.sources import sources_index
from . import entries


def _segments(source: str, full_path: str) -> list[str] | None:
    sep = pathrules.sep(source)
    root = pathrules.source_root(source).rstrip(sep)
    fold = not pathrules.case_sensitive(source)
    a, b = (full_path.lower(), root.lower()) if fold else (full_path, root)
    if not a.startswith(b):
        return None
    rel = full_path[len(root):].strip(sep)
    return [s for s in rel.split(sep) if s] if rel else []


def _find(entries: list[dict], name: str, fold: bool) -> dict | None:
    for e in entries:
        if e.get("name") == name:
            return e
    if fold:
        low = name.lower()
        for e in entries:
            if (e.get("name") or "").lower() == low:
                return e
    return None


def holding_snapshots(repo: str, source: str, full_paths: list[str],
                      snaps: list[dict] | None = None) -> dict[str, list[dict]]:
    """``{full_path: [{snapshot_id, start_time, obj, size, type, pinned, retention}]}``, newest
    snapshot first, for every snapshot of ``source`` whose tree contains the path. Directory
    lookups are batched per depth and shared across snapshots and paths."""
    if snaps is None:
        snaps = sources_index(repo).get(source, [])
    fold = not pathrules.case_sensitive(source)
    out: dict[str, list[dict]] = {p: [] for p in full_paths}
    segs = {p: _segments(source, p) for p in full_paths}
    live = {p: s for p, s in segs.items() if s}
    if not live or not snaps:
        return out
    # state per (path, snapshot): the directory oid reached so far
    cursor: dict[tuple[str, int], str] = {}
    for p in live:
        for i, snap in enumerate(snaps):
            obj = (snap.get("rootEntry") or {}).get("obj")
            if obj:
                cursor[(p, i)] = obj
    depth = 0
    while cursor:
        need = sorted({oid for oid in cursor.values()})
        children = entries.children_of(repo, need)
        nxt: dict[tuple[str, int], str] = {}
        for (p, i), oid in cursor.items():
            parts = live[p]
            entry = _find(children.get(oid, []), parts[depth], fold)
            if entry is None:
                continue
            if depth == len(parts) - 1:
                snap = snaps[i]
                summ = entry.get("summ") or {}
                out[p].append({
                    "snapshot_id": snap.get("id"),
                    "start_time": snap.get("startTime", ""),
                    "obj": entry.get("obj", ""),
                    "size": int(entry.get("size", 0) or summ.get("size", 0) or 0),
                    "files": int(summ.get("files", 0) or 0),
                    "mtime": entry.get("mtime", ""),
                    "type": entry.get("type", ""),
                    "pinned": bool(snap.get("pins")),
                    "retention": snap.get("retention") or snap.get("retentionReason") or [],
                })
            elif entry.get("type") == "d" and entry.get("obj"):
                nxt[(p, i)] = entry["obj"]
        cursor = nxt
        depth += 1
    for rows in out.values():
        rows.sort(key=lambda r: (r["start_time"], r["snapshot_id"] or ""), reverse=True)
    return out


def snapshots_for_path(repo: str, source: str, full_path: str) -> list[dict]:
    return holding_snapshots(repo, source, [full_path])[full_path]


def folder_states(repo: str, prefixes: list[str], source: str | None = None,
                  index: dict | None = None) -> dict[str, dict]:
    """``{prefix: {versions, snapshots, sources}}``: how many distinct states each folder has had -
    the different folder objects at its path across every snapshot of the sources that back it
    (kopia stores an unchanged folder once). One walk per source for all the prefixes."""
    if index is None:
        index = sources_index(repo)
    objs: dict[str, set] = {p: set() for p in prefixes}
    held = {p: 0 for p in prefixes}
    used = {p: 0 for p in prefixes}
    for src in ([source] if source else list(index)):
        snaps = index.get(src, [])
        if not snaps:
            continue
        segs = {p: _segments(src, p) for p in prefixes}
        roots = [p for p, s in segs.items() if s == []]
        deep = [p for p, s in segs.items() if s]
        for p in roots:            # the source's own root
            rows = [(s.get("rootEntry") or {}).get("obj") for s in snaps]
            rows = [o for o in rows if o]
            used[p] += 1
            held[p] += len(rows)
            objs[p].update(rows)
        if deep:
            for p, rows in holding_snapshots(repo, src, deep, snaps).items():
                used[p] += 1
                found = [r.get("obj") for r in rows if r.get("obj")]
                held[p] += len(found)
                objs[p].update(found)
    return {p: {"versions": len(objs[p]), "snapshots": held[p], "sources": used[p]} for p in prefixes}
