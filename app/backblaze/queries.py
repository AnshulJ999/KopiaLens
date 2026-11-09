"""Read queries over the Backblaze index. Pure reads; safe to call concurrently.

The `files` table reads from the ACTIVE store (): Parquet generations (`pq.py`, the default,
`backblaze.store` = "parquet") or the legacy SQLite `files` table + FTS5 ("sqlite", kept for
rollback). Everything else (kv, folder_snapshots, events_archive, backup_days, queue, events,
churn, folder_rollup) always reads the slim SQLite via bbdb.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time

from . import bbdb, parsers, paths, pq
from ..core.paths import prefix_bounds

_CHURN_TAIL = 30 * 1024 * 1024  # bytes of the bzfileids tail scanned for "recently added"


def _sep() -> str:
    """The data's own path separator ("\\" for a Windows bzdata, "/" for a Mac's)."""
    return paths.sep()


def _with_volumes(rows: list[dict]) -> list[dict]:
    """Add each row's volume `label` and `root` (served fresh, so old cached rows get them too)."""
    vols = paths.volume_info()
    info = {v["drive"]: v for v in vols}
    for r in rows:
        v = info.get(r.get("drive"))
        r["label"] = v["label"] if v else f"{r.get('drive')}:"
        r["root"] = v["root"] if v else f"{r.get('drive')}:\\"
    order = {v["drive"]: i for i, v in enumerate(vols)}   # drive letters, or a Mac's "/" first
    rows.sort(key=lambda r: (order.get(r.get("drive"), len(order)), str(r.get("drive"))))
    return rows
_CHURN_CACHE: dict = {}         # (top, depth, collapse_key) -> (ts, result)


# ---- active store for the `files` table (: "sqlite" | "parquet") -------------------
_STORE_OVERRIDE: str | None = None  # test hook - beats the setting


def _store() -> str:
    if _STORE_OVERRIDE:
        return _STORE_OVERRIDE
    try:
        from ..settings import load_settings
        return load_settings().get("backblaze", {}).get("store", "parquet")
    except Exception:  # noqa: BLE001 - a settings problem must not break reads
        return "parquet"


class _EmptyFiles:
    """Stands in for the Parquet `files` view before the first parquet build (the sqlite store's
    empty-table behavior): every read answers zero rows, count answers 0."""

    _DESCR = [("drive",), ("full_path",), ("path_lc",), ("name",), ("ext",),
              ("size",), ("mtime_hex",), ("excluded",), ("excl_rule",)]

    def __init__(self):
        self.description = self._DESCR

    def execute(self, _sql, _params=None):
        return self

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []

    def fetchmany(self, _n):
        return []


@contextlib.contextmanager
def _files_cursor(store: str | None = None):
    """Cursor over the `files` rows in the active store. sqlite: the thread-local bbdb connection
    (never closed here). parquet: a DuckDB cursor over the current generation (released on exit);
    an empty stand-in until the first parquet build lands."""
    store = store or _store()
    if store == "parquet":
        if not pq.available():
            yield _EmptyFiles()
            return
        with pq.files_cursor() as cur:
            yield cur
    else:
        yield bbdb.connect()


def _dict_rows(cur, rows) -> list[dict]:
    """sqlite Rows and DuckDB tuples -> dicts (callers and the API index columns by name)."""
    if not rows:
        return []
    if hasattr(rows[0], "keys"):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _apply_collapse(path: str, depth: int, collapse_lc):
    """Roll a path up to a folder key at `depth`, EXCEPT paths under a collapse prefix (e.g.
    `E:\\Kopia`) which roll up to that prefix as a single unit. `collapse_lc` = [(lower, original)]."""
    sep = _sep()
    pl = path.lower()
    for clc, corig in collapse_lc:
        if pl == clc or pl.startswith(clc + sep):
            return corig
    comps = path.split(sep)
    return sep.join(comps[:depth + 1]) if len(comps) > depth else path

