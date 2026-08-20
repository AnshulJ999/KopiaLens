"""Read queries over the Backblaze SQLite index. Pure reads; safe to call concurrently (WAL)."""

from __future__ import annotations

import json
import threading
import time

from . import bbdb, parsers, paths

_SEP = "\\"
_CHURN_TAIL = 30 * 1024 * 1024  # bytes of the bzfileids tail scanned for "recently added"
_CHURN_CACHE: dict = {}         # (top, depth, collapse_key) -> (ts, result)


def _apply_collapse(path: str, depth: int, collapse_lc):
    """Roll a path up to a folder key at `depth`, EXCEPT paths under a collapse prefix (e.g.
    `E:\\Kopia`) which roll up to that prefix as a single unit. `collapse_lc` = [(lower, original)]."""
    pl = path.lower()
    for clc, corig in collapse_lc:
        if pl == clc or pl.startswith(clc + _SEP):
            return corig
    comps = path.split(_SEP)
    return _SEP.join(comps[:depth + 1]) if len(comps) > depth else path

_SORT_COLS = {"size": "size", "name": "name", "path": "path_lc", "drive": "drive", "ext": "ext"}


# ---- search (Everything-style substring) ----------------------------------------------
def _ext_list(raw):
    return [e for e in (x.strip().lower().lstrip(".") for x in str(raw).split(",")) if e] if raw else []


def _where(query, drive, ext, status, min_size, ext_exclude=None):
    sql, params = [], []
    if query and len(query) >= 3 and bbdb.fts_available():
        sql.append("rowid IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)")
        params.append('"' + query.replace('"', '""') + '"')
    elif query:
        sql.append("path_lc LIKE ?")
        params.append("%" + query.lower() + "%")
    if drive:
        sql.append("drive = ?"); params.append(drive.upper())
    exts = _ext_list(ext)                       # multi-ext include (comma list)
    if exts:
        sql.append("ext IN (%s)" % ",".join("?" * len(exts))); params += exts
    exx = _ext_list(ext_exclude)                # multi-ext exclude (matches Kopia's hide-ext)
    if exx:
        sql.append("ext NOT IN (%s)" % ",".join("?" * len(exx))); params += exx
    if status == "backed":
        sql.append("excluded = 0")
    elif status == "excluded":
        sql.append("excluded = 1")
    if min_size:
        sql.append("size >= ?"); params.append(int(min_size))
    return (" WHERE " + " AND ".join(sql)) if sql else "", params


def search(query="", drive=None, ext=None, status="all", min_size=0,
           sort="size", direction="desc", limit=500, offset=0, ext_exclude=None) -> dict:
    con = bbdb.connect()
    where, params = _where(query, drive, ext, status, min_size, ext_exclude)
    total = con.execute(f"SELECT count(*) FROM files{where}", params).fetchone()[0]
    col = _SORT_COLS.get(sort, "size")
    dir_ = "DESC" if str(direction).lower() != "asc" else "ASC"
    rows = con.execute(
        f"SELECT drive, full_path, name, ext, size, excluded, excl_rule FROM files{where} "
        f"ORDER BY {col} {dir_} LIMIT ? OFFSET ?", params + [int(limit), int(offset)]).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


def export_iter(query="", drive=None, ext=None, status="all", min_size=0,
                sort="size", direction="desc", ext_exclude=None):
    con = bbdb.connect()
    where, params = _where(query, drive, ext, status, min_size, ext_exclude)
    col = _SORT_COLS.get(sort, "size")
    dir_ = "DESC" if str(direction).lower() != "asc" else "ASC"
    return con.execute(
        f"SELECT drive, full_path, name, ext, size, excluded, excl_rule FROM files{where} "
        f"ORDER BY {col} {dir_}", params)


# ---- per-drive overview (vs Backblaze authoritative) ----------------------------------
def overview() -> dict:
    """Instant-open reader: serves the kv snapshot written at the end of the last refresh() (the
    `files` table only changes during a refresh, so the aggregate stays exact between them). Falls
    back to the live full-table scan when no cache exists yet (first run / pre-Pass-B DB)."""
    con = bbdb.connect()
    cached = bbdb.kv_get(con, "cache_overview")
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass
    out = _overview_live()
    out["cached_at"] = None
    return out


