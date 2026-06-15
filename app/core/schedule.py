"""Compute each source's next scheduled snapshot time.

kopia only exposes nextSnapshotTime through a live read-WRITE server (which we don't
run), so we replicate its ``SchedulingPolicy.NextSnapshotTime`` logic from the source's
scheduling policy (interval / timeOfDay / cron). ``policy show`` is one subprocess per
source, so results are cached to disk and refreshed in the background.
"""

from __future__ import annotations

import datetime as dt
import json
import os

from config import CACHEDIR

from .applog import get_logger
from .kopia import kopia

log = get_logger("schedule")


def _field(expr: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in expr.split(","):
        p, step = part, 1
        if "/" in p:
            p, s = p.split("/", 1)
            step = int(s)
        if p in ("*", ""):
            a, b = lo, hi
        elif "-" in p:
            a, b = (int(x) for x in p.split("-", 1))
        else:
            a = b = int(p)
        out.update(range(a, b + 1, step))
    return out


def cron_next(expr: str, now: dt.datetime) -> dt.datetime | None:
    """Next datetime matching a 5-field cron (minute hour dom month dow), local time."""
    expr = expr.split("#", 1)[0].strip()  # strip trailing comment
    parts = expr.split()
    if len(parts) != 5:
        return None
    try:
        mins, hrs = _field(parts[0], 0, 59), _field(parts[1], 0, 23)
        doms, mons = _field(parts[2], 1, 31), _field(parts[3], 1, 12)
        dows = _field(parts[4], 0, 6)  # cron: 0=Sunday
    except ValueError:
        return None
    t = (now + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(367 * 24 * 60):  # bounded ~1 year
        if (t.minute in mins and t.hour in hrs and t.month in mons
                and t.day in doms and ((t.weekday() + 1) % 7) in dows):
            return t
        t += dt.timedelta(minutes=1)
    return None


def next_time(sched: dict, last_dt: dt.datetime, now: dt.datetime) -> dt.datetime | None:
    if sched.get("manual"):
        return None
    candidates: list[dt.datetime] = []
    iv = sched.get("intervalSeconds")
    if iv:
        nt = last_dt + dt.timedelta(seconds=int(iv))
        candidates.append(nt if nt >= now else now)
    for tod in sched.get("timeOfDay", []) or []:
        c = now.replace(hour=int(tod.get("hour", 0)), minute=int(tod.get("min", 0)), second=0, microsecond=0)
        if c <= now:
            c += dt.timedelta(days=1)
        candidates.append(c)
    for cron in sched.get("cron", []) or []:
        c = cron_next(cron, now)
        if c:
            candidates.append(c)
    return min(candidates) if candidates else None


def _cache_path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"nextsnap_repo{repo}.json")


def load_next_snapshots(repo: str) -> dict[str, str]:
    try:
        with open(_cache_path(repo), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _to_local_naive(iso: str | None, fallback: dt.datetime) -> dt.datetime:
    if not iso:
        return fallback
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except Exception:
        return fallback


def compute_next_snapshots(repo: str) -> dict[str, str]:
    """Recompute next-snapshot times for all sources (one ``policy show`` each) and cache."""
    from .sources import sources_index  # local import avoids a circular dependency

    srcs = sources_index(repo)
    now = dt.datetime.now()
    out: dict[str, str] = {}
    for key, snaps in srcs.items():
        try:
            res = kopia(repo, ["policy", "show", key, "--json"])
            pol = json.loads(res.stdout) if res.stdout.strip() else {}
            sched = pol.get("scheduling", {}) or {}
            last_dt = _to_local_naive(snaps[-1].get("startTime") if snaps else None, now)
            nt = next_time(sched, last_dt, now)
            if nt:
                out[key] = nt.isoformat()
        except Exception as exc:  # noqa: BLE001 - per-source, keep going
            log.warning("next_snapshot compute failed for %s: %s", key, exc)
    try:
        with open(_cache_path(repo), "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    except Exception:
        pass
    log.info("next_snapshots repo=%s computed for %d sources", repo, len(out))
    return out