_SORT_COLS = {"size": "size", "name": "name", "path": "path_lc", "drive": "drive", "ext": "ext"}


# ---- search (Everything-style substring) ----------------------------------------------
def _ext_list(raw):
    return [e for e in (x.strip().lower().lstrip(".") for x in str(raw).split(",")) if e] if raw else []


def _where(query, drive, ext, status, min_size, ext_exclude=None, store=None):
    store = store or _store()
    sql, params = [], []
    if query and len(query) >= 3 and store == "sqlite" and bbdb.fts_available():
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
    store = _store()
    where, params = _where(query, drive, ext, status, min_size, ext_exclude, store)
    col = _SORT_COLS.get(sort, "size")
    dir_ = "DESC" if str(direction).lower() != "asc" else "ASC"
    with _files_cursor(store) as fc:
        total = fc.execute(f"SELECT count(*) FROM files{where}", params).fetchone()[0]
        cur = fc.execute(
            f"SELECT drive, full_path, name, ext, size, excluded, excl_rule FROM files{where} "
            f"ORDER BY {col} {dir_} LIMIT ? OFFSET ?", params + [int(limit), int(offset)])
        rows = _dict_rows(cur, cur.fetchall())
    return {"total": total, "rows": rows}


def export_iter(query="", drive=None, ext=None, status="all", min_size=0,
                sort="size", direction="desc", ext_exclude=None):
    """Lazily streamed rows for the CSV export. sqlite: the cursor (Row objects). parquet: a
    generator of dicts over the current generation (held open for the stream's lifetime)."""
    store = _store()
    where, params = _where(query, drive, ext, status, min_size, ext_exclude, store)
    col = _SORT_COLS.get(sort, "size")
    dir_ = "DESC" if str(direction).lower() != "asc" else "ASC"
    sql = (f"SELECT drive, full_path, name, ext, size, excluded, excl_rule FROM files{where} "
           f"ORDER BY {col} {dir_}")
    if store == "parquet":
        if not pq.available():
            return iter(())

        def gen():
            with pq.files_cursor() as fc:
                cur = fc.execute(sql, params)
                cols = [d[0] for d in cur.description]
                while True:
                    batch = cur.fetchmany(50_000)
                    if not batch:
                        return
                    for t in batch:
                        yield dict(zip(cols, t))
        return gen()
    return bbdb.connect().execute(sql, params)


# ---- per-drive overview (vs Backblaze authoritative) ----------------------------------
def overview() -> dict:
    """Instant-open reader: serves the kv snapshot written at the end of the last refresh() (the
    `files` table only changes during a refresh, so the aggregate stays exact between them). Falls
    back to the live full-table scan when no cache exists yet (first run / pre-Pass-B DB)."""
    con = bbdb.connect()
    cached = bbdb.kv_get(con, "cache_overview")
    if cached:
        try:
            out = json.loads(cached)
            _with_volumes(out.get("rows") or [])
            return out
        except Exception:
            pass
    out = _overview_live()
    out["cached_at"] = None
    _with_volumes(out.get("rows") or [])
    return out


