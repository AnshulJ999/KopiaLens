"""Prune versions: remove chosen versions of one file, or chosen snapshots of one folder.

A file's rows are its distinct versions (one object each, held by one or more snapshots); a
folder's rows are the snapshots that hold it (the folder's object in that snapshot, and every file
object under it). Removing a row removes that object from exactly those snapshots, with
``kopia snapshot fix remove-files --object-id <obj> --manifest-id <snapshot>...``, in every source
on this machine that backs up the path.

Pricing works on (source, object) pairs. Removing rows drops the pairs no kept row and no other
path still holds; a piece is freed when every source (crefs) and every pair (crefo) using it is
among the dropped ones - the same rule as ``physical._freed``.
"""

from __future__ import annotations

import collections
import threading
import time
import uuid

import pyarrow as pa

from ..core.applog import get_logger
from ..core.sources import sources_index
from . import entries, holding, physical, queries, resolve
from .store import read_cursor

log = get_logger("prune")

_PLANS: collections.OrderedDict[str, dict] = collections.OrderedDict()
_PLANS_MAX = 8
_LOCK = threading.Lock()


def _machine(source: str) -> str:
    return source.split(":", 1)[0]


def _holders(repo: str, source: str | None, full_path: str) -> tuple[list[str], bool, list[str]]:
    """(sources on one machine holding the path, is it a folder, sources on other machines)."""
    cur = read_cursor(repo)
    try:
        files = [r[0] for r in cur.execute(
            "SELECT DISTINCT s.source FROM pathx p JOIN src s ON s.src_id = p.src_id "
            "WHERE p.full_path = ? ORDER BY s.source", [full_path]).fetchall()]
    finally:
        cur.close()
    if files:
        cands, is_dir = files, False
    else:
        cands, is_dir = [r["source"] for r in queries.folder_sources(repo, full_path)], True
    if not cands:
        return [], is_dir, []
    machine = _machine(source) if source else _machine(cands[0])
    mine = sorted(s for s in cands if _machine(s) == machine)
    if source and source not in mine and not is_dir:
        mine = sorted(set(mine) | {source})
    return mine, is_dir, sorted(s for s in cands if _machine(s) != machine)


def _tree_objects(cur, pairs: list[tuple[str, str]]) -> dict[str, set[str]]:
    """(tag, folder object) pairs -> tag -> every file object under those folders, for folders the
    tree holds (one recursive query)."""
    out: dict[str, set[str]] = {}
    ids = resolve.cid_ids(cur, sorted({o for _t, o in pairs}))
    rows_in = [(t, ids[o]) for t, o in pairs if o in ids]
    if not rows_in:
        return out
    name = f"_prune_roots_{uuid.uuid4().hex[:8]}"
    cur.register(name, pa.table({"tag": pa.array([t for t, _ in rows_in], pa.string()),
                                 "root": pa.array([r for _, r in rows_in], pa.int32())}))
    try:
        rows = cur.execute(f"""
            WITH RECURSIVE r(tag, dir_id) AS (
                SELECT w.tag, t.dir_id FROM tdir t JOIN {name} w ON w.root = t.cid_id
                UNION
                SELECT r.tag, e.child FROM r JOIN tent e ON e.dir_id = r.dir_id
                WHERE e.child IS NOT NULL)
            SELECT DISTINCT r.tag, c.content_id FROM r
            JOIN tent e ON e.dir_id = r.dir_id
            JOIN cid c ON c.cid_id = e.cid_id
            WHERE e.etype = 0 AND e.cid_id IS NOT NULL""").fetchall()
    finally:
        cur.unregister(name)
    for tag, obj in rows:
        out.setdefault(tag, set()).add(obj)
    return out