def _overview_live() -> dict:
    con = bbdb.connect()
    auth = {}
    raw = bbdb.kv_get(con, "totalbackup")
    if raw:
        d = json.loads(raw)
        per_guid = d.get("per_guid", {})
        guid_letter = {v.guid: v.letter for v in paths.volumes()}
        for guid, (nf, nb) in per_guid.items():
            L = guid_letter.get(guid)
            if L:
                auth[L] = (nf, nb)
    rows = []
    for r in con.execute(
        "SELECT drive, count(*) total, "
        "sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END) backed_files, "
        "sum(CASE WHEN excluded=0 THEN size ELSE 0 END) backed_bytes, "
        "sum(CASE WHEN excluded=1 THEN 1 ELSE 0 END) excluded_files, "
        "sum(CASE WHEN excluded=1 THEN size ELSE 0 END) excluded_bytes "
        "FROM files GROUP BY drive ORDER BY drive").fetchall():
        a = auth.get(r["drive"])
        rows.append({
            "drive": r["drive"], "total": r["total"],
            "backed_files": r["backed_files"] or 0, "backed_bytes": r["backed_bytes"] or 0,
            "excluded_files": r["excluded_files"] or 0, "excluded_bytes": r["excluded_bytes"] or 0,
            "auth_files": a[0] if a else None, "auth_bytes": a[1] if a else None,
            "diff": (r["backed_files"] or 0) - a[0] if a else None,
        })
    grand = json.loads(bbdb.kv_get(con, "totalbackup") or "{}").get("grand", [0, 0])
    return {"rows": rows, "auth_grand_files": grand[0], "auth_grand_bytes": grand[1]}


# ---- folder rollup (bloat finder) -----------------------------------------------------
def group_by_folder(drive=None, depth=2, status="backed", min_files=1, top=300, collapse=None) -> list[dict]:
    con = bbdb.connect()
    where, params = _where("", drive, None, status, 0)
    cl = [(c.lower(), c) for c in (collapse or [])]
    counts: dict[str, list] = {}
    for full_path, size in con.execute(f"SELECT full_path, size FROM files{where}", params):
        key = _apply_collapse(full_path, depth, cl)
        e = counts.get(key)
        if e:
            e[0] += 1; e[1] += size
        else:
            counts[key] = [1, size]
    rows = [{"folder": k, "files": v[0], "bytes": v[1]} for k, v in counts.items() if v[0] >= min_files]
    rows.sort(key=lambda x: x["bytes"], reverse=True)
    return rows[:top]


# ---- exclusions + impact --------------------------------------------------------------
def exclusions() -> dict:
    from . import rules
    con = bbdb.connect()
    layers = rules.list_layers()
    impact = [{"rule": r["excl_rule"], "files": r["c"], "bytes": r["b"]} for r in con.execute(
        "SELECT excl_rule, count(*) c, sum(size) b FROM files WHERE excluded=1 AND excl_rule IS NOT NULL "
        "GROUP BY excl_rule ORDER BY b DESC")]
    matched = {i["rule"] for i in impact}
    dormant = [d for d in layers["folders"] if f"folder:{d}" not in matched]
    return {"layers": layers, "impact": impact, "dormant_folders": dormant}