def _overview_live() -> dict:
    con = bbdb.connect()
    auth = {}
    raw = bbdb.kv_get(con, "totalbackup")
    if raw:
        d = json.loads(raw)
        per_guid = d.get("per_guid", {})
        guid_key = {v.guid: v.key for v in paths.volumes()}
        for guid, (nf, nb) in per_guid.items():
            L = guid_key.get(guid)
            if L:
                auth[L] = (nf, nb)
    with _files_cursor() as fc:
        cur = fc.execute(
            "SELECT drive, count(*) total, "
            "sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END) backed_files, "
            "sum(CASE WHEN excluded=0 THEN size ELSE 0 END) backed_bytes, "
            "sum(CASE WHEN excluded=1 THEN 1 ELSE 0 END) excluded_files, "
            "sum(CASE WHEN excluded=1 THEN size ELSE 0 END) excluded_bytes "
            "FROM files GROUP BY drive ORDER BY drive")
        agg = _dict_rows(cur, cur.fetchall())
    rows = []
    for r in agg:
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
    where, params = _where("", drive, None, status, 0)
    cl = [(c.lower(), c) for c in (collapse or [])]
    counts: dict[str, list] = {}
    with _files_cursor() as fc:
        cur = fc.execute(f"SELECT full_path, size FROM files{where}", params)
        while True:
            batch = cur.fetchmany(200_000)
            if not batch:
                break
            for full_path, size in batch:
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
    with _files_cursor() as fc:
        cur = fc.execute(
            "SELECT excl_rule, count(*) c, sum(size) b FROM files WHERE excluded=1 AND excl_rule IS NOT NULL "
            "GROUP BY excl_rule ORDER BY b DESC")
        impact = [{"rule": r["excl_rule"], "files": r["c"], "bytes": r["b"]}
                  for r in _dict_rows(cur, cur.fetchall())]
    matched = {i["rule"] for i in impact}
    dormant = [d for d in layers["folders"] if f"folder:{d}" not in matched]
    return {"layers": layers, "impact": impact, "dormant_folders": dormant}