def _subtree_objects(repo: str, cur, dir_objs: list[str]) -> tuple[dict[str, set[str]], set[str]]:
    """Folder object -> every file object under it, plus the folders that could not be read.

    Folders the tree holds resolve in SQL; a snapshot newer than the last index walks through
    ``entries.children_of`` (kopia for what the tree lacks) until it reaches folders the tree has.
    """
    in_tree = set(resolve.cid_ids(cur, dir_objs))
    if in_tree:
        in_tree = {r[0] for r in cur.execute(
            "SELECT c.content_id FROM tdir t JOIN cid c ON c.cid_id = t.cid_id "
            "WHERE c.content_id IN (%s)" % ",".join("?" * len(in_tree)), sorted(in_tree)).fetchall()}
    out: dict[str, set[str]] = {d: set() for d in dir_objs}
    unknown: set[str] = set()
    sql_pairs = [(d, d) for d in dir_objs if d in in_tree]
    frontier = [(d, d) for d in dir_objs if d not in in_tree]
    while frontier:
        kids = entries.children_of(repo, [o for _r, o in frontier], cursor=cur)
        nxt: list[tuple[str, str]] = []
        sub = sorted({e.get("obj") for _r, o in frontier for e in kids.get(o, [])
                      if e.get("type") == "d" and e.get("obj")})
        sub_known = set(resolve.cid_ids(cur, sub)) if sub else set()
        if sub_known:
            sub_known = {r[0] for r in cur.execute(
                "SELECT c.content_id FROM tdir t JOIN cid c ON c.cid_id = t.cid_id "
                "WHERE c.content_id IN (%s)" % ",".join("?" * len(sub_known)), sorted(sub_known)).fetchall()}
        for root, o in frontier:
            if o not in kids:
                unknown.add(root)
                continue
            for e in kids[o]:
                obj = e.get("obj")
                if not obj:
                    continue
                if e.get("type") == "f":
                    out[root].add(obj)
                elif e.get("type") == "d":
                    (sql_pairs if obj in sub_known else nxt).append((root, obj))
        frontier = nxt
    for tag, objs in _tree_objects(cur, sql_pairs).items():
        out[tag] |= objs
    return out, unknown


def _outside_pairs(cur, objs: set[str], holders: list[str], full_path: str, is_dir: bool) -> set:
    """(source, object) pairs holding these objects at a path OUTSIDE the target (another path,
    another source, another machine): those pairs stay whatever is pruned."""
    if not objs:
        return set()
    ids = resolve.cid_ids(cur, sorted(objs))
    if not ids:
        return set()
    sids = resolve.src_map(cur)
    hold = [sids[h] for h in holders if h in sids] or [-1]
    name = f"_prune_objs_{uuid.uuid4().hex[:8]}"
    cur.register(name, pa.table({"cid": pa.array(list(ids.values()), pa.int32())}))
    try:
        if is_dir:
            scope, params = queries.scope_sql(cur, full_path, include_path=True)
            inside = f"({scope}) AND p.src_id IN ({','.join('?' * len(hold))})"
            params = list(params) + hold
        else:
            inside = f"p.full_path = ? AND p.src_id IN ({','.join('?' * len(hold))})"
            params = [full_path] + hold
        rows = cur.execute(f"""
            SELECT DISTINCT s.source, c.content_id FROM filev f
            JOIN {name} w ON w.cid = f.cid_id
            JOIN pathx p ON p.path_id = f.path_id
            JOIN src s ON s.src_id = p.src_id
            JOIN cid c ON c.cid_id = f.cid_id
            WHERE NOT ({inside})""", params).fetchall()
    finally:
        cur.unregister(name)
    return {(s, o) for s, o in rows}