def generate_exclusion(kind: str, value: str) -> dict:
    """Generate a paste-ready exclusion (NO file write). kind: 'editable_endswith' | 'editable_contains' | 'folder'."""
    value = value.strip()
    if kind == "folder":
        return {"how": "Backblaze app -> Settings -> Exclusions -> add this folder (relative, applies to all drives)",
                "snippet": value}
    crit = {"editable_endswith": ("endsWith", value.lower()),
            "editable_contains": ("contains_1", value.lower())}.get(kind)
    if not crit:
        return {"error": f"unknown kind {kind!r}"}
    key, val = crit
    attrs = {"plat": "win", "osVers": "*", "ruleIsOptional": "t",
             "skipFirstCharThenStartsWith": "*", "contains_1": "*", "contains_2": "*",
             "doesNotContain": "*", "endsWith": "*", "hasFileExtension": "*"}
    attrs[key] = val
    snippet = "<excludefname_rule " + " ".join(f'{k}="{v}"' for k, v in attrs.items()) + " />"
    return {"how": "Paste into bzexcluderules_editable.xml (advanced; the file is designed to be edited). "
                   "Then restart Backblaze. KopiaLens does NOT write this for you (read-only).",
            "snippet": snippet}


# ---- sessions / history ----------------------------------------------------------------
def sessions(limit=400) -> dict:
    con = bbdb.connect()
    # live session events (last ~day) UNION the permanent imported archive (~1 year); dedupe overlap
    ses = [dict(r) for r in con.execute(
        "SELECT ts, kind, files, bytes, detail, src FROM ("
        "  SELECT ts, kind, files, bytes, detail, src FROM events WHERE kind IN ('session_start','session_end') "
        "  UNION ALL SELECT ts, kind, files, bytes, detail, src FROM events_archive) "
        "GROUP BY ts, kind ORDER BY ts DESC LIMIT ?", [limit])]
    # upload_day rows store success in `files` and fail count in `bytes` (repurposed slot)
    uploads = [dict(r) for r in con.execute(
        "SELECT ts AS date, files AS success, bytes AS fail, detail FROM events WHERE kind='upload_day' "
        "ORDER BY ts DESC LIMIT ?", [limit])]
    deletions = [dict(r) for r in con.execute(
        "SELECT ts, bytes, detail, src FROM events WHERE kind='bz_done_big' ORDER BY ts DESC")]
    # Per-day totals from bz_done - the real "how much was uploaded" figure. The session rows above
    # remain useful for WHEN backups ran, but their byte counts come from END_BACKUP lines that
    # Backblaze rarely writes (see parsers.parse_bz_done_day_totals).
    days = [dict(r) for r in con.execute(
        "SELECT day, sum(files) AS files, sum(bytes) AS bytes FROM backup_days "
        "GROUP BY day ORDER BY day DESC LIMIT ?", [limit])]
    total_30 = con.execute(
        "SELECT COALESCE(sum(files), 0), COALESCE(sum(bytes), 0) FROM backup_days "
        "WHERE day >= date('now', '-30 day')").fetchone()
    return {"sessions": ses, "uploads": uploads, "deletion_events": deletions,
            "backup_days": days,
            "last_30d": {"files": total_30[0] or 0, "bytes": total_30[1] or 0}}


# ---- queue ----------------------------------------------------------------------------
def queue(depth=1, collapse=None) -> dict:
    con = bbdb.connect()
    by_drive = [dict(r) for r in con.execute(
        "SELECT drive, count(*) files, sum(size) bytes FROM queue GROUP BY drive ORDER BY drive")]
    cl = [(c.lower(), c) for c in (collapse or [])]
    folders: dict[str, list] = {}
    for full_path, size in con.execute("SELECT full_path, size FROM queue"):
        key = _apply_collapse(full_path, depth, cl)
        e = folders.get(key)
        if e:
            e[0] += 1; e[1] += size
        else:
            folders[key] = [1, size]
    top = sorted(({"folder": k, "files": v[0], "bytes": v[1]} for k, v in folders.items()),
                 key=lambda x: x["bytes"], reverse=True)[:150]
    tot = con.execute("SELECT count(*), coalesce(sum(size),0) FROM queue").fetchone()
    return {"total_files": tot[0], "total_bytes": tot[1], "by_drive": by_drive, "by_folder": top}