def generate_exclusion(kind: str, value: str) -> dict:
    """Generate a paste-ready exclusion (NO file write). kind: 'editable_endswith' | 'editable_contains' | 'folder'."""
    value = value.strip()
    if kind == "folder":
        if paths.platform() == "mac":
            return {"how": "Backblaze -> Settings -> Exclusions -> add this folder", "snippet": value}
        return {"how": "Backblaze app -> Settings -> Exclusions -> add this folder (relative, applies to all drives)",
                "snippet": value}
    crit = {"editable_endswith": ("endsWith", value.lower()),
            "editable_contains": ("contains_1", value.lower())}.get(kind)
    if not crit:
        return {"error": f"unknown kind {kind!r}"}
    key, val = crit
    attrs = {"plat": "mac" if paths.platform() == "mac" else "win", "osVers": "*", "ruleIsOptional": "t",
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
            issues.append(f"Drive {r['label'].rstrip(':')}: 0 files selected (possible aborted scan / F:-bug)")
    for r in con.execute("SELECT detail FROM events WHERE kind='scan_stub'"):
        issues.append(r["detail"])
    for r in con.execute("SELECT detail, src FROM events WHERE kind='bz_done_big' AND detail LIKE '%ACTIVE%'"):
        issues.append(f"{r['src']}: {r['detail']} mass-deletion event")
    remaining = json.loads(bbdb.kv_get(con, "remaining") or "{}")
    return {"issues": issues, "ok": not issues, "drives": ov["rows"], "remaining": remaining,
            "version": client_version(con)}


def client_version(con=None) -> dict:
    """Installed vs newest Backblaze version (both read from Backblaze's own files at refresh).
    `outdated` only when both are known and the installed one is older."""
    con = con or bbdb.connect()
    try:
        v = json.loads(bbdb.kv_get(con, "client_version") or "{}")
    except Exception:  # noqa: BLE001
        v = {}
    inst, new = v.get("installed") or "", v.get("newest") or ""
    it, nt = parsers.version_tuple(inst), parsers.version_tuple(new)
    return {"installed": inst, "newest": new, "platform": v.get("platform") or paths.platform(),
            "outdated": bool(it and nt and it < nt)}


# ---- duplicate candidates (heuristic, NOT BZ's real server-side dedup) -----------------
def dupes(min_size=50 * 1_000_000, top=200) -> list[dict]:
    with _files_cursor() as fc:
        cur = fc.execute(
            "SELECT name, size, count(*) copies, size*count(*) wasted FROM files "
            "WHERE excluded=0 AND size>=? GROUP BY name, size HAVING copies>1 "
            "ORDER BY wasted DESC LIMIT ?", [int(min_size), int(top)])
        return _dict_rows(cur, cur.fetchall())


# ---- coverage for the Excluded browser merge (called by index/excluded.py BZ branch) ---
def coverage_children(folder: str) -> dict[str, dict]:
    """Per immediate child segment under `folder`: backed (excluded=0) coverage from the BZ index.
    Shape mirrors index/excluded.py's backed_children: {seg: {logical, files, has_file}}. Windows
    data is keyed case-folded (seg_lc); a Mac's by the exact names, which is how the Kopia side
    keys "/" paths (both read the same disk, so the names match)."""
    sep = _sep()
    fold = sep == "\\"
    col = "path_lc" if fold else "full_path"
    lo = (folder.lower() if fold else folder).rstrip(sep) + sep
    _, hi = prefix_bounds(lo)                 # not lo + U+FFFF: emoji sort above it
    plen = len(lo)
    agg: dict[str, dict] = {}
    with _files_cursor() as fc:
        cur = fc.execute(
            "SELECT seg, max(has_file) has_file, sum(size) logical, count(*) cnt FROM ("
            "  SELECT substr(rem,1,CASE WHEN instr(rem,?)>0 THEN instr(rem,?)-1 ELSE length(rem) END) seg,"
            "         (instr(rem,?)=0) has_file, size FROM ("
            f"    SELECT substr({col}, ?) rem, size FROM files"
            f"    WHERE excluded=0 AND {col}>=? AND {col}<?))"
            " WHERE seg<>'' GROUP BY seg",
            [sep, sep, sep, plen + 1, lo, hi])
        cov_rows = _dict_rows(cur, cur.fetchall())
    for r in cov_rows:
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
    sep = _sep()
    lo = prefix.lower().rstrip(sep) + sep
    rr = con.execute(
        "SELECT child_lc, name, is_dir, backed_files, backed_bytes, excluded_files, excluded_bytes, excl_rule "
        "FROM folder_rollup WHERE parent_lc=?", [lo]).fetchall()
    if rr:
        return _rollup_to_children(rr)
    _, hi = prefix_bounds(lo)                 #
    plen = len(lo)
    with _files_cursor() as fc:
        cur = fc.execute(
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
            [sep, plen + 1, lo, hi])
        rows = _dict_rows(cur, cur.fetchall())
    out = []
    for r in rows:
        bf, ef = r["backed_files"] or 0, r["excluded_files"] or 0
        bb, eb = r["backed_bytes"] or 0, r["excluded_bytes"] or 0
        is_file = bool(r["is_file"])
        rem = (r["fp"] or "")[plen:]
        si = rem.find(sep)
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
    q = query.strip().lower()
    store = _store()
    if len(q) >= 3 and store == "sqlite" and bbdb.fts_available():
        with _files_cursor(store) as fc:
            rows = fc.execute(
                "SELECT full_path, size, excluded FROM files WHERE rowid IN "
                "(SELECT rowid FROM files_fts WHERE files_fts MATCH ?)",
                ['"' + q.replace('"', '""') + '"']).fetchall()
    else:
        with _files_cursor(store) as fc:
            rows = fc.execute(
                "SELECT full_path, size, excluded FROM files WHERE path_lc LIKE ?", ["%" + q + "%"]).fetchall()
    agg: dict[str, dict] = {}
    sep = _sep()
    for full_path, size, excluded in rows:
        comps = full_path.split(sep)
        key = sep.join(comps[:depth + 1]) if len(comps) > depth else full_path
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


def _churn_folder_stats(folders: list[tuple[str, str, str]]) -> dict[str, tuple]:
    """Per-folder (total, backed, bytes) for churn enrichment; folders = [(folder, lo, hi)].
    sqlite: the per-folder indexed range query. parquet: one grouped range-join - a per-folder
    loop over the unindexed Parquet view would pay the scan floor once per folder."""
    if _store() == "parquet":
        if not folders:
            return {}
        if not pq.available():
            return {f: (0, 0, 0) for f, _lo, _hi in folders}
        vals = ", ".join("(?, ?, ?)" for _ in folders)
        params = [x for trip in folders for x in trip]
        with pq.files_cursor() as fc:
            rows = fc.execute(
                f"WITH r(folder, lo, hi) AS (VALUES {vals}) "
                "SELECT r.folder, count(f.path_lc) t, "
                "sum(CASE WHEN f.excluded=0 THEN 1 ELSE 0 END) b, "
                "sum(CASE WHEN f.excluded=0 THEN f.size ELSE 0 END) bb "
                "FROM r LEFT JOIN files f ON f.path_lc >= r.lo AND f.path_lc < r.hi "
                "GROUP BY r.folder", params).fetchall()
        got = {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0) for r in rows}
        return {f: got.get(f, (0, 0, 0)) for f, _lo, _hi in folders}
    con = bbdb.connect()
    out = {}
    for folder, lo, hi in folders:
        st = con.execute(
            "SELECT count(*) t, sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END) b, "
            "sum(CASE WHEN excluded=0 THEN size ELSE 0 END) bb FROM files WHERE path_lc>=? AND path_lc<?",
            [lo, hi]).fetchone()
        out[folder] = (st["t"] or 0, st["b"] or 0, st["bb"] or 0)
    return out


