"""Excluded browser: which on-disk paths are NOT backed up by EITHER kopia repo.

"Backed up" = the path is present in `paths(in_latest=true)` of repo1 OR repo2 (case-insensitive,
via the `path_lc` index). Disk side = Everything (`es.exe`). For each folder you open we do ONE
indexed range scan per repo (bounded to that subtree), aggregated by immediate child segment.
"""

from __future__ import annotations

from config import REPOS

from ..core.everything import es_children
from .schema import read_cursor

_SEP = chr(92)  # backslash


def _prefix_bounds(folder: str) -> tuple[str, str, int]:
    """Lowercased [lo, hi) range that captures everything under `folder`, plus the prefix length
    (chars to strip to get the path relative to the folder)."""
    lo = folder.lower().rstrip(_SEP) + _SEP
    return lo, lo + "￿", len(lo)


def backed_children(folder: str) -> dict[str, dict]:
    """Per immediate child segment under `folder`, the backed-up (in_latest) coverage from BOTH
    repos combined: {seg_lower: {"logical": bytes, "files": n, "has_file": bool}}. `logical` uses
    file size (comparable to Everything's disk size); `has_file` marks a direct backed-up file."""
    lo, hi, plen = _prefix_bounds(folder)
    agg: dict[str, dict] = {}
    for repo in REPOS:
        cur = read_cursor(repo)
        try:
            rows = cur.execute(
                """
                SELECT seg, bool_or(is_file) AS has_file, sum(sz) AS logical, count(*) AS cnt
                FROM (
                    SELECT split_part(substr(path_lc, ?), ?, 1) AS seg,
                           (instr(substr(path_lc, ?), ?) = 0) AS is_file,
                           size AS sz
                    FROM paths
                    WHERE repo = ? AND in_latest AND path_lc >= ? AND path_lc < ?
                )
                GROUP BY seg
                """,
                [plen + 1, _SEP, plen + 1, _SEP, repo, lo, hi],
            ).fetchall()
        finally:
            cur.close()
        for seg, has_file, logical, cnt in rows:
            if not seg:
                continue
            a = agg.setdefault(seg, {"logical": 0, "files": 0, "has_file": False})
            a["logical"] += int(logical or 0)
            a["files"] += int(cnt or 0)
            a["has_file"] = a["has_file"] or bool(has_file)
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


def folder_listing(folder: str) -> dict:
    """Combine the disk listing (es.exe) with backup coverage (index) for one folder. Files get a
    backed/excluded/new status; folders get backed/partial/excluded + an 'excluded under here'
    byte total (disk recursive size - backed-up logical)."""
    disk = es_children(folder)
    backed = backed_children(folder)
    newest = _newest_snapshot()
    items: list[dict] = []
    for d in disk:
        seg = d["name"].lower()
        b = backed.get(seg)
        if d["is_dir"]:
            bb = b["logical"] if b else 0
            db = d["size"] or 0
            excl = max(0, db - bb)
            status = "backed" if (bb > 0 and excl == 0) else ("excluded" if bb == 0 else "partial")
            items.append({
                "name": d["name"], "is_dir": True, "disk_bytes": db, "backed_bytes": bb,
                "excluded_bytes": excl, "status": status, "mtime": d["mtime"],
            })
        else:
            backed_file = bool(b and b.get("has_file"))
            mt = (d["mtime"] or "").replace("T", " ")[:19]
            status = "backed" if backed_file else ("new" if (newest and mt > newest) else "excluded")
            items.append({
                "name": d["name"], "is_dir": False, "disk_bytes": d["size"] or 0,
                "backed_bytes": (d["size"] or 0) if backed_file else 0,
                "excluded_bytes": 0 if backed_file else (d["size"] or 0),
                "status": status, "mtime": d["mtime"],
            })
    items.sort(key=lambda x: (not x["is_dir"], -x["excluded_bytes"], x["name"].lower()))
    return {
        "folder": folder,
        "items": items,
        "total_excluded": sum(i["excluded_bytes"] for i in items),
        "count": len(items),
    }
