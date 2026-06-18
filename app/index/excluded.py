"""Excluded browser: which on-disk paths are NOT backed up by EITHER kopia repo.

"Backed up" = the path is present in `paths(in_latest=true)` of repo1 OR repo2 (case-insensitive,
via the `path_lc` index). Disk side = Everything (`es.exe`). For each folder you open we do ONE
indexed range scan per repo (bounded to that subtree), aggregated by immediate child segment.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from config import REPOS

from ..core.everything import es_children, list_drives, recursive_count, recursive_size
from .schema import read_cursor

_SEP = chr(92)  # backslash


def _prefix_bounds(folder: str) -> tuple[str, str, int]:
    """Lowercased [lo, hi) range that captures everything under `folder`, plus the prefix length
    (chars to strip to get the path relative to the folder)."""
    lo = folder.lower().rstrip(_SEP) + _SEP
    return lo, lo + "￿", len(lo)


def backed_children(folder: str) -> dict[str, dict]:
    """Per immediate child segment under `folder`, the backed-up (in_latest) coverage from BOTH
    repos combined: {seg_lower: {logical bytes, files, has_file, last_seen, repos:set}}. `logical`
    = kopia's stored file size (exact backed size, comparable to Everything's disk size)."""
    lo, hi, plen = _prefix_bounds(folder)
    agg: dict[str, dict] = {}
    for repo in REPOS:
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
                    SELECT path_lc,
                           any_value(split_part(substr(path_lc, ?), ?, 1)) AS seg,
                           any_value(instr(substr(path_lc, ?), ?) = 0) AS is_file,
                           max(size) AS sz, max(last_seen) AS ls
                    FROM paths
                    WHERE repo = ? AND in_latest AND path_lc >= ? AND path_lc < ?
                    GROUP BY path_lc
                )
                GROUP BY seg
                """,
                [plen + 1, _SEP, plen + 1, _SEP, repo, lo, hi],
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


def _newest_snapshot() -> str:
    """Newest snapshot start_time across both repos (small `indexed_snapshots` table = instant).
    Used only to label not-backed-up files 'new' (mtime newer than any snapshot) vs 'excluded'."""
    best = ""
    for repo in REPOS:
        cur = read_cursor(repo)
        try:
            row = cur.execute("SELECT max(start_time) FROM indexed_snapshots WHERE repo=?", [repo]).fetchone()
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
    disk = es_children(folder)
    backed = backed_children(folder)
    newest = _newest_snapshot()
    items: list[dict] = []
    for d in disk:
        seg = d["name"].lower()
        b = backed.get(seg)
        repos = sorted(b["repos"]) if b else []
        repo_label = "both" if len(repos) == 2 else (repos[0] if repos else "")
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
                "repo": repo_label, "status": status, "mtime": d["mtime"],
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
                "last_backed": last_backed, "repo": repo_label, "status": status, "mtime": d["mtime"],
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
    return {
        "folder": folder,
        "items": items,
        "total_excluded": sum(i["excluded_bytes"] for i in items),
        "total_backed": sum(i["backed_bytes"] for i in items),
        "count": len(items),
    }


def drive_stats() -> list[dict]:
    """Per local drive: total (Everything) vs backed (index, deduped) → excluded size + file count.
    Parallel es calls. Used by the drives ('This PC') view so each drive shows its REAL excluded
    total, not its used-disk size."""
    drives = list_drives()  # [{path, used, total}]

    def _one(d: dict) -> dict:
        p = d["path"]
        ts, tc = recursive_size(p), recursive_count(p)
        lo = p.lower().rstrip(_SEP) + _SEP
        hi = lo + "￿"
        bs = bc = 0
        for repo in REPOS:
            cur = read_cursor(repo)
            try:
                row = cur.execute(
                    "SELECT sum(sz), count(*) FROM (SELECT max(size) AS sz FROM paths "
                    "WHERE repo=? AND in_latest AND path_lc >= ? AND path_lc < ? GROUP BY path_lc)",
                    [repo, lo, hi],
                ).fetchone()
            finally:
                cur.close()
            bs += int(row[0] or 0)
            bc += int(row[1] or 0)
        return {
            "path": p, "used": d["used"], "total_size": ts, "total_files": tc,
            "backed_bytes": bs, "backed_files": bc,
            "excluded_bytes": max(0, ts - bs), "excluded_files": max(0, tc - bc),
        }

    return list(ThreadPoolExecutor(max_workers=8).map(_one, drives)) if drives else []
