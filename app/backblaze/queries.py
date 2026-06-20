"""Read queries over the Backblaze SQLite index. Pure reads; safe to call concurrently (WAL)."""

from __future__ import annotations

import json

from . import bbdb, paths

_SEP = "\\"

_SORT_COLS = {"size": "size", "name": "name", "path": "path_lc", "drive": "drive", "ext": "ext"}


# ---- search (Everything-style substring) ----------------------------------------------
def _where(query, drive, ext, status, min_size):
    sql, params = [], []
    if query and len(query) >= 3 and bbdb.fts_available():
        sql.append("rowid IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)")
        params.append('"' + query.replace('"', '""') + '"')
    elif query:
        sql.append("path_lc LIKE ?")
        params.append("%" + query.lower() + "%")
    if drive:
        sql.append("drive = ?"); params.append(drive.upper())
    if ext:
        sql.append("ext = ?"); params.append(ext.lower().lstrip("."))
    if status == "backed":
        sql.append("excluded = 0")
    elif status == "excluded":
        sql.append("excluded = 1")
    if min_size:
        sql.append("size >= ?"); params.append(int(min_size))
    return (" WHERE " + " AND ".join(sql)) if sql else "", params


def search(query="", drive=None, ext=None, status="all", min_size=0,
           sort="size", direction="desc", limit=500, offset=0) -> dict:
    con = bbdb.connect()
    where, params = _where(query, drive, ext, status, min_size)
    total = con.execute(f"SELECT count(*) FROM files{where}", params).fetchone()[0]
    col = _SORT_COLS.get(sort, "size")
    dir_ = "DESC" if str(direction).lower() != "asc" else "ASC"
    rows = con.execute(
        f"SELECT drive, full_path, name, ext, size, excluded, excl_rule FROM files{where} "
        f"ORDER BY {col} {dir_} LIMIT ? OFFSET ?", params + [int(limit), int(offset)]).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


def export_iter(query="", drive=None, ext=None, status="all", min_size=0,
                sort="size", direction="desc"):
    con = bbdb.connect()
    where, params = _where(query, drive, ext, status, min_size)
    col = _SORT_COLS.get(sort, "size")
    dir_ = "DESC" if str(direction).lower() != "asc" else "ASC"
    return con.execute(
        f"SELECT drive, full_path, name, ext, size, excluded, excl_rule FROM files{where} "
        f"ORDER BY {col} {dir_}", params)


# ---- per-drive overview (vs Backblaze authoritative) ----------------------------------
def overview() -> dict:
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
def group_by_folder(drive=None, depth=2, status="backed", min_files=1, top=300) -> list[dict]:
    con = bbdb.connect()
    where, params = _where("", drive, None, status, 0)
    counts: dict[str, list] = {}
    for full_path, size in con.execute(f"SELECT full_path, size FROM files{where}", params):
        comps = full_path.split(_SEP)
        key = _SEP.join(comps[:depth + 1]) if len(comps) > depth else full_path
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
def sessions(limit=200) -> dict:
    con = bbdb.connect()
    ses = [dict(r) for r in con.execute(
        "SELECT ts, kind, files, bytes, detail, src FROM events "
        "WHERE kind IN ('session_start','session_end') ORDER BY ts DESC LIMIT ?", [limit])]
    uploads = [dict(r) for r in con.execute(
        "SELECT ts AS date, files AS success, detail FROM events WHERE kind='upload_day' ORDER BY ts DESC LIMIT ?",
        [limit])]
    deletions = [dict(r) for r in con.execute(
        "SELECT ts, bytes, detail, src FROM events WHERE kind='bz_done_big' ORDER BY ts DESC")]
    return {"sessions": ses, "uploads": uploads, "deletion_events": deletions}


# ---- queue ----------------------------------------------------------------------------
def queue(depth=1) -> dict:
    con = bbdb.connect()
    by_drive = [dict(r) for r in con.execute(
        "SELECT drive, count(*) files, sum(size) bytes FROM queue GROUP BY drive ORDER BY drive")]
    folders: dict[str, list] = {}
    for full_path, size in con.execute("SELECT full_path, size FROM queue"):
        comps = full_path.split(_SEP)
        key = _SEP.join(comps[:depth + 1]) if len(comps) > depth else full_path
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