# ---- health ---------------------------------------------------------------------------
def health() -> dict:
    con = bbdb.connect()
    issues: list[str] = []
    ov = overview()
    for r in ov["rows"]:
        if (r["auth_files"] == 0) or (r["total"] == 0):
            issues.append(f"Drive {r['drive']}: 0 files selected (possible aborted scan / F:-bug)")
    for r in con.execute("SELECT detail FROM events WHERE kind='scan_stub'"):
        issues.append(r["detail"])
    for r in con.execute("SELECT detail, src FROM events WHERE kind='bz_done_big' AND detail LIKE '%ACTIVE%'"):
        issues.append(f"{r['src']}: {r['detail']} mass-deletion event")
    remaining = json.loads(bbdb.kv_get(con, "remaining") or "{}")
    return {"issues": issues, "ok": not issues, "drives": ov["rows"], "remaining": remaining}


# ---- duplicate candidates (heuristic, NOT BZ's real server-side dedup) -----------------
def dupes(min_size=50 * 1_000_000, top=200) -> list[dict]:
    con = bbdb.connect()
    return [dict(r) for r in con.execute(
        "SELECT name, size, count(*) copies, size*count(*) wasted FROM files "
        "WHERE excluded=0 AND size>=? GROUP BY name, size HAVING copies>1 "
        "ORDER BY wasted DESC LIMIT ?", [int(min_size), int(top)])]


# ---- coverage for the Excluded browser merge (called by index/excluded.py BZ branch) ---
def coverage_children(folder: str) -> dict[str, dict]:
    """Per immediate child segment under `folder`: backed (excluded=0) coverage from the BZ index.
    Shape mirrors index/excluded.py's backed_children: {seg_lc: {logical, files, has_file}}."""
    con = bbdb.connect()
    lo = folder.lower().rstrip(_SEP) + _SEP
    hi = lo + "￿"
    plen = len(lo)
    agg: dict[str, dict] = {}
    for r in con.execute(
        "SELECT seg, max(has_file) has_file, sum(size) logical, count(*) cnt FROM ("
        "  SELECT substr(rem,1,CASE WHEN instr(rem,?)>0 THEN instr(rem,?)-1 ELSE length(rem) END) seg,"
        "         (instr(rem,?)=0) has_file, size FROM ("
        "    SELECT substr(path_lc, ?) rem, size FROM files"
        "    WHERE excluded=0 AND path_lc>=? AND path_lc<?))"
        " WHERE seg<>'' GROUP BY seg",
        [_SEP, _SEP, _SEP, plen + 1, lo, hi]).fetchall():
        agg[r["seg"]] = {"logical": int(r["logical"] or 0), "files": int(r["cnt"] or 0),
                         "has_file": bool(r["has_file"])}
    return agg


# ---- folder children (powers the Tree tab + the BZ-native Excluded folder drill) -------
def _rollup_to_children(rr) -> list[dict]:
    out = []
    for r in rr:
        bf, ef = r["backed_files"] or 0, r["excluded_files"] or 0
        bb, eb = r["backed_bytes"] or 0, r["excluded_bytes"] or 0
        is_dir = bool(r["is_dir"])
        status = ("backed" if bf else "excluded") if not is_dir else (
            "excluded" if bf == 0 else ("backed" if ef == 0 else "mixed"))
        out.append({"name": r["name"], "is_dir": is_dir, "total_files": bf + ef,
                    "backed_files": bf, "backed_bytes": bb, "excluded_files": ef, "excluded_bytes": eb,
                    "total_bytes": bb + eb, "status": status, "excl_rule": r["excl_rule"]})
    out.sort(key=lambda x: (not x["is_dir"], -x["total_bytes"], x["name"].lower()))
    return out


