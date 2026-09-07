"""Excluded browser: which on-disk paths are NOT backed up by EITHER kopia repo.

"Backed up" = the path is present in `pathx(in_latest=true)` of repo1 OR repo2 (case-insensitive,
via inline `lower(full_path)`). Disk side = Everything (`es.exe`). For each folder you open we do
ONE indexed range scan per repo (bounded to that subtree), aggregated by immediate child segment.
"""

from __future__ import annotations

import collections
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config import kopia_repos

from ..core.everything import es_children, list_drives, recursive_count, recursive_size
from .store import read_cursor

_SEP = chr(92)  # backslash
# (folder_lc, tolerance) -> (ts, result); TTL so repeated nav is instant. LRU-bounded (matches the
# _RECLAIM_CACHE/_RECL_FILES_CACHE style) - was TTL-only with no count cap, so a long session
# browsing many distinct folders grew this unbounded (Pass D6 footgun fix).
_LIST_CACHE: collections.OrderedDict[tuple, tuple] = collections.OrderedDict()
_LIST_TTL = 120
_LIST_CACHE_MAX = 200
_LIST_LOCK = threading.Lock()  # guards the LRU: sync FastAPI handlers run in a threadpool, so the
# check->get->move_to_end->popitem sequence can interleave across requests (KeyError / lost entries)


def _list_cache_get(ckey: tuple):
    with _LIST_LOCK:
        val = _LIST_CACHE.get(ckey)
        if val is not None:
            _LIST_CACHE.move_to_end(ckey)
        return val


def _list_cache_put(ckey: tuple, val: tuple) -> None:
    with _LIST_LOCK:
        _LIST_CACHE[ckey] = val
        _LIST_CACHE.move_to_end(ckey)
        while len(_LIST_CACHE) > _LIST_CACHE_MAX:
            _LIST_CACHE.popitem(last=False)


def _prefix_bounds(folder: str) -> tuple[str, str, int]:
    """Lowercased [lo, hi) range that captures everything under `folder`, plus the prefix length
    (chars to strip to get the path relative to the folder)."""
    lo = folder.lower().rstrip(_SEP) + _SEP
    return lo, lo + "￿", len(lo)


def kopia_backed_children(folder: str) -> dict[str, dict]:
    """Per immediate child segment under `folder`, the backed-up (in_latest) coverage from BOTH
    kopia repos combined: {seg_lower: {logical bytes, files, has_file, last_seen, repos:set}}.
    `logical` = kopia's stored file size (exact backed size, comparable to Everything's disk size)."""
    lo, hi, plen = _prefix_bounds(folder)
    agg: dict[str, dict] = {}
    for repo in kopia_repos():
        cur = read_cursor(repo)
        try:
            # Dedupe by path FIRST (GROUP BY path_lc): a file backed by several overlapping sources
            # (e.g. the C:\ source AND a granular C:\Users\... source) must count ONCE, not per-source
            # - else backed size inflates past disk size and the folder wrongly reads fully-backed.
            rows = cur.execute(
                """
                SELECT seg, bool_or(is_file) AS has_file, sum(sz) AS logical, count(*) AS cnt,
                       max(ls) AS last_seen
                FROM (
                    SELECT lower(p.full_path) AS path_lc,
                           any_value(split_part(substr(lower(p.full_path), ?), ?, 1)) AS seg,
                           any_value(instr(substr(lower(p.full_path), ?), ?) = 0) AS is_file,
                           max(p.size) AS sz, max(p.last_seen) AS ls
                    FROM pathx p
                    WHERE p.in_latest AND lower(p.full_path) >= ? AND lower(p.full_path) < ?
                    GROUP BY lower(p.full_path)
                )
                GROUP BY seg
                """,
                [plen + 1, _SEP, plen + 1, _SEP, lo, hi],
            ).fetchall()
        finally:
            cur.close()
        for seg, has_file, logical, cnt, last_seen in rows:
            if not seg:
                continue
            a = agg.setdefault(seg, {"logical": 0, "files": 0, "has_file": False, "last_seen": "", "repos": set()})
            a["logical"] += int(logical or 0)
            a["files"] += int(cnt or 0)
            a["has_file"] = a["has_file"] or bool(has_file)
            ls = str(last_seen)[:19].replace("T", " ") if last_seen else ""
            if ls > a["last_seen"]:
                a["last_seen"] = ls
            a["repos"].add(repo)
    return agg


