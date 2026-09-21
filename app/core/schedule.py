"""Compute each source's next scheduled snapshot time.

kopia only exposes nextSnapshotTime through a live read-WRITE server (which we don't
run), so we replicate its ``SchedulingPolicy.NextSnapshotTime`` logic from the source's
scheduling policy (interval / timeOfDay / cron). ``policy show`` is one subprocess per
source, so results are cached to disk and refreshed in the background.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import re
import time

from config import CACHEDIR

from .applog import get_logger
from .kopia import kopia

log = get_logger("schedule")


# ---- cron: port of hashicorp/cronexpr v1.1.3 (what kopia 0.23.1 parses with) ----------------
# 5 fields = minute hour dom month dow; 6 fields add a trailing YEAR; 7 fields add a leading second.
# Times are local and naive; the DST slow-clock branch is not ported (no effect outside DST shifts).

_NUM = r"0?[0-9]|[1-5][0-9]"
_FIELDS = {
    "second": (0, 59, _NUM), "minute": (0, 59, _NUM), "hour": (0, 23, r"0?[0-9]|1[0-9]|2[0-3]"),
    "dom": (1, 31, r"0?[1-9]|[12][0-9]|3[01]"),
    "month": (1, 12, r"0?[1-9]|1[012]|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february"
                     r"|march|april|june|july|august|september|october|november|december"),
    "dow": (0, 6, r"0?[0-7]|sun|mon|tue|wed|thu|fri|sat|sunday|monday|tuesday|wednesday|thursday"
                  r"|friday|saturday"),
    "year": (1970, 2099, r"19[789][0-9]|20[0-9]{2}"),
}
_MONTHS = {n: i + 1 for i, names in enumerate(
    [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
     ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "september"),
     ("oct", "october"), ("nov", "november"), ("dec", "december")]) for n in names}
_DOWS = {n: i for i, names in enumerate(
    [("sun", "sunday"), ("mon", "monday"), ("tue", "tuesday"), ("wed", "wednesday"),
     ("thu", "thursday"), ("fri", "friday"), ("sat", "saturday")]) for n in names}
_MACROS = (("@yearly", "0 0 0 1 1 * *"), ("@annually", "0 0 0 1 1 * *"), ("@monthly", "0 0 0 1 * * *"),
           ("@weekly", "0 0 0 * * 0 *"), ("@daily", "0 0 0 * * * *"), ("@hourly", "0 0 * * * * *"))


def _atoi(field: str, s: str) -> int:
    if field == "month" and s in _MONTHS:
        return _MONTHS[s]
    if field == "dow":
        return _DOWS[s] if s in _DOWS else int(s) % 7
    return int(s)


def _directives(s: str, field: str) -> list[tuple]:
    """(kind, first, last, step, raw) per comma entry; kind is one/span/all/none."""
    lo, hi, val = _FIELDS[field]
    entries = re.findall(r"[^,]+", s)
    if not entries:
        raise ValueError(f"{field} field: missing directive")
    out = []
    for raw in entries:
        e = raw.lower()
        if re.fullmatch(r"\*|\?", e):
            out.append(("all", lo, hi, 1, raw))
            continue
        if re.fullmatch(f"({val})", e):
            out.append(("one", _atoi(field, e), 0, 0, raw))
            continue
        m = re.fullmatch(f"({val})-({val})", e)
        if m:
            out.append(("span", _atoi(field, m.group(1)), _atoi(field, m.group(2)), 1, raw))
            continue
        for pat, first, last in ((r"\*/(\d+)", None, None), (f"({val})/(\\d+)", 1, None),
                                 (f"({val})-({val})/(\\d+)", 1, 2)):
            m = re.fullmatch(pat, e)
            if m:
                step = int(m.groups()[-1])
                if step < 1 or step > hi:
                    raise ValueError(f"invalid interval {e}")
                a = lo if first is None else _atoi(field, m.group(first))
                b = hi if last is None else _atoi(field, m.group(last))
                out.append(("span", a, b, step, raw))
                break
        else:
            out.append(("none", 0, 0, 0, raw))
    return out


def _generic(s: str, field: str) -> list[int]:
    lo, hi, _ = _FIELDS[field]
    values: set[int] = set()
    for kind, a, b, step, raw in _directives(s, field):
        if kind == "none":
            raise ValueError(f"syntax error in {field} field: '{raw}'")
        if kind == "all":
            return list(range(lo, hi + 1))
        values.update([a] if kind == "one" else range(a, b + 1, step))
    return sorted(values)


class CronExpr:
    """A parsed cron line; raises ValueError like cronexpr.Parse."""

    def __init__(self, line: str) -> None:
        cron = line
        for macro, repl in _MACROS:
            cron = cron.replace(macro, repl)
        fields = cron.split()
        if len(fields) < 5:
            raise ValueError("missing field(s)")
        fields = fields[:7]
        i = 0
        if len(fields) == 7:
            self.seconds = _generic(fields[0], "second")
            i = 1
        else:
            self.seconds = [0]
        self.minutes = _generic(fields[i], "minute")
        self.hours = _generic(fields[i + 1], "hour")
        self._dom(fields[i + 2])
        self.months = _generic(fields[i + 3], "month")
        self._dow(fields[i + 4])
        self.years = (_generic(fields[i + 5], "year") if len(fields) > i + 5
                      else list(range(1970, 2100)))

    def _dom(self, s: str) -> None:
        self.dom_restricted, self.last_dom, self.last_workdom = True, False, False
        self.doms, self.workdoms = set(), set()
        for kind, a, b, step, raw in _directives(s, "dom"):
            e = raw.lower()
            if kind == "none":
                if e == "l":
                    self.last_dom = True
                elif e == "lw":
                    self.last_workdom = True
                elif re.fullmatch(f"({_FIELDS['dom'][2]})w", e):
                    self.workdoms.add(int(e[:-1]))
                else:
                    raise ValueError(f"syntax error in day-of-month field: '{raw}'")
            elif kind == "one":
                self.doms.add(a)
            else:
                self.doms.update(range(a, b + 1, step))
                if kind == "all":
                    self.dom_restricted = False

    def _dow(self, s: str) -> None:
        self.dow_restricted = True
        self.dows, self.nth_dows, self.last_dows = set(), set(), set()
        val = _FIELDS["dow"][2]
        for kind, a, b, step, raw in _directives(s, "dow"):
            e = raw.lower()
            if kind == "none":
                m = re.fullmatch(f"({val})l", e)
                if m:
                    self.last_dows.add(_atoi("dow", m.group(1)))
                    continue
                m = re.fullmatch(f"({val})#([1-5])", e)
                if not m:
                    raise ValueError(f"syntax error in day-of-week field: '{raw}'")
                self.nth_dows.add((int(m.group(2)) - 1) * 7 + (_atoi("dow", m.group(1)) % 7))
            elif kind == "one":
                self.dows.add(a)
            else:
                if kind == "span" and b == 0:
                    b = 6
                self.dows.update(range(a, b + 1, step))
                if kind == "all":
                    self.dow_restricted = False

    def _days(self, year: int, month: int) -> list[int]:
        last = calendar.monthrange(year, month)[1]
        if not self.dom_restricted and not self.dow_restricted:
            return list(range(1, last + 1))
        days: set[int] = set()
        go_wd = lambda y, mo, d: (dt.date(y, mo, d).weekday() + 1) % 7  # noqa: E731 - Go: Sunday=0

        def workday(d: int) -> int:
            wd = go_wd(year, month, d)
            if wd == 6:
                return d - 1 if d > 1 else d + 2
            if wd == 0:
                return d + 1 if d < last else d - 2
            return d

        if self.dom_restricted:
            if self.last_dom:
                days.add(last)
            if self.last_workdom:
                days.add(workday(last))
            days.update(d for d in self.doms if d <= last)
            days.update(workday(d) for d in self.workdoms if d <= last)
        if self.dow_restricted:
            first_wd = go_wd(year, month, 1)
            offset = 7 - first_wd
            for v in self.dows:
                start = 1 + (offset + v) % 7
                days.update(d for d in range(start, last + 1, 7))
            for v in self.nth_dows:
                d = 1 + 7 * (v // 7) + (offset + v) % 7
                if d <= last:
                    days.add(d)
            origin = dt.date(year, month, last) - dt.timedelta(days=6)
            loff = 7 - (origin.weekday() + 1) % 7
            for v in self.last_dows:
                d = origin.day + (loff + v) % 7
                if d <= last:
                    days.add(d)
        return sorted(days)

    def next(self, after: dt.datetime) -> dt.datetime | None:
        """The first matching instant strictly after ``after`` (second precision)."""
        t = after.replace(microsecond=0) + dt.timedelta(seconds=1)
        for year in self.years:
            if year < t.year:
                continue
            for month in self.months:
                if (year, month) < (t.year, t.month):
                    continue
                for day in self._days(year, month):
                    if (year, month, day) < (t.year, t.month, t.day):
                        continue
                    same_day = (year, month, day) == (t.year, t.month, t.day)
                    for hour in self.hours:
                        if same_day and hour < t.hour:
                            continue
                        same_hour = same_day and hour == t.hour
                        for minute in self.minutes:
                            if same_hour and minute < t.minute:
                                continue
                            same_min = same_hour and minute == t.minute
                            for second in self.seconds:
                                if same_min and second < t.second:
                                    continue
                                return dt.datetime(year, month, day, hour, minute, second)
        return None


_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def describe_cron(line: str) -> str:
    """A short plain-language reading of common cron shapes; "Custom schedule" otherwise."""
    try:
        c = CronExpr(strip_cron_comment(line))
    except ValueError:
        return ""
    if c.seconds != [0] or len(c.years) < 130 or c.last_dom or c.last_workdom or c.workdoms \
            or c.nth_dows or c.last_dows or len(c.months) != 12:
        return "Custom schedule"
    if len(c.hours) == 24 and len(c.minutes) == 1:
        when = f"every hour at minute {c.minutes[0]}"
    elif len(c.hours) == 1 and len(c.minutes) == 1:
        when = f"at {c.hours[0]:02d}:{c.minutes[0]:02d}"
    elif len(c.minutes) == 1 and len(c.hours) <= 6:
        when = "at " + ", ".join(f"{h:02d}:{c.minutes[0]:02d}" for h in c.hours)
    else:
        return "Custom schedule"
    if not c.dom_restricted and not c.dow_restricted:
        return "Every day " + when if not when.startswith("every") else when[0].upper() + when[1:]
    if c.dom_restricted and not c.dow_restricted and len(c.doms) == 1:
        return f"Day {min(c.doms)} of every month, {when}"
    if c.dow_restricted and not c.dom_restricted:
        days = sorted(c.dows)
        if days == [1, 2, 3, 4, 5]:
            return f"Weekdays, {when}"
        return ", ".join(_DOW_NAMES[d] for d in days) + f", {when}"
    return "Custom schedule"


def strip_cron_comment(expr: str) -> str:
    return str(expr).split("#", 1)[0].strip()


def cron_next(expr: str, now: dt.datetime) -> dt.datetime | None:
    """Next datetime matching a kopia cron line, local time. None for an invalid line."""
    try:
        return CronExpr(strip_cron_comment(expr)).next(now)
    except ValueError:
        return None


def _next_tod(sched: dict, now: dt.datetime) -> dt.datetime | None:
    best = None
    for tod in sched.get("timeOfDay") or []:
        c = now.replace(hour=int(tod.get("hour", 0)), minute=int(tod.get("min", 0)), second=0, microsecond=0)
        if now > c:
            c += dt.timedelta(days=1)
        if best is None or c < best:
            best = c
    return best


def _next_cron(sched: dict, now: dt.datetime) -> dt.datetime | None:
    best = None
    for expr in sched.get("cron") or []:
        c = cron_next(expr, now)
        if c is not None and (best is None or c < best):
            best = c
    return best


_GO_ZERO_OFFSET = 62135596800  # seconds from Go's zero time (year 1) to the Unix epoch


def next_time(sched: dict, last_dt: dt.datetime, now: dt.datetime) -> dt.datetime | None:
    """Port of kopia's SchedulingPolicy.NextSnapshotTime (interval truncation, times of day,
    cron, run-missed)."""
    if sched.get("manual"):
        return None
    nxt = None
    iv = int(sched.get("intervalSeconds") or 0)
    if iv:
        ts = (last_dt + dt.timedelta(seconds=iv)).timestamp()
        abs_ts = int(ts) + _GO_ZERO_OFFSET
        nxt = dt.datetime.fromtimestamp(abs_ts - abs_ts % iv - _GO_ZERO_OFFSET)
        if nxt < now:
            nxt = now
    tod = _next_tod(sched, now)
    if tod is not None and (nxt is None or tod < nxt):
        nxt = tod
    cron = _next_cron(sched, now)
    if cron is not None and (nxt is None or cron < nxt):
        nxt = cron
    if nxt is not None and sched.get("runMissed"):
        after = last_dt + dt.timedelta(seconds=1)
        t2, c2 = _next_tod(sched, after), _next_cron(sched, after)
        if t2 is not None or c2 is not None:
            missed = nxt
            if t2 is not None and t2 < missed:
                missed = t2
            if c2 is not None and c2 < missed:
                missed = c2
            if missed < now and nxt > now + dt.timedelta(minutes=30):
                nxt = now
    return nxt


def next_times(sched: dict, last_dt: dt.datetime | None, now: dt.datetime, count: int = 3) -> list[dt.datetime]:
    """The next ``count`` scheduled snapshot times, each computed as if the previous one ran."""
    out: list[dt.datetime] = []
    prev, cur = last_dt or now, now
    for _ in range(count):
        t = next_time(sched, prev, cur)
        if t is None or (out and t <= out[-1]):
            break
        out.append(t)
        prev, cur = t, t + dt.timedelta(seconds=1)
    return out


def _cache_name(repo: str) -> str:
    return f"nextsnap_repo{repo}"


def load_next_snapshots(repo: str) -> dict[str, str]:
    from ..index import sidecache  # lazy: avoids core<-index import cycle on load
    data = sidecache.read(_cache_name(repo))
    return data if isinstance(data, dict) else {}


def _to_local_naive(iso: str | None, fallback: dt.datetime) -> dt.datetime:
    if not iso:
        return fallback
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except Exception:
        return fallback


_POLICY_REUSE_MAX_AGE = 3600     # how fresh core.policy's cache must be to stand in for a spawn


def _recent_policies(repo: str) -> dict:
    """Scheduling blocks for every source, without a `policy show` per source ( +).

    The nightly's `policies` stage refreshes the per-source effective-policy cache moments before
    this stage runs, so a fresh cache is used directly. A stale one (the 30-minute light loop)
    resolves from ONE `policy export` instead of ~100 `policy show` calls, and the export itself is
    re-read once it is older than the reuse window - so a schedule edited in KopiaUI still lands
    within the hour, the same as before. Returns {} when neither is readable, which is the caller's
    cue to fall back to a spawn per source.
    """
    try:
        from . import policyresolve
        from .policy import load_defined_policies, load_policies, refresh_defined_policies
        from .sources import sources_index

        env = load_policies(repo)
        if time.time() - float(env.get("ts") or 0) <= _POLICY_REUSE_MAX_AGE:
            return env.get("policies") or {}
        exp = load_defined_policies(repo)
        if time.time() - float(exp.get("ts") or 0) > _POLICY_REUSE_MAX_AGE:
            exp, _fresh = refresh_defined_policies(repo, force=True)
        pols = exp.get("policies") or {}
        if pols:
            return {key: policyresolve.resolve(pols, key)[0] for key in sources_index(repo)}
    except Exception:  # noqa: BLE001 - no cache is simply a miss
        pass
    return {}


def compute_next_snapshots(repo: str, force: bool = False) -> dict[str, str]:
    """Compute next-snapshot times for all sources and cache them.

    Each source needs one ``policy show`` subprocess, so to avoid spawning kopia for every
    source on every background cycle we REUSE any cached next-time that is still in the future
    and only recompute sources that are missing or past-due (``force`` recomputes all). A
    past-due cached time means a snapshot likely just ran, so recomputing then keeps it fresh.
    """
    from .sources import sources_index  # local import avoids a circular dependency

    srcs = sources_index(repo)
    now = dt.datetime.now()
    prev = load_next_snapshots(repo) if not force else {}
    pols = _recent_policies(repo)
    out: dict[str, str] = {}
    spawned = reused = 0
    for key, snaps in srcs.items():
        cached = prev.get(key)
        if cached:
            try:
                if dt.datetime.fromisoformat(cached) > now:  # still in the future -> trust it, skip kopia
                    out[key] = cached
                    continue
            except Exception:
                pass
        try:
            pol = pols.get(key)
            if pol is None:
                res = kopia(repo, ["policy", "show", key, "--json"])
                pol = json.loads(res.stdout) if res.stdout.strip() else {}
                spawned += 1
            else:
                reused += 1
            sched = pol.get("scheduling", {}) or {}
            last_dt = _to_local_naive(snaps[-1].get("startTime") if snaps else None, now)
            nt = next_time(sched, last_dt, now)
            if nt:
                out[key] = nt.isoformat()
        except Exception as exc:  # noqa: BLE001 - per-source, keep going
            log.warning("next_snapshot compute failed for %s: %s", key, exc)
    try:
        from ..index import sidecache
        sidecache.write(_cache_name(repo), out)
    except Exception:  # noqa: BLE001 - a cache write must never fail the computation
        pass
    log.debug("next_snapshots repo=%s: %d sources (%d via policy show, %d from the policy cache)",
              repo, len(out), spawned, reused)
    return out
