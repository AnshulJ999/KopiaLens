"""Repo-level stats: content stats, repository status, cache info, maintenance info.

the Stats tab no longer dumps these four raw blocks - the same payloads are parsed into
readable cards plus a set of repository HEALTH checks on top (the owner-mismatch that silently
killed GC for 5.5 months was visible here the whole time, in raw form nobody read). The raw
output is still served for the collapsible "raw output" sections.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time

from fastapi import APIRouter

from config import DATA_DIR

from ..core.applog import get_logger
from ..core import audit
from ..core.kopia import kopia
from ..index import sidecache

router = APIRouter(tags=["stats"])
log = get_logger("stats")

_blob_cache: dict[str, str] = {}   # the last `blob stats` job's output per repo

# Repo stats change rarely but cost 4 kopia subprocess calls (~4.5s). Cache + serve-stale:
# return the cached blocks instantly and refresh in the background (also driven by the bg warm
# pass), so opening the Stats tab is never slow.
_STATS_TTL = 300.0
_stats_cache: dict[str, dict] = {}      # repo -> data
_stats_ts: dict[str, float] = {}        # repo -> last computed
_stats_refreshing: set[str] = set()
_stats_lock = threading.Lock()

_HISTORY_FILE = os.path.join(DATA_DIR, "repo_size_history.json")
_HISTORY_SAMPLE_MIN = 3600  # one growth sample per hour, however often stats refresh
_HISTORY_KEEP = 24 * 90     # ~90 days of hourly samples

_SIZE_RE = re.compile(r"([\d.]+)\s*(B|KB|MB|GB|TB|PB)", re.IGNORECASE)
_SIZE_MULT = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12, "pb": 10**15}


def _text(repo: str, args: list[str]) -> str:
    try:
        res = kopia(repo, args)
        return (res.stdout or res.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 - surface as text, never 500 the whole panel
        return f"(error running {' '.join(args)}: {exc})"


def _json(repo: str, args: list[str]):
    try:
        res = kopia(repo, args)
        return json.loads(res.stdout) if res.stdout.strip() else None
    except Exception:
        return None


def _parse_size(text: str) -> int:
    """'1.5 TB' -> bytes (kopia's decimal-ish humanized units; consistent enough for a trend)."""
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).lower()])