def backed_children(folder: str) -> dict[str, dict]:
    """Kopia coverage (kopia_backed_children) PLUS a Backblaze cross-coverage flag per child.
    The BZ contribution is additive metadata only - it adds "bz" to the child's `repos` set and a
    separate `bz_logical`/`bz_files`, but is NEVER folded into `logical`/`files` (those stay the
    selected-Kopia backed total, so the Excluded math is unchanged - cross-coverage doesn't
    supersede the per-repo numbers)."""
    agg = kopia_backed_children(folder)
    try:
        from ..backblaze import queries as bzq
        for seg, cov in bzq.coverage_children(folder).items():
            a = agg.setdefault(seg, {"logical": 0, "files": 0, "has_file": False,
                                     "last_seen": "", "repos": set()})
            a["repos"].add("bz")
            a["bz_logical"] = int(cov.get("logical", 0))
            a["bz_files"] = int(cov.get("files", 0))
    except Exception:  # bzdata missing / BZ index not built - Kopia Excluded still works
        pass
    return agg


def kopia_drive_backed() -> dict[str, tuple]:
    """{DRIVE_LETTER: (files, bytes)} of kopia-backed (in_latest, deduped by path) per local drive
    letter. Used by the BZ Excluded drive view for its Kopia cross-coverage column. Overlap between
    repo1/repo2 on the same path is counted once per repo (a coarse cross-coverage hint, not exact)."""
    out: dict[str, tuple] = {}
    for repo in kopia_repos():
        cur = read_cursor(repo)
        try:
            rows = cur.execute(
                "SELECT upper(substr(path_lc, 1, 1)) AS drive, sum(sz) AS b, count(*) AS c FROM ("
                "  SELECT lower(p.full_path) AS path_lc, max(p.size) AS sz FROM pathx p"
                "  WHERE p.in_latest AND substr(lower(p.full_path), 2, 1) = ':' "
                "  GROUP BY lower(p.full_path)"
                ") GROUP BY drive").fetchall()
        finally:
            cur.close()
        for drive, b, c in rows:
            f0, b0 = out.get(drive, (0, 0))
            out[drive] = (f0 + int(c or 0), b0 + int(b or 0))
    return out


def _newest_snapshot() -> str:
    """Newest snapshot start_time across both repos (small `indexed_snapshots` table = instant).
    Used only to label not-backed-up files 'new' (mtime newer than any snapshot) vs 'excluded'."""
    best = ""
    for repo in kopia_repos():
        cur = read_cursor(repo)
        try:
            row = cur.execute("SELECT max(start_time) FROM isnaps").fetchone()
        finally:
            cur.close()
        if row and row[0]:
            t = str(row[0]).replace("T", " ")[:19]
            if t > best:
                best = t
    return best


