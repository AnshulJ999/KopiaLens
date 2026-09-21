"""Retention replica semantics - the kopia divergences this repo's own data cannot show.

Every real source here runs keepLatest 100/200, which hides the three differences found by
reading kopia's retention_policy.go against the replica: the age cutoffs (kopia's "48 hourly" is
"within the last 48 hours", not "the 48 latest distinct hours"), Go's AddDate month-overflow
arithmetic behind those cutoffs, and the rules for incomplete snapshots. An all-zero policy is the
fourth: kopia keeps EVERYTHING, not nothing.

The semantics are pinned here with synthetic snapshots at kopia's defaults. Live verification of
the same code: `check_retention` reports 0 mismatches against kopia's own retentionReason on
repos 1, 2 and 3 (10,224 snapshots, 195 sources).
"""

from __future__ import annotations

import datetime as dt

from app.core import retention

SEP = chr(92)
DEFAULTS = {"latest": 10, "hourly": 48, "daily": 7, "weekly": 4, "monthly": 24, "annual": 3}


def _snap(sid: str, start: str, *, incom: str = "", pins=None, reason=None) -> dict:
    snap = {"id": sid, "startTime": start, "endTime": start}
    if incom:
        snap["incomplete"] = incom
    if pins:
        snap["pins"] = pins
    if reason:
        snap["retention"] = reason
    return snap


def _at(hours_back: int, end: str = "2026-09-20T12:00:00+00:00") -> str:
    t = dt.datetime.fromisoformat(end) - dt.timedelta(hours=hours_back)
    return t.isoformat().replace("+00:00", "Z")


def _kept(snaps: list[dict], policy: dict) -> set[str]:
    return retention.retention_keep(snaps, policy)[0]


def test_hourly_bucket_is_age_bounded_not_the_n_newest_hours():
    """48 hourly = within the last 48 hours, measured from the newest COMPLETE snapshot."""
    # One snapshot a day for five days; only latest/daily/weekly/... are 1, so the union is the
    # hourly set plus the single newest snapshot.
    snaps = [_snap(f"s{i}", _at(24 * i)) for i in range(5)]
    policy = {**DEFAULTS, "latest": 1, "daily": 1, "weekly": 1, "monthly": 1, "annual": 1}
    assert _kept(snaps, policy) == {"s0", "s1", "s2"}   # s2 sits exactly ON the 48 h cutoff
    assert retention.retention_keep(snaps, policy)[1]["s2"] == ["hourly-3"]


def test_monthly_cutoff_uses_go_adddate_normalization():
    """Mar 31 minus one month is Mar 3 in Go (Feb 31 normalizes), never Feb 28 - a clamp keeps
    an older February snapshot that kopia drops."""
    snaps = [_snap("new", "2026-03-31T12:00:00Z"), _snap("old", "2026-02-28T13:00:00Z")]
    policy = {"latest": 1, "hourly": 0, "daily": 0, "weekly": 0, "monthly": 1, "annual": 0}
    # Go: cutoff = 2026-03-03 12:00 -> "old" is out. Clamp to Feb 28 12:00 -> "old" is in.
    assert _kept(snaps, policy) == {"new"}


def test_all_zero_policy_keeps_every_complete_snapshot():
    snaps = [_snap("a", "2026-09-20T12:00:00Z"), _snap("b", "2026-09-19T12:00:00Z")]
    zeros = {b: 0 for b in retention._BUCKETS}
    assert _kept(snaps, zeros) == {"a", "b"}
    assert "keeps every snapshot" in (retention.keep_all_note(zeros) or "")
    assert retention.keep_all_note(DEFAULTS) is None
    # an incomplete snapshot still follows kopia's own rule under an all-zero policy
    with_incomplete = [_snap("new", "2026-09-20T12:00:00Z"),
                       _snap("old-checkpoint", "2026-09-19T12:00:00Z", incom="checkpoint")]
    assert _kept(with_incomplete, zeros) == {"new"}


def test_incomplete_snapshots_follow_the_leading_rule():
    """kopia tags leading incomplete ones younger than 4 h or among the first three."""
    snaps = [_snap("i1", _at(1), incom="checkpoint"), _snap("i2", _at(5), incom="checkpoint"),
             _snap("i3", _at(6), incom="checkpoint"), _snap("i4", _at(7), incom="checkpoint"),
             _snap("c1", _at(8)), _snap("c2", _at(9))]
    assert _kept(snaps, DEFAULTS) == {"i1", "i2", "i3", "c1", "c2"}


def test_pins_are_kept_outside_every_bucket():
    snaps = [_snap("new", "2026-09-20T12:00:00Z"),
             _snap("pinned", "2020-01-01T12:00:00Z", pins=["do-not-delete"])]
    assert _kept(snaps, DEFAULTS) == {"new", "pinned"}


def test_check_retention_matches_kopia_and_reports_the_rest():
    ok = f"u@h:C:{SEP}ok"
    bad = f"u@h:C:{SEP}bad"
    srcs = {
        ok: [_snap("s1", "2026-09-20T12:00:00Z", reason=["latest-1"]),
             _snap("s2", "2026-09-19T12:00:00Z")],
        bad: [_snap("s1", "2026-09-20T12:00:00Z", reason=["latest-1"]),
              _snap("s2", "2026-09-19T12:00:00Z", reason=["latest-2"])],
        f"u@h:C:{SEP}unknown": [_snap("s1", "2026-09-20T12:00:00Z")],
    }
    policies = {ok: {"retention": {"keepLatest": 1}}, bad: {"retention": {"keepLatest": 1}}}
    out = retention.check_retention("3", srcs=srcs, policies=policies)
    assert out["checked"] == 2
    assert out["failed"] == [f"u@h:C:{SEP}unknown"]
    assert len(out["mismatches"]) == 1
    mismatch = out["mismatches"][0]
    assert mismatch["source"] == bad and mismatch["we_drop"] == 1 and mismatch["we_keep"] == 0