def plan(repo: str, source: str | None, full_path: str) -> dict:
    """Rows with per-row "frees on its own", plus a token for pricing a set of ticks."""
    t0 = time.perf_counter()
    holders, is_dir, other_machines = _holders(repo, source, full_path)
    if not holders:
        return {"found": False}
    snaps: list[dict] = []
    for h in holders:
        if is_dir and holding._segments(h, full_path) == []:
            # The folder IS this source's root: every snapshot holds it, and pruning it there means
            # deleting the snapshot (remove-files only removes entries inside a folder).
            for s in sources_index(repo).get(h, []):
                root = s.get("rootEntry") or {}
                summ = root.get("summ") or {}
                if root.get("obj"):
                    snaps.append({"snapshot_id": s.get("id"), "start_time": s.get("startTime", ""),
                                  "obj": root["obj"], "size": int(summ.get("size", 0) or 0),
                                  "files": int(summ.get("files", 0) or 0), "type": "d",
                                  "pinned": bool(s.get("pins")),
                                  "retention": s.get("retention") or s.get("retentionReason") or [],
                                  "source": h, "root": True})
            continue
        for s in holding.holding_snapshots(repo, h, [full_path])[full_path]:
            if s.get("obj") and (s.get("type") == "d") == is_dir:
                snaps.append({**s, "source": h})
    if not snaps:
        return {"found": False}

    rows: list[dict] = []
    if is_dir:
        cur = read_cursor(repo)
        try:
            sub, unreadable = _subtree_objects(repo, cur, sorted({s["obj"] for s in snaps}))
        finally:
            cur.close()
        for s in sorted(snaps, key=lambda x: (x["start_time"], x["snapshot_id"])):
            rows.append({"obj": s["obj"], "snapshots": [s], "size": s["size"], "files": s.get("files", 0),
                         "unreadable": s["obj"] in unreadable,
                         "pairs": {(s["source"], o) for o in sub.get(s["obj"], ())}})
    else:
        by_obj: dict[str, list[dict]] = {}
        for s in snaps:
            by_obj.setdefault(s["obj"], []).append(s)
        for obj, ss in sorted(by_obj.items(), key=lambda kv: min((x["start_time"], x["snapshot_id"]) for x in kv[1])):
            ss.sort(key=lambda x: (x["start_time"], x["snapshot_id"]))
            rows.append({"obj": obj, "snapshots": ss, "size": ss[0]["size"],
                         "pairs": {(x["source"], obj) for x in ss}})

    every_obj = {o for r in rows for _s, o in r["pairs"]}
    cur = read_cursor(repo)
    try:
        outside = _outside_pairs(cur, every_obj, holders, full_path, is_dir)
    finally:
        cur.close()
    chunks = physical._chunks_for_objs(repo, sorted(every_obj))
    fresh: set[str] = set()   # versions backed up since the last index: ask kopia
    for obj, got in list(chunks.items()):
        if got is None and obj.startswith("I"):
            chunks[obj] = physical._chunks_from_kopia(repo, obj)
            fresh.add(obj)
    pieces = {c for cs in chunks.values() if cs for c in cs}
    refs = physical._refcount_for(repo, pieces)
    if refs is None:
        refs = physical._refcount_map(repo)
    state = {"repo": repo, "full_path": full_path, "is_dir": is_dir, "holders": holders,
             "rows": rows, "outside": outside, "chunks": chunks, "refs": refs,
             "objrefs": physical._objrefs_for(repo, pieces), "sizes": physical._sizes_for(repo, pieces)}
    stored = sum(state["sizes"].get(c, 0) for c in pieces)
    for i, r in enumerate(rows):
        r["frees"] = _price(state, {i})["frees"]
    token = uuid.uuid4().hex
    with _LOCK:
        _PLANS[token] = state
        while len(_PLANS) > _PLANS_MAX:
            _PLANS.popitem(last=False)
    elsewhere = {o for _s, o in outside}
    unknown = sum(1 for o in every_obj if chunks.get(o) is None)
    log.info("repo=%s prune plan: %d row(s), %d object(s) in %.1fs", repo, len(rows), len(every_obj),
             time.perf_counter() - t0)
    return {
        "found": True, "token": token, "kind": "folder" if is_dir else "file", "full_path": full_path,
        "holders": holders, "other_machines": other_machines, "stored": stored, "unknown": unknown,
        "rows": [{
            "id": i, "obj": r["obj"], "size": r["size"], "files": r.get("files"), "frees": r["frees"],
            "first": r["snapshots"][0]["start_time"], "last": r["snapshots"][-1]["start_time"],
            "pinned": any(s.get("pinned") for s in r["snapshots"]),
            "retention": sorted({x for s in r["snapshots"] for x in (s.get("retention") or [])}),
            "elsewhere": (not is_dir) and r["obj"] in elsewhere,
            "whole_snapshot": any(s.get("root") for s in r["snapshots"]),
            "unreadable": bool(r.get("unreadable")),
            # sizes of pieces newer than the last index are not known yet, so this row reads low
            "since_index": any(o in fresh for _s, o in r["pairs"]),
            "snapshots": [{"id": s["snapshot_id"], "source": s["source"], "start_time": s["start_time"],
                           "pinned": bool(s.get("pinned"))} for s in r["snapshots"]],
        } for i, r in enumerate(rows)],
    }


def _price(state: dict, remove: set[int]) -> dict:
    rows = state["rows"]
    kept = {p for i, r in enumerate(rows) if i not in remove for p in r["pairs"]} | state["outside"]
    dropped = {p for i in remove for p in rows[i]["pairs"]} - kept
    # Pieces a kept version or another path still uses stay, whatever the maps say: the maps
    # cover the rest of the repository, this covers the file's own versions directly.
    kept_pieces = {c for _s, o in kept for c in (state["chunks"].get(o) or ())}
    by_piece: dict[str, tuple[set, set]] = {}
    unknown = 0
    for src, obj in dropped:
        cs = state["chunks"].get(obj)
        if cs is None:
            unknown += 1
            continue
        for c in cs:
            srcs, pairs = by_piece.setdefault(c, (set(), set()))
            srcs.add(src)
            pairs.add((src, obj))
    sizes, refs, objrefs = state["sizes"], state["refs"], state["objrefs"]
    frees = sum(sizes.get(c, 0) for c, (srcs, pairs) in by_piece.items()
                if c not in kept_pieces and physical._freed(c, refs, objrefs, len(srcs), len(pairs)))
    # A folder snapshot that could not be read: a kept one may hold more than counted here.
    uncertain = any(r.get("unreadable") for r in rows)
    return {"frees": frees, "unknown": unknown, "uncertain": uncertain}