def _churn_build(top: int, depth: int, cl) -> dict:
    """The live compute path: roll the bzfileids tail up to folders at `depth`, then status/size/
    Delta-enrich the top N. Shared by churn()'s live path and the background revalidate thread."""
    raw = _churn_recompute(int(depth), cl)
    total = sum(raw.values()) or 1
    con = bbdb.connect()
    has_hist = (con.execute("SELECT count(DISTINCT day) FROM folder_snapshots").fetchone()[0] or 0) >= 2
    top_folders = sorted(raw.items(), key=lambda x: x[1], reverse=True)[:int(top)]
    folders = []
    sep = _sep()
    for folder, _cnt in top_folders:
        lo = folder.lower().rstrip(sep) + sep
        _, hi = prefix_bounds(lo)             #
        folders.append((folder, lo, hi))
    stats = _churn_folder_stats(folders)
    vols = paths.volumes()
    rows = []
    for folder, cnt in top_folders:
        drive = paths.volume_key_for(folder, vols)
        tcnt, bcnt, bbytes = stats[folder]
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
    on a refresh in progress - the heavy work runs in a separate worker process/connection.
    Carries the cached `remaining` stat alongside (the header pill reads it from here)."""
    con = bbdb.connect()

    def _remaining():
        try:
            return json.loads(bbdb.kv_get(con, "remaining") or "{}")
        except Exception:  # noqa: BLE001 - a junk stat must not break the status payload
            return {}

    cached = bbdb.kv_get(con, "cache_status")
    out = None
    if cached:
        try:
            out = json.loads(cached)
        except Exception:
            out = None
    if out is None:
        out = _status_live()
        out["cached_at"] = None
    out["remaining"] = _remaining()
    _with_volumes(out.get("drives") or [])
    out["platform"] = paths.platform()
    out["sep"] = _sep()
    out["computer"] = paths.computer_name()
    out["this_computer"] = paths.is_this_computer()
    return out


def _status_live() -> dict:
    with _files_cursor() as fc:
        cur = fc.execute(
            "SELECT drive, count(*) total, sum(CASE WHEN excluded=0 THEN 1 ELSE 0 END) backed "
            "FROM files GROUP BY drive ORDER BY drive")
        rows = _dict_rows(cur, cur.fetchall())
    drives = [{"drive": r["drive"], "total": r["total"], "backed": r["backed"] or 0} for r in rows]
    return {"built": bool(drives), "drives": drives,
            "total_files": sum(d["total"] for d in drives),
            "backed_files": sum(d["backed"] for d in drives)}