def _parse_ts(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(re.sub(r"\.(\d{1,6})\d*", r".\1", s))
    except ValueError:
        return None


def _parse_content_stats(text: str) -> dict:
    return {
        "count": int(m.group(1)) if (m := re.search(r"Count:\s*(\d+)", text)) else 0,
        "total_bytes": _parse_size(_find_line(text, "Total Bytes:")),
        "packed_bytes": _parse_size(_find_line(text, "Total Packed:")),
        "compression_pct": (float(m.group(1)) if (m := re.search(r"compression\s+([\d.]+)%", text)) else None),
    }


def _find_line(text: str, prefix: str) -> str:
    for line in (text or "").splitlines():
        if prefix in line:
            return line.split(prefix, 1)[1]
    return ""


def _kopia_cmd(repo: str, args: list[str]) -> str:
    """A pasteable kopia command that targets THIS repo (config args included). A bare
    `kopia maintenance ...` silently operates on the DEFAULT repository - copying a fix from
    repo 2 onto repo 1 is exactly the failure this prevents."""
    from ..core.kopia import _kopia_exe, _repo_args

    def _q(a) -> str:
        a = str(a)
        return f'"{a}"' if " " in a else a
    # the resolved program itself is quoted too: a KopiaUI bundle path has a space in it, and an
    # unquoted one is not a command anybody can paste.
    return " ".join([_q(_kopia_exe())] + [_q(a) for a in _repo_args(repo)] + [_q(a) for a in args])


def _maintenance_health(repo: str, maint: dict, status: dict) -> tuple[dict, list[dict]]:
    """Parse `maintenance info --json` into card fields + the two maintenance health checks."""
    health: list[dict] = []
    card = {"owner": maint.get("owner"), "full_enabled": bool((maint.get("full") or {}).get("enabled", True)),
            "next_full": (maint.get("schedule") or {}).get("nextFullMaintenance"),
            "next_quick": (maint.get("schedule") or {}).get("nextQuickMaintenance"),
            "last_run": None, "last_run_success": None}
    # Newest run of any task: schedule.runs maps task-name -> [{start, end, success}, ...]
    newest, newest_ok = None, None
    for runs in (maint.get("schedule") or {}).get("runs", {}).values():
        for run in runs:
            start = _parse_ts(run.get("start") or "")
            if start and (newest is None or start > newest):
                newest, newest_ok = start, bool(run.get("success"))
    card["last_run"] = newest.isoformat(timespec="seconds") if newest else None
    card["last_run_success"] = newest_ok

    # Owner check: the owner that actually runs maintenance is user@host of whichever client was
    # first. A mismatch means THIS machine will never run it - GC dies silently (5.5 months once).
    client = status.get("clientOptions") or {}
    this_host = str(client.get("hostname") or "")
    this_id = f"{client.get('username') or ''}@{this_host}" if this_host else ""
    card["owner_match"] = not card["owner"] or not this_id or card["owner"] == this_id
    if not card["owner_match"]:
        health.append({
            "id": "maintenance-owner", "status": "warn",
            "title": "This connection can't run maintenance",
            "detail": f"Owner is {card['owner']!r}, this connection is {this_id!r}. Garbage collection "
                      "never runs from here - deleted data keeps consuming space until it is fixed.",
            "fix_command": _kopia_cmd(repo, ["maintenance", "info"]),
        })
    try:
        from ..core import coexist
        idc = coexist.identity_check(repo)
    except Exception:  # noqa: BLE001 - the check is optional
        idc = None
    if idc:
        health.append({
            "id": "identity-case", "status": "warn",
            "title": "Snapshots here use a different spelling of this computer's name",
            "detail": (f"Snapshots were made as {idc['suggested']}; this connection is {idc['current']}. "
                       "A snapshot from here starts a separate history. Fix it under Who runs backups "
                       "on the Dashboard."),
            "fix_command": _kopia_cmd(repo, ["repository", "set-client", f"--hostname={idc['host']}",
                                             f"--username={idc['user']}"]),
        })
    if not card["full_enabled"]:
        health.append({
            "id": "maintenance-full-disabled", "status": "warn",
            "title": "Full maintenance is disabled",
            "detail": "Without a full pass, deleted snapshots and removed files are never actually "
                      "rewritten away - the repository only ever grows.",
            "fix_command": _kopia_cmd(repo, ["maintenance", "set-params", "--help"]),
        })
    age_days = ((dt.datetime.now(dt.timezone.utc) - newest.astimezone(dt.timezone.utc)).total_seconds() / 86400
                if newest else None)
    card["last_run_age_days"] = round(age_days, 1) if age_days is not None else None
    if newest is None:
        health.append({
            "id": "maintenance-never", "status": "warn",
            "title": "Maintenance has never run on this repository",
            "detail": "No maintenance runs are recorded. Space from deleted data is not being freed.",
            "fix_command": _kopia_cmd(repo, ["maintenance", "run", "--full"]),
        })
    elif card["last_run_age_days"] is not None and card["last_run_age_days"] > 7:
        health.append({
            "id": "maintenance-stale", "status": "warn",
            "title": f"Last maintenance ran {card['last_run_age_days']} days ago",
            "detail": "Quick maintenance normally runs about hourly. A gap this long usually means "
                      "an owner mismatch, a disabled schedule, or a machine that is rarely on.",
            "fix_command": _kopia_cmd(repo, ["maintenance", "run", "--full"]),
        })
    return card, health


def _index_health(repo: str) -> list[dict]:
    """KopiaLens's own index: size and generation of the published Parquet artifacts.

    This used to track DuckDB bloat against a compaction baseline. Artifacts are immutable now, so
    there is no dead space and nothing to compact - the useful facts are how big the current
    generation is and whether one exists at all. The caller renders ALL entries in the
    KopiaLens-index card but only WARNs in the Health list (shown twice otherwise)."""
    try:
        from ..index import store
        manifest = store.read_manifest(repo)
        if manifest is None:
            return [{"id": "index-missing", "status": "warn", "title": "No index yet",
                     "detail": "This repo has not been indexed. Run an index to populate it."}]
        pool = os.path.join(store.repo_dir(repo), "pool")
        size = sum(os.path.getsize(os.path.join(d, f))
                   for d, _s, fs in os.walk(pool) for f in fs)
        stats = manifest.get("stats", {})
        return [{"id": "index-size", "status": "ok",
                 "title": f"Index: {size / 10**9:.2f} GB",
                 "tip": f"Index generation {manifest.get('generation')}",
                 "detail": f"{stats.get('paths', 0):,} paths, {stats.get('file_versions', 0):,} "
                           f"file versions."}]
    except Exception as exc:  # noqa: BLE001 - a health check must never break the panel
        log.warning("index health check failed: %s", exc)
        return []


def _retention_check(repo: str) -> dict:
    """The nightly's retention self-check (``app/index/retentioncheck.py``), read from disk.

    kopia computes its own ``retentionReason`` while listing, so the check compares the replica
    against it, source by source - no subprocess here. The panel shows a line only when something
    disagrees; "none" means no check has been recorded yet."""
    try:
        from ..index import retentioncheck
        rec = retentioncheck.load(repo) or {}
    except Exception:  # noqa: BLE001 - a health read must never break the panel
        rec = {}
    if not rec:
        return {"status": "none", "checked": 0, "mismatches": 0, "failed": 0, "at": None, "detail": []}
    mismatches = rec.get("mismatches") or []
    failed = rec.get("failed") or []
    ts = rec.get("ts")
    return {
        "status": "mismatch" if mismatches else ("failed" if failed else "ok"),
        "checked": int(rec.get("checked") or 0),
        "mismatches": len(mismatches),
        "failed": len(failed),
        "at": dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else None,
        "detail": [f"{m.get('source')}: {m.get('we_drop')} kept by kopia that we drop, "
                   f"{m.get('we_keep')} we keep that kopia drops" for m in mismatches[:5]],
    }


_HISTORY_LOCK = threading.Lock()  # two repos refreshing at once must not lose samples


def _record_growth(repo: str, packed_bytes: int) -> list:
    """Hourly (ts, packed-bytes) samples for the growth trend. Small JSON file, bounded length."""
    try:
        with _HISTORY_LOCK:
            from ..core import filecrypto
            history = filecrypto.read_json(_HISTORY_FILE)   # missing/unreadable: start fresh
            if not isinstance(history, dict):
                history = {}
            samples = history.get(repo) or []
            now = time.time()
            if not samples or now - samples[-1][0] >= _HISTORY_SAMPLE_MIN:
                samples.append([round(now), packed_bytes])
                samples = samples[-_HISTORY_KEEP:]
                history[repo] = samples
                filecrypto.write_json(_HISTORY_FILE, history)
            return samples
    except Exception as exc:  # noqa: BLE001 - a trend sample must never break the panel
        log.warning("growth sample failed: %s", exc)
        return []


def _compute_stats(repo: str) -> dict:
    status = _json(repo, ["repository", "status", "--json"])
    maint = _json(repo, ["maintenance", "info", "--json"])
    data = {
        "repo": repo,
        "repository_status": status or _text(repo, ["repository", "status"]),
        "content_stats": _text(repo, ["content", "stats"]),
        "cache_info": _text(repo, ["cache", "info"]),
        "maintenance": maint or _text(repo, ["maintenance", "info"]),
    }
    # ---- parsed cards + health -----------------------------------------------------
    content = _parse_content_stats(data["content_stats"])
    maint_card, health = _maintenance_health(repo, maint or {}, status or {})
    if isinstance(status, dict):
        vol = status.get("volume") or {}
        storage = (status.get("storage") or {})
        data["parsed"] = {
            "unique_id": str(status.get("uniqueIDHex") or "")[:16],
            "storage_type": storage.get("type"),
            "storage_path": (storage.get("config") or {}).get("path"),
            "capacity": vol.get("capacity"), "available": vol.get("available"),
            "content": content,
            "maintenance": maint_card,
        }
        avail, cap = vol.get("available"), vol.get("capacity")
        if avail is not None and cap and avail / cap < 0.10:
            health.append({"id": "disk-space", "status": "warn",
                           "title": f"Storage volume is {avail / 10**9:.0f} GB from full",
                           "detail": "Backups and maintenance both fail on a full volume.",
                           "fix_command": None})
    else:
        data["parsed"] = {}
    data["parsed"]["duckdb_health"] = _index_health(repo)
    health.extend(h for h in data["parsed"]["duckdb_health"] if h["status"] == "warn")
    rc = _retention_check(repo)
    data["parsed"]["retention_check"] = rc
    if rc["status"] == "mismatch":
        health.append({
            "id": "retention-mismatch", "status": "warn",
            "title": f"Retention check: {rc['mismatches']} of {rc['checked']} sources disagree with kopia",
            "detail": "The simulator and the delete panel's keep counts come from KopiaLens's "
                      "retention replica, so a disagreement means those numbers can be wrong. "
                      + "; ".join(rc["detail"][:3]),
            "fix_command": None,
        })
    if content["packed_bytes"]:
        samples = _record_growth(repo, content["packed_bytes"])
        data["parsed"]["growth"] = samples
        week = [s for s in samples if s[0] >= time.time() - 7 * 86400]
        if len(week) >= 2:
            delta = week[-1][1] - week[0][1]
            data["parsed"]["growth_7d"] = delta
            if delta > 50 * 10**9:  # >50 GB in a week is worth a look, not necessarily a problem
                health.append({"id": "growth", "status": "info",
                               "title": f"Repository grew {delta / 10**9:+.0f} GB in 7 days",
                               "detail": "Growth itself is normal; rapid growth with no new backups "
                                         "usually means GC is not running (see maintenance checks).",
                               "fix_command": None})
    data["health"] = health
    data["audit_size"] = audit.size_bytes()
    at = time.time()
    with _stats_lock:
        _stats_cache[repo] = data
        _stats_ts[repo] = at
        _stats_refreshing.discard(repo)
    sidecache.write(_saved_name(repo), {"at": at, "data": data})
    return data


def _saved_name(repo: str) -> str:
    return f"stats_repo{repo}"


def _load_saved(repo: str) -> dict | None:
    """The last computed stats from disk (survives a restart), or None."""
    saved = sidecache.read(_saved_name(repo))
    if not isinstance(saved, dict) or not isinstance(saved.get("data"), dict):
        return None
    with _stats_lock:
        _stats_cache.setdefault(repo, saved["data"])
        _stats_ts.setdefault(repo, float(saved.get("at") or 0))
        return _stats_cache[repo]


def refresh_stats(repo: str) -> None:
    """Recompute + cache stats (called by the background warm pass). Single-flight."""
    with _stats_lock:
        if repo in _stats_refreshing:
            return
        _stats_refreshing.add(repo)
    try:
        _compute_stats(repo)
    except Exception as exc:  # noqa: BLE001
        log.warning("stats refresh repo=%s failed: %s", repo, exc)
        with _stats_lock:
            _stats_refreshing.discard(repo)


@router.get("/stats")
def stats(repo: str, refresh: bool = False) -> dict:
    """Fast read-only repo-level info for the Stats tab. Served from cache (serve-stale, saved
    across restarts); a stale cache is refreshed in the background. ``refresh`` recomputes now.
    ``blob stats`` runs only as a job (kind ``blob_stats``), started by the user."""
    with _stats_lock:
        cached = _stats_cache.get(repo)
    if cached is None and not refresh:
        cached = _load_saved(repo)
    if cached is None or refresh:
        cached = _compute_stats(repo)  # nothing saved yet, or asked for: compute synchronously
    with _stats_lock:
        at = _stats_ts.get(repo, 0)
    if time.time() - at > _STATS_TTL:  # stale: serve now, refresh in background
        threading.Thread(target=refresh_stats, args=(repo,), daemon=True).start()
    out = dict(cached)
    out["blob_stats"] = _blob_cache.get(repo)
    out["computed_at"] = at
    return out


def set_blob_stats(repo: str, text: str) -> None:
    """Keep a finished `blob stats` job's output for the Stats tab."""
    _blob_cache[repo] = text