def folder_listing(folder: str, tolerance: float = 0.05) -> dict:
    """Combine the disk listing (es.exe) with backup coverage (index) for one folder. BACKED bytes
    are exact (index sum of in_latest sizes). EXCLUDED bytes are disk - backed (approximate by the
    changed/deleted-file drift; the backed-file COUNT helps tell drift from real exclusion). A
    folder counts as 'backed' when its excluded fraction is below `tolerance` (cuts partial noise)."""
    ckey = (folder.lower(), tolerance)
    cached = _list_cache_get(ckey)
    if cached and time.time() - cached[0] < _LIST_TTL:
        return cached[1]  # repeated navigation is instant (within TTL)
    disk = es_children(folder)
    backed = backed_children(folder)
    newest = _newest_snapshot()
    items: list[dict] = []
    for d in disk:
        seg = d["name"].lower()
        b = backed.get(seg)
        repos = sorted(b["repos"]) if b else []
        repo_label = "+".join(repos) if repos else ""  # N backends: e.g. "1", "1+2", "bz", "1+2+bz"
        bz_b = int(b.get("bz_logical", 0)) if b else 0   # BZ cross-coverage (additive display only)
        bz_f = int(b.get("bz_files", 0)) if b else 0
        last_backed = b["last_seen"] if b else ""
        if d["is_dir"]:
            bb = b["logical"] if b else 0
            db = d["size"] or 0
            excl = max(0, db - bb)
            frac = (excl / db) if db else 0
            status = "excluded" if bb == 0 else ("backed" if frac <= tolerance else "partial")
            items.append({
                "name": d["name"], "is_dir": True, "disk_bytes": db, "backed_bytes": bb,
                "excluded_bytes": excl, "pct_excluded": round(frac * 100, 1),
                "backed_files": b["files"] if b else 0, "last_backed": last_backed,
                "repo": repo_label, "bz_backed_bytes": bz_b, "bz_backed_files": bz_f,
                "status": status, "mtime": d["mtime"],
            })
        else:
            backed_file = bool(b and b.get("has_file"))
            mt = (d["mtime"] or "").replace("T", " ")[:19]
            status = "backed" if backed_file else ("new" if (newest and mt > newest) else "excluded")
            sz = d["size"] or 0
            items.append({
                "name": d["name"], "is_dir": False, "disk_bytes": sz,
                "backed_bytes": sz if backed_file else 0, "excluded_bytes": 0 if backed_file else sz,
                "pct_excluded": 0 if backed_file else 100, "backed_files": 1 if backed_file else 0,
                "last_backed": last_backed, "repo": repo_label,
                "bz_backed_bytes": bz_b, "bz_backed_files": bz_f,
                "status": status, "mtime": d["mtime"],
            })
    # Recursive FILE counts: direct files = 1; child folders via parallel Everything count-queries
    # (instant, index-computed). total_size = disk_bytes; excluded_files = total - backed.
    sep = _SEP
    base = folder.rstrip(sep)
    dirs = [it for it in items if it["is_dir"]]

    def _cnt(it):
        return recursive_count(base + sep + it["name"])

    counts = list(ThreadPoolExecutor(max_workers=16).map(_cnt, dirs)) if dirs else []
    cmap = {id(it): n for it, n in zip(dirs, counts)}
    for it in items:
        it["total_size"] = it["disk_bytes"]
        tf = cmap.get(id(it), 1) if it["is_dir"] else 1
        it["total_files"] = tf
        it["excluded_files"] = max(0, tf - it["backed_files"])

    items.sort(key=lambda x: (not x["is_dir"], -x["excluded_bytes"], x["name"].lower()))
    result = {
        "folder": folder,
        "items": items,
        "total_excluded": sum(i["excluded_bytes"] for i in items),
        "total_backed": sum(i["backed_bytes"] for i in items),
        "count": len(items),
    }
    _list_cache_put(ckey, (time.time(), result))
    return result


def drive_stats() -> list[dict]:
    """Per local drive: total (Everything) vs backed (index, deduped) → excluded size + file count.
    Parallel es calls. Used by the drives ('This PC') view so each drive shows its REAL excluded
    total, not its used-disk size."""
    drives = list_drives()  # [{path, used, total}]
    bz_by_letter: dict[str, tuple] = {}
    try:
        from ..backblaze import queries as bzq
        for r in bzq.overview()["rows"]:
            bz_by_letter[str(r["drive"]).upper()] = (r["backed_files"], r["backed_bytes"])
    except Exception:  # bzdata missing / BZ index not built - drive view still works without it
        pass

    def _one(d: dict) -> dict:
        p = d["path"]
        ts, tc = recursive_size(p), recursive_count(p)
        lo = p.lower().rstrip(_SEP) + _SEP
        hi = lo + "￿"
        bs = bc = 0
        for repo in kopia_repos():
            cur = read_cursor(repo)
            try:
                row = cur.execute(
                    "SELECT sum(sz), count(*) FROM (SELECT max(p.size) AS sz FROM pathx p "
                    "WHERE p.in_latest AND lower(p.full_path) >= ? AND lower(p.full_path) < ? "
                    "GROUP BY lower(p.full_path))",
                    [lo, hi],
                ).fetchone()
            finally:
                cur.close()
            bs += int(row[0] or 0)
            bc += int(row[1] or 0)
        bzf, bzb = bz_by_letter.get(p[:1].upper(), (0, 0))
        return {
            "path": p, "used": d["used"], "total_size": ts, "total_files": tc,
            "backed_bytes": bs, "backed_files": bc,
            "excluded_bytes": max(0, ts - bs), "excluded_files": max(0, tc - bc),
            "bz_backed_bytes": bzb, "bz_backed_files": bzf,
        }

    return list(ThreadPoolExecutor(max_workers=8).map(_one, drives)) if drives else []