def folder_children(prefix: str) -> list[dict]:
    """Immediate children under `prefix` with the full backed/excluded split per child. Serves the
    Tree browser + the BZ Excluded coverage drill. Shallow levels (drive roots, big folders) come
    from the precomputed folder_rollup (instant); deeper levels use a fast SQL-optimized live scan
    (one substr + one instr per row, original-case name from a representative path)."""
    con = bbdb.connect()
    lo = prefix.lower().rstrip(_SEP) + _SEP
    rr = con.execute(
        "SELECT child_lc, name, is_dir, backed_files, backed_bytes, excluded_files, excluded_bytes, excl_rule "
        "FROM folder_rollup WHERE parent_lc=?", [lo]).fetchall()
    if rr:
        return _rollup_to_children(rr)
    hi = lo + "￿"
    plen = len(lo)
    rows = con.execute(
        "SELECT seg_lc, min(full_path) fp, max(is_file) is_file, "
        "sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END) backed_files, "
        "sum(CASE WHEN excluded=0 THEN size ELSE 0 END) backed_bytes, "
        "sum(CASE WHEN excluded=1 THEN 1 ELSE 0 END) excluded_files, "
        "sum(CASE WHEN excluded=1 THEN size ELSE 0 END) excluded_bytes, "
        "max(excl_rule) sample_rule FROM ("
        "  SELECT CASE WHEN p>0 THEN substr(remlc,1,p-1) ELSE remlc END seg_lc, (p=0) is_file, "
        "         full_path, excluded, size, excl_rule FROM ("
        "    SELECT remlc, instr(remlc, ?) p, full_path, excluded, size, excl_rule FROM ("
        "      SELECT substr(path_lc, ?) remlc, full_path, excluded, size, excl_rule "
        "      FROM files WHERE path_lc>=? AND path_lc<?)))"
        " WHERE seg_lc<>'' GROUP BY seg_lc",
        [_SEP, plen + 1, lo, hi]).fetchall()
    out = []
    for r in rows:
        bf, ef = r["backed_files"] or 0, r["excluded_files"] or 0
        bb, eb = r["backed_bytes"] or 0, r["excluded_bytes"] or 0
        is_file = bool(r["is_file"])
        rem = (r["fp"] or "")[plen:]
        si = rem.find(_SEP)
        name = rem if si < 0 else rem[:si]
        status = ("backed" if bf else "excluded") if is_file else (
            "excluded" if bf == 0 else ("backed" if ef == 0 else "mixed"))
        out.append({"name": name, "is_dir": not is_file, "total_files": bf + ef,
                    "backed_files": bf, "backed_bytes": bb, "excluded_files": ef, "excluded_bytes": eb,
                    "total_bytes": bb + eb, "status": status, "excl_rule": r["sample_rule"]})
    out.sort(key=lambda x: (not x["is_dir"], -x["total_bytes"], x["name"].lower()))
    return out


# ---- naming-gap hunter (excluded in one spot, NOT in another) --------------------------
def naming_gap(query: str, depth: int = 2, top: int = 300) -> list[dict]:
    """Every folder (depth-N) whose path contains `query`, with its backed-vs-excluded split.
    Surfaces 'you excluded \\google takeout\\ but F:\\GoogleTakeout is still backed up' gaps."""
    if not query or not query.strip():
        return []
    con = bbdb.connect()
    q = query.strip().lower()
    if len(q) >= 3 and bbdb.fts_available():
        rows = con.execute(
            "SELECT full_path, size, excluded FROM files WHERE rowid IN "
            "(SELECT rowid FROM files_fts WHERE files_fts MATCH ?)",
            ['"' + q.replace('"', '""') + '"']).fetchall()
    else:
        rows = con.execute(
            "SELECT full_path, size, excluded FROM files WHERE path_lc LIKE ?", ["%" + q + "%"]).fetchall()
    agg: dict[str, dict] = {}
    for full_path, size, excluded in rows:
        comps = full_path.split(_SEP)
        key = _SEP.join(comps[:depth + 1]) if len(comps) > depth else full_path
        e = agg.get(key)
        if not e:
            e = agg[key] = {"folder": key, "backed_files": 0, "backed_bytes": 0,
                            "excluded_files": 0, "excluded_bytes": 0}
        if excluded:
            e["excluded_files"] += 1; e["excluded_bytes"] += size
        else:
            e["backed_files"] += 1; e["backed_bytes"] += size
    out = list(agg.values())
    for e in out:
        bf, ef = e["backed_files"], e["excluded_files"]
        e["status"] = "excluded" if bf == 0 else ("backed" if ef == 0 else "mixed")
        e["total_files"] = bf + ef
        e["total_bytes"] = e["backed_bytes"] + e["excluded_bytes"]
    out.sort(key=lambda x: x["total_bytes"], reverse=True)
    return out[:top]


