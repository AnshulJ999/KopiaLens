"""Excluded browser: which on-disk paths are NOT backed up by ANY kopia repo.

"Backed up" = the path is present in `pathx(in_latest=true)` of any repo that counts as a backup
(`coverage_repos()` - a repo with the record's `counts_as_backup` off is a test/junk repository and
must not make a folder look covered). Disk side = this computer's disk lookup (Everything, or a
folder scan). The folder is a path on THIS computer, so its own shape decides the separator and
case rule: Windows paths fold case, POSIX paths match exactly. For each folder you open we do ONE
range scan per repo (bounded to that subtree), aggregated by immediate child segment.
"""

from __future__ import annotations

import collections
import contextvars
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config import REPOS, kopia_repos

from ..core import paths as pathrules
from ..core import disklookup
from ..core.disklookup import children as es_children, drive_totals, list_drives, mapping_for, recursive_count
from . import lanes
from .store import manifest_path, read_cursor


def coverage_repos() -> list:
    """Kopia repos that COUNT as backup coverage. A repo with `counts_as_backup` off (a test or
    junk repository) still shows its own tabs, but it must not make a folder look backed up here."""
    return [r for r in kopia_repos() if REPOS.get(r, {}).get("counts_as_backup", True)]

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


def forget_listings() -> None:
    """Drops remembered listings (a Count or Recount changes the disk side)."""
    with _LIST_LOCK:
        _LIST_CACHE.clear()


def _rules(folder: str) -> tuple[str, bool]:
    """(separator, fold case) for a path on this computer."""
    sep = pathrules.path_sep(folder)
    return sep, sep == pathrules.WIN_SEP


def _key(name: str, fold: bool) -> str:
    return name.lower() if fold else name


def _prefix_bounds(folder: str) -> tuple[str, str, int]:
    """[lo, hi) range (case-folded for Windows paths) that captures everything under `folder`,
    plus the prefix length (chars to strip to get the path relative to the folder). The
    bound comes from ``paths.prefix_bounds`` - ``lo + U+FFFF`` dropped every child whose name
    starts above U+FFFF."""
    sep, fold = _rules(folder)
    lo = _key(folder, fold).rstrip(sep) + sep
    _, hi = pathrules.prefix_bounds(lo)
    return lo, hi, len(lo)


def _path_sql(fold: bool) -> str:
    return "lower(p.full_path)" if fold else "p.full_path"


def _computer_sql(computer: str) -> tuple[str, str, list]:
    """(join, condition, params) limiting ``pathx`` to one computer's backups; empty for any."""
    if not computer:
        return "", "", []
    from .queries import _HOST_SQL
    return "JOIN src s ON s.src_id = p.src_id", f" AND {_HOST_SQL} = ?", [computer.lower()]


def _repo_computer(repo: str, computer: str | None) -> str:
    """The computer whose backups count: the given one ("" = any), or None = this computer, by the
    name the repository's snapshots use for it. Another computer's backup of the same path is
    another computer's files."""
    if computer is not None:
        return computer
    from ..core.repos import client_host
    return client_host(repo)


