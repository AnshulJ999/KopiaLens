"""Read + cache each source's kopia policy (retention, schedule, ignore rules, compression).

``compute_next_snapshots`` already ran ``policy show`` for the scheduling block and threw the
rest away; this module keeps the whole policy so the UI can answer "what backs this folder up,
how often, how long is it kept, and is it excluded". One subprocess per source, cached to disk.

Read-only: ``policy show`` never mutates. Policy EDITS are not done here (command-generation only,
elsewhere) - this module never writes to kopia.
"""

from __future__ import annotations

import json
import os
import threading
import time

from config import CACHEDIR

from .applog import get_logger
from .kopia import kopia

log = get_logger("policy")

POLICY_TTL = 24 * 3600          # per-source policies are refreshed nightly; this is the read-side floor
_PATH_TTL = 900.0               # effective-policy-for-a-folder lookups (one subprocess each)
_PATH_CACHE: dict[tuple, tuple[float, dict]] = {}
_PATH_CACHE_MAX = 300
_PATH_LOCK = threading.Lock()


def _cache_path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"policies_repo{repo}.json")


def load_policies(repo: str) -> dict:
    """Cached ``{"ts": float, "policies": {source_key: raw_policy}}``; empty dict when never built."""
    try:
        with open(_cache_path(repo), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_policies(repo: str, policies: dict) -> None:
    os.makedirs(CACHEDIR, exist_ok=True)
    tmp = _cache_path(repo) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "policies": policies}, fh)
        os.replace(tmp, _cache_path(repo))
    except Exception as exc:  # noqa: BLE001 - a cache write must never fail the caller
        log.warning("repo=%s policy cache write failed: %s", repo, exc)


def show_policy(repo: str, target: str) -> dict:
    """One ``kopia policy show <target> --json``. ``target`` is a source key (``user@host:path``)
    or the same user@host with any absolute path - kopia resolves the EFFECTIVE (inherited) policy
    for a path that has no policy of its own."""
    res = kopia(repo, ["policy", "show", target, "--json"])
    out = (res.stdout or "").strip()
    return json.loads(out) if out else {}


def refresh_policies(repo: str, force: bool = False) -> dict:
    """Re-read every source's policy (one subprocess each) and cache it. Skips entirely when the
    cache is younger than POLICY_TTL unless ``force``. Runs in the nightly worker."""
    from .sources import sources_index  # local import: avoids a circular dependency

    cached = load_policies(repo)
    if not force and cached.get("policies") and time.time() - float(cached.get("ts") or 0) < POLICY_TTL:
        return cached["policies"]
    policies: dict = {}
    failed = 0
    for key in sorted(sources_index(repo)):
        try:
            policies[key] = show_policy(repo, key)
        except Exception as exc:  # noqa: BLE001 - one bad source must not lose the rest
            failed += 1
            log.warning("policy show failed for %s: %s", key, exc)
    if policies:
        _write_policies(repo, policies)
    log.info("policies repo=%s: %d read, %d failed", repo, len(policies), failed)
    return policies


def _user_host(source_key: str) -> str:
    """A source key like ``Anshul@anshul-desktop:C:`` + drive root -> ``Anshul@anshul-desktop``.
    The path itself contains a
    colon on Windows, so split on the FIRST one only (user@host never contains a colon)."""
    return source_key.split(":", 1)[0]


def effective_for_path(repo: str, source_key: str, path: str) -> dict:
    """Effective policy for an arbitrary folder under a source (kopia resolves inheritance).
    One subprocess, cached ~15 min - the folder detail panel calls this per folder opened."""
    target = f"{_user_host(source_key)}:{path}"
    ck = (repo, target.lower())
    now = time.time()
    with _PATH_LOCK:
        hit = _PATH_CACHE.get(ck)
        if hit is not None and now - hit[0] < _PATH_TTL:
            return hit[1]
    try:
        pol = show_policy(repo, target)
    except Exception as exc:  # noqa: BLE001
        log.warning("effective policy failed for %s: %s", target, exc)
        pol = {}
    with _PATH_LOCK:
        if len(_PATH_CACHE) >= _PATH_CACHE_MAX:
            for stale in sorted(_PATH_CACHE, key=lambda k: _PATH_CACHE[k][0])[:_PATH_CACHE_MAX // 4]:
                _PATH_CACHE.pop(stale, None)
        _PATH_CACHE[ck] = (now, pol)
    return pol


def _fmt_interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        n = seconds // 86400
        return f"every {n}d" if n > 1 else "daily"
    if seconds % 3600 == 0:
        return f"every {seconds // 3600}h"
    if seconds % 60 == 0:
        return f"every {seconds // 60}m"
    return f"every {seconds}s"


def describe_schedule(sched: dict) -> str:
    """Human one-liner for a scheduling policy block, e.g. ``every 6h + 22:00``."""
    if not sched:
        return "inherited"
    if sched.get("manual"):
        return "manual only"
    parts: list[str] = []
    iv = sched.get("intervalSeconds")
    if iv:
        parts.append(_fmt_interval(int(iv)))
    for tod in sched.get("timeOfDay") or []:
        parts.append(f"{int(tod.get('hour', 0)):02d}:{int(tod.get('min', 0)):02d}")
    for expr in sched.get("cron") or []:
        clean = str(expr).split("#", 1)[0].strip()
        if clean:
            parts.append(f"cron {clean}")
    return " + ".join(parts) if parts else "no schedule"


_RETENTION_LABELS = (
    ("keepLatest", "latest"), ("keepHourly", "h"), ("keepDaily", "d"),
    ("keepWeekly", "w"), ("keepMonthly", "m"), ("keepAnnual", "y"),
)


def describe_retention(ret: dict) -> str:
    """``200 latest / 360h / 360d / 120w / 48m / 5y`` - the six fixed kopia count buckets."""
    if not ret:
        return "inherited"
    parts = []
    for field, label in _RETENTION_LABELS:
        val = ret.get(field)
        if val:
            parts.append(f"{int(val)} {label}" if label == "latest" else f"{int(val)}{label}")
    return " / ".join(parts) if parts else "no retention"


def summarize(pol: dict) -> dict:
    """Compact, UI-ready view of a raw policy."""
    files = pol.get("files") or {}
    return {
        "schedule": describe_schedule(pol.get("scheduling") or {}),
        "retention": describe_retention(pol.get("retention") or {}),
        "ignore": list(files.get("ignore") or []),
        "ignore_dot_files": list(files.get("ignoreDotFiles") or []),
        "ignore_cache_dirs": bool(files.get("ignoreCacheDirs")),
        "max_file_size": files.get("maxFileSize"),
        "compression": ((pol.get("compression") or {}).get("compressorName") or "none"),
        "ignore_identical": bool((pol.get("retention") or {}).get("ignoreIdenticalSnapshots")),
    }


def source_summaries(repo: str) -> dict[str, dict]:
    """``{source_key: summarize(policy)}`` from cache; refreshes when the cache is stale/missing."""
    cached = load_policies(repo)
    policies = cached.get("policies") or {}
    if not policies or time.time() - float(cached.get("ts") or 0) >= POLICY_TTL:
        policies = refresh_policies(repo) or policies
    return {key: summarize(pol) for key, pol in policies.items()}