# ---- churn (recently-added folders = early warning for a new high-churn folder) ---------
def _churn_recompute(depth: int, collapse_lc) -> dict:
    """Roll the recent bzfileids tail up to folder keys at `depth` (collapse-aware)."""
    fp = paths.fileids_path()
    size = parsers.fileids_total_size(fp)
    counts: dict = {}
    if not size:
        return counts
    for path in parsers.iter_fileids_from(fp, max(0, size - _CHURN_TAIL)):
        k = _apply_collapse(path, depth, collapse_lc)
        counts[k] = counts.get(k, 0) + 1
    return counts


def _default_collapse_pairs() -> list:
    """The collapse list `refresh()` used to precompute cache_churn_default, as [(lower, original)]
    pairs (the shape _apply_collapse/_churn_recompute expect)."""
    try:
        from ..settings import load_settings
        return [(c.lower(), c) for c in (load_settings().get("backblaze", {}).get("collapse_prefixes", []) or [])]
    except Exception:
        return []


def _default_collapse_lc() -> list:
    """Sorted lowercased default collapse list, for matching a request against that default (must
    match exactly - a different collapse list means a different result)."""
    return sorted(c[0] for c in _default_collapse_pairs())


def _churn_build(top: int, depth: int, cl) -> dict:
    """The live compute path: roll the bzfileids tail up to folders at `depth`, then status/size/
    Delta-enrich the top N. Shared by churn()'s live path and the background revalidate thread."""
    raw = _churn_recompute(int(depth), cl)
    total = sum(raw.values()) or 1
    con = bbdb.connect()
    has_hist = (con.execute("SELECT count(DISTINCT day) FROM folder_snapshots").fetchone()[0] or 0) >= 2
    rows = []
    for folder, cnt in sorted(raw.items(), key=lambda x: x[1], reverse=True)[:int(top)]:
        drive = folder[0].upper() if len(folder) > 1 and folder[1] == ":" else ""
        lo = folder.lower().rstrip(_SEP) + _SEP
        hi = lo + "￿"
        st = con.execute(
            "SELECT count(*) t, sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END) b, "
            "sum(CASE WHEN excluded=0 THEN size ELSE 0 END) bb FROM files WHERE path_lc>=? AND path_lc<?",
            [lo, hi]).fetchone()
        tcnt, bcnt, bbytes = st["t"] or 0, st["b"] or 0, st["bb"] or 0
        status = "gone" if tcnt == 0 else ("active" if bcnt > 0 else "excluded")
        delta = None
        if has_hist:
            old = con.execute("SELECT files FROM folder_snapshots WHERE folder=? ORDER BY day ASC LIMIT 1",
                              [folder]).fetchone()
            if old is not None:
                delta = bcnt - (old[0] or 0)
        rows.append({"folder": folder, "count": cnt, "pct": round(100.0 * cnt / total, 1), "drive": drive,
                     "status": status, "backed_now": bcnt, "bytes_now": bbytes, "delta": delta})
    return {"total_recent": total, "rows": rows, "depth": int(depth)}


_CHURN_REVALIDATE_LOCK = threading.Lock()


def _churn_revalidate_default() -> None:
    """Single-flight background recompute of the default churn key (top=200, depth=2, settings
    collapse list). Serving the kv snapshot on a cold RAM cache is instant but freezes the Churn tab
    at refresh-time data for up to 4h; bzfileids.dat changes continuously between refreshes, so this
    kicks a live recompute in the background and seeds _CHURN_CACHE with the fresh result (kv itself
    stays refresh-owned - only refresh() writes cache_churn_default)."""
    if not _CHURN_REVALIDATE_LOCK.acquire(blocking=False):
        return  # a revalidate is already in flight - this call's kv-served response is fine as-is
    try:
        cl = _default_collapse_pairs()
        result = _churn_build(200, 2, cl)
        ckey = (200, 2, tuple(sorted(c[0] for c in cl)))
        _CHURN_CACHE[ckey] = (time.time(), result)
    except Exception:  # noqa: BLE001 - best-effort background refresh, never raise into the thread
        pass
    finally:
        _CHURN_REVALIDATE_LOCK.release()