def price(token: str, remove: list[int]) -> dict | None:
    """Frees / stays stored / what else goes, for one set of ticked rows. None = plan expired."""
    with _LOCK:
        state = _PLANS.get(token)
        if state is not None:
            _PLANS.move_to_end(token)
    if state is None:
        return None
    rm = {i for i in remove if 0 <= i < len(state["rows"])}
    out = _price(state, rm)
    pieces = {c for cs in state["chunks"].values() if cs for c in cs}
    stored = sum(state["sizes"].get(c, 0) for c in pieces)
    rows = state["rows"]
    out.update({
        "remove": len(rm), "keep": len(rows) - len(rm), "stays": stored - out["frees"],
        "snapshots": sum(len(rows[i]["snapshots"]) for i in rm),
        "commands": items_for(state, rm),
    })
    return out


def items_for(state: dict, remove: set[int]) -> list[dict]:
    """The job's input: one entry per object, with exactly the snapshots it is removed from. A
    snapshot whose root IS the folder is deleted instead (``obj`` None)."""
    by_obj: dict[str | None, list[str]] = {}
    for i in sorted(remove):
        for s in state["rows"][i]["snapshots"]:
            key = None if s.get("root") else state["rows"][i]["obj"]
            by_obj.setdefault(key, []).append(s["snapshot_id"])
    return [{"obj": o, "snapshot_ids": ids} for o, ids in by_obj.items()]


def outliers(repo: str, limit: int = 100) -> dict:
    """Versions of big files that free the most on their own: pieces used only by this version
    (crefo) in only its holders (crefs), the version held at one path and not the newest one.
    ``ready`` is False until a generation carries the piece-sharing map."""
    cur = read_cursor(repo)
    try:
        if not cur.execute("SELECT count(*) FROM crefo").fetchone()[0]:
            return {"rows": [], "ready": False}
        rows = cur.execute("""
            WITH v AS (SELECT p.full_path, f.cid_id, p.src_id, min(sn.start_time) AS first_time,
                              max(f.size) AS size
                       FROM filev f JOIN pathx p ON p.path_id = f.path_id
                       JOIN snap sn ON sn.snap_id = f.snap_id
                       JOIN cid c ON c.cid_id = f.cid_id
                       WHERE c.content_id LIKE 'I%'
                       GROUP BY p.full_path, f.cid_id, p.src_id),
            vp AS (SELECT full_path, cid_id, count(DISTINCT src_id) AS holders, min(first_time) AS first_time,
                          max(size) AS size, min(src_id) AS src_id FROM v GROUP BY full_path, cid_id),
            multi AS (SELECT full_path, count(*) AS versions FROM vp GROUP BY full_path HAVING count(*) > 1),
            one_path AS (SELECT cid_id FROM vp GROUP BY cid_id HAVING count(*) = 1),
            newest AS (SELECT DISTINCT full_path, cid_id FROM pathx),
            cand AS (SELECT vp.*, m.versions FROM vp JOIN multi m USING (full_path)
                     JOIN one_path USING (cid_id)
                     WHERE NOT EXISTS (SELECT 1 FROM newest n
                                       WHERE n.full_path = vp.full_path AND n.cid_id = vp.cid_id))
            SELECT s.source, cand.full_path, c.content_id, cand.first_time, cand.size, cand.versions,
                   sum(z.packed) AS frees
            FROM cand JOIN tpart t ON t.iobj_cid = cand.cid_id
            JOIN crefo o ON o.cid_id = t.part_cid AND o.refs <= cand.holders
            JOIN crefs r ON r.cid_id = t.part_cid AND r.refs <= cand.holders
            JOIN csize z ON z.cid_id = t.part_cid
            JOIN cid c ON c.cid_id = cand.cid_id
            JOIN src s ON s.src_id = cand.src_id
            GROUP BY ALL ORDER BY frees DESC LIMIT ?""", [limit]).fetchall()
    finally:
        cur.close()
    return {"ready": True, "rows": [
        {"source": s, "full_path": fp, "obj": o, "first": str(t) if t else "", "size": int(z or 0),
         "versions": int(n), "frees": int(fr or 0)} for s, fp, o, t, z, n, fr in rows]}