def kopia_backed_children(folder: str, computer: str | None = None) -> dict[str, dict]:
    """Per immediate child segment under `folder`, the backed-up (in_latest) coverage from BOTH
    kopia repos combined: {seg_lower: {logical bytes, files, has_file, last_seen, repos:set}}.
    `logical` = kopia's stored file size (exact backed size, comparable to Everything's disk size).
    ``computer``: whose backups count - None = this computer's, "" = any computer's, or a name (a
    mapped network place)."""
    lo, hi, plen = _prefix_bounds(folder)
    sep, fold = _rules(folder)
    path = _path_sql(fold)
    agg: dict[str, dict] = {}
    for repo in coverage_repos():
        join, cond, cparams = _computer_sql(_repo_computer(repo, computer))
        cur = read_cursor(repo)
        try:
            # Dedupe by path FIRST: a file backed by several overlapping sources (e.g. the C:\
            # source AND a granular C:\Users\... source) must count ONCE, not per-source - else
            # backed size inflates past disk size and the folder wrongly reads fully-backed.
            rows = cur.execute(
                f"""
                SELECT seg, bool_or(is_file) AS has_file, sum(sz) AS logical, count(*) AS cnt,
                       max(ls) AS last_seen
                FROM (
                    SELECT {path} AS path_key,
                           any_value(split_part(substr({path}, ?), ?, 1)) AS seg,
                           any_value(instr(substr({path}, ?), ?) = 0) AS is_file,
                           max(p.size) AS sz, max(p.last_seen) AS ls
                    FROM pathx p {join}
                    WHERE p.in_latest AND {path} >= ? AND {path} < ?{cond}
                    GROUP BY {path}
                )
                GROUP BY seg
                """,
                [plen + 1, sep, plen + 1, sep, lo, hi, *cparams],
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
        from ..backblaze import paths as bzp, queries as bzq
        if not bzp.is_this_computer():
            return agg   # another computer's Backblaze: nothing to compare with this disk
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
    """{Backblaze volume key: (files, bytes)} of kopia-backed (in_latest, deduped by path) per
    volume: the drive letter on Windows, the mount on a Mac. Used by the BZ Excluded drive view for
    its Kopia cross-coverage column. Overlap between repo1/repo2 on the same path is counted once
    per repo (a coarse cross-coverage hint, not exact)."""
    from ..backblaze import paths as bzp
    if bzp.platform() == "mac":
        return _kopia_mount_backed(bzp.volumes())
    out: dict[str, tuple] = {}
    for repo in coverage_repos():
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


def _kopia_mount_backed(vols) -> dict[str, tuple]:
    """Per Mac volume: kopia-backed (files, bytes) under its mount. A volume mounted inside
    another ("/Volumes/X" inside "/") counts there only."""
    gen = _generation_key()
    tot = {v.key: _drive_backed(v.mount, gen) for v in vols}   # (bytes, files)
    out: dict[str, tuple] = {}
    for v in vols:
        b, f = tot[v.key]
        for o in vols:
            if o is not v and o.root != v.root and o.root.startswith(v.root):
                b -= tot[o.key][0]
                f -= tot[o.key][1]
        out[v.key] = (max(0, f), max(0, b))
    return out


def _bz_drive_key(path: str) -> str:
    """Match a disk with a Backblaze volume: the drive letter, or the mount without its trailing "/"."""
    if len(path) > 1 and path[1] == ":":
        return path[:1].upper()
    return path.rstrip("/") or "/"


def _newest_snapshot() -> str:
    """Newest snapshot start_time across both repos (small `indexed_snapshots` table = instant).
    Used only to label not-backed-up files 'new' (mtime newer than any snapshot) vs 'excluded'."""
    best = ""
    for repo in coverage_repos():
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
    folder counts as 'backed' when its excluded fraction is below `tolerance` (cuts partial noise).
    A folder inside a mapped place (Settings > Disk scanning) is compared with its backed-up path."""
    sep, fold = _rules(folder)
    ckey = (_key(folder, fold), tolerance)
    cached = _list_cache_get(ckey)
    if cached and time.time() - cached[0] < _LIST_TTL:
        return cached[1]  # repeated navigation is instant (within TTL)
    disk = es_children(folder)
    mapped = mapping_for(folder)
    if mapped:
        # Another computer's files: its own path rules, and Backblaze (this PC only) doesn't apply.
        backed = kopia_backed_children(mapped["backup_folder"], mapped["computer"])
        ifold = _rules(mapped["backup_folder"])[1]
    else:
        backed = backed_children(folder)
        ifold = fold
    newest = _newest_snapshot()
    items: list[dict] = []
    for d in disk:
        seg = _key(d["name"], ifold)
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
            if d["size"] is None:   # the disk side isn't known: being counted, not counted, another disk
                status = {"counting": "counting", "other_disk": "other_disk"}.get(d.get("state"), "not_counted")
                excl, frac = 0, 0
            elif db == 0:
                status = "empty"   # nothing on disk to back up
            else:
                status = "excluded" if bb == 0 else ("backed" if frac <= tolerance else "partial")
            items.append({
                "name": d["name"], "is_dir": True, "disk_bytes": db, "backed_bytes": bb,
                "excluded_bytes": excl, "pct_excluded": round(frac * 100, 1) if d["size"] is not None else None,
                "backed_files": b["files"] if b else 0, "last_backed": last_backed,
                "repo": repo_label, "bz_backed_bytes": bz_b, "bz_backed_files": bz_f,
                "status": status, "mtime": d["mtime"], "_disk_files": d.get("files"),
                "counted_at": d.get("counted_at"),
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
    # Recursive FILE counts: direct files = 1; the scanner counts files with sizes; Everything
    # answers per child folder (parallel count queries, instant). excluded_files = total - backed.
    base = folder.rstrip(sep)
    dirs = [it for it in items if it["is_dir"]]
    everything = disklookup.provider() == "everything"

    def _cnt(it):
        if it["_disk_files"] is not None:
            return it["_disk_files"]
        if not everything or it["status"] in ("counting", "not_counted", "other_disk"):
            return None
        return recursive_count(base + sep + it["name"])  # None when Everything could not count

    counts = list(ThreadPoolExecutor(max_workers=16).map(_cnt, dirs)) if dirs else []
    cmap = {id(it): n for it, n in zip(dirs, counts)}
    for it in items:
        it.pop("_disk_files", None)
        it["total_size"] = it["disk_bytes"]
        tf = (cmap.get(id(it)) or 0) if it["is_dir"] else 1
        it["total_files"] = tf
        it["excluded_files"] = max(0, tf - it["backed_files"])

    items.sort(key=lambda x: (not x["is_dir"], -x["excluded_bytes"], x["name"].lower()))
    stamps = [i["counted_at"] for i in items if i.get("counted_at")]
    result = {
        "folder": folder,
        "items": items,
        "total_excluded": sum(i["excluded_bytes"] for i in items),
        "total_backed": sum(i["backed_bytes"] for i in items),
        "count": len(items),
        "counting": sum(1 for i in items if i["status"] == "counting"),
        "not_counted": sum(1 for i in items if i["status"] == "not_counted"),
        "counted_at": min(stamps) if stamps else None,
        "notes": disklookup.notes_for(folder),
        "not_indexed": everything and not disk and disklookup.not_indexed(folder),
        "backed_up_as": ({"path": mapped["backup_folder"], "computer": mapped["computer"]} if mapped else None),
    }
    if not result["counting"]:   # a listing still being counted is asked again shortly
        _list_cache_put(ckey, (time.time(), result))
    return result


# drive -> (generation key, (backed bytes, backed files)). The index side of a drive total only
# changes when a new index is published; the disk side is read fresh on every call.
_DRIVE_BACKED: dict[str, tuple] = {}


def _generation_key() -> tuple:
    out = []
    for repo in coverage_repos():
        try:
            out.append((repo, os.stat(manifest_path(repo)).st_mtime_ns))
        except OSError:
            out.append((repo, None))
    return tuple(out)


def _drive_backed(p: str, gen: tuple, computer: str | None = None) -> tuple[int, int]:
    hit = _DRIVE_BACKED.get((p, computer))
    if hit and hit[0] == gen:
        return hit[1]
    lo, hi, _plen = _prefix_bounds(p)
    path = _path_sql(_rules(p)[1])
    bs = bc = 0
    for repo in coverage_repos():
        join, cond, cparams = _computer_sql(_repo_computer(repo, computer))
        cur = read_cursor(repo)
        try:
            row = cur.execute(
                f"SELECT sum(sz), count(*) FROM (SELECT max(p.size) AS sz FROM pathx p {join} "
                f"WHERE p.in_latest AND {path} >= ? AND {path} < ?{cond} GROUP BY {path})",
                [lo, hi, *cparams],
            ).fetchone()
        finally:
            cur.close()
        bs += int(row[0] or 0)
        bc += int(row[1] or 0)
    if all(mt is not None for _r, mt in gen):
        _DRIVE_BACKED[(p, computer)] = (gen, (bs, bc))
    return bs, bc


def drive_stats() -> list[dict]:
    """Per local drive: total (Everything) vs backed (index, deduped) → excluded size + file count.
    Parallel es calls. Used by the drives ('This PC') view so each drive shows its REAL excluded
    total, not its used-disk size."""
    drives = list_drives()  # [{path, used, total}]
    bz_by_drive: dict[str, tuple] = {}
    try:
        from ..backblaze import paths as bzp, queries as bzq
        if bzp.is_this_computer():   # another computer's Backblaze doesn't describe these disks
            for r in bzq.overview()["rows"]:
                bz_by_drive[_bz_drive_key(r["root"])] = (r["backed_files"], r["backed_bytes"])
    except Exception:  # bzdata missing / BZ index not built - drive view still works without it
        pass

    gen = _generation_key()

    def _one(d: dict) -> dict:
        p = d["path"]
        tot = drive_totals(p)   # never starts a count just by looking (unless the user asked for that)
        ts, tc = tot["size"], tot["files"]
        place = d.get("place")
        mapped = mapping_for(p) if place else None
        with lanes.heavy():         # one index query per drive in parallel can outgrow a small limit
            if mapped:
                bs, bc = _drive_backed(mapped["backup_folder"], gen, mapped["computer"])
                bzf = bzb = 0
            else:
                bs, bc = _drive_backed(p, gen)
                bzf, bzb = bz_by_drive.get(_bz_drive_key(p), (0, 0)) if not place else (0, 0)
        return {
            "path": p, "used": d["used"], "total_size": ts, "total_files": tc,
            "counting": tot["state"] == "counting", "state": tot["state"], "counted_at": tot.get("counted_at"),
            "backed_bytes": bs, "backed_files": bc,
            "excluded_bytes": max(0, (ts or 0) - bs), "excluded_files": max(0, (tc or 0) - bc),
            "bz_backed_bytes": bzb, "bz_backed_files": bzf,
            "place": bool(place),
            "backed_up_as": ({"path": mapped["backup_folder"], "computer": mapped["computer"]} if mapped else None),
        }

    ctx = contextvars.copy_context()        # the request's lane, so the drive queries take turns
    return list(ThreadPoolExecutor(max_workers=8).map(lambda d: ctx.copy().run(_one, d), drives)) if drives else []