def churn(top: int = 200, depth: int = 2, collapse=None) -> dict:
    """Top folders by recent fileID additions (bzfileids tail), recomputed on demand at the requested
    depth (1-8) with the collapse-list applied, then status/size/Delta-enriched. Cached per
    (top, depth, collapse) for 120s in RAM. For depth=2 + the settings collapse list + top<=200 (the
    Dashboard's top=40 call included), a cold RAM cache serves the kv snapshot written at the end of
    the last refresh() (instant) - but ALSO kicks a single-flight background live recompute of the
    full default key so a subsequent call within 120s gets fresh RAM data instead of staying pinned
    to refresh-time data for up to 4h. The user's #1 lever: catch a new churner before it balloons.

    status: active (still has backed files) / excluded (still scanned, all excluded) / gone (deleted).
    delta: backed-files change vs the OLDEST daily snapshot of that folder - auto-fills once >=2 days
    of folder_snapshots history exist (best at the default depth 2, where snapshots are keyed)."""
    cl = [(c.lower(), c) for c in (collapse or [])]
    ckey = (int(top), int(depth), tuple(sorted(c[0] for c in cl)))
    now = time.time()
    hit = _CHURN_CACHE.get(ckey)
    if hit and now - hit[0] < 120:
        return hit[1]
    if int(depth) == 2 and int(top) <= 200 and sorted(c[0] for c in cl) == _default_collapse_lc():
        cached = bbdb.kv_get(bbdb.connect(), "cache_churn_default")
        if cached:
            try:
                full = json.loads(cached)
                result = {**full, "rows": full["rows"][:int(top)]}  # fresh dict - never mutate the cached copy
                threading.Thread(target=_churn_revalidate_default, daemon=True).start()
                return result
            except Exception:
                pass
    result = _churn_build(int(top), int(depth), cl)
    _CHURN_CACHE[ckey] = (now, result)
    return result


# ---- assembled status XMLs (Dashboard cards + Stats) -----------------------------------
def stats() -> dict:
    con = bbdb.connect()

    def jget(k, default=None):
        raw = bbdb.kv_get(con, k)
        try:
            return json.loads(raw) if raw else default
        except Exception:
            return default

    tb = jget("totalbackup", {"grand": [0, 0], "per_guid": {}})
    grand = tb.get("grand", [0, 0])
    return {
        "grand_files": grand[0], "grand_bytes": grand[1],
        "filestats": jget("filestats", {}),
        "remaining": jget("remaining", {}),
        "last_completed": jget("last_completed", {}),
        "overview": jget("overview", {}),
        "zbytes_history": jget("zbytes_history", []),
        "fileids_total": jget("fileids_total", None),
    }


# ---- index status (built? counts? — the BZ analog of /api/index/status) -----------------
def status() -> dict:
    """Instant-open reader: serves the kv snapshot written at the end of the last refresh(). Falls
    back to a live GROUP BY scan when no cache exists yet (first run / pre-Pass-B DB). Never blocks
    on a refresh in progress - the heavy work runs in a separate worker process/connection."""
    con = bbdb.connect()
    cached = bbdb.kv_get(con, "cache_status")
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass
    out = _status_live()
    out["cached_at"] = None
    return out


def _status_live() -> dict:
    con = bbdb.connect()
    rows = con.execute(
        "SELECT drive, count(*) total, sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END) backed "
        "FROM files GROUP BY drive ORDER BY drive").fetchall()
    drives = [{"drive": r["drive"], "total": r["total"], "backed": r["backed"] or 0} for r in rows]
    return {"built": bool(drives), "drives": drives,
            "total_files": sum(d["total"] for d in drives),
            "backed_files": sum(d["backed"] for d in drives)}
