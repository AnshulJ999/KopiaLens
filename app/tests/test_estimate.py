"""First-index estimates: ranges from the snapshot listing, and the progress fallback they feed."""

from __future__ import annotations

import time

from app.core import nightly
from app.index import estimate, perf


def snap(when: str, files: int, dirs: int) -> dict:
    return {"startTime": when, "rootEntry": {"summ": {"files": files, "dirs": dirs}}}


def test_a_long_history_scales_the_newest_snapshot_and_a_short_one_is_capped():
    long = [snap(f"2026-01-{d:02d}T00:00:00Z", 900 + d, 100) for d in range(1, 31)]
    short = [snap("2026-01-01T00:00:00Z", 90, 10), snap("2026-01-02T00:00:00Z", 95, 10)]
    lo, hi = estimate.entries_range({"a": long, "b": short})
    newest_a, every_b = 930 + 100, 100 + 105
    assert lo == int(newest_a * estimate.ENTRY_FACTOR[0]) + every_b
    assert hi == int(newest_a * estimate.ENTRY_FACTOR[1]) + every_b


def test_ranges_are_ordered_and_an_exact_count_still_gives_a_range():
    e = estimate.from_entries(1_000_000, 2_000_000)
    assert e["bytes"][0] < e["bytes"][1] and e["seconds"][0] < e["seconds"][1]
    x = estimate.from_entries(1_500_000, 1_500_000, exact=True)
    assert x["exact"] and x["seconds"][0] < x["seconds"][1]
    assert abs(sum(x["stage_seconds"].values()) - (x["seconds"][0] * x["seconds"][1]) ** 0.5) < 2


def test_a_first_index_without_run_history_uses_the_estimate(monkeypatch):
    monkeypatch.setattr(nightly, "_stage_weights", lambda repo, full: {})
    est = estimate.from_entries(1_000_000, 1_000_000, exact=True)
    out = nightly.run_progress("9", {"stage": "analysis", "full": True, "estimate": est,
                                     "stage_started": time.time()})
    assert out["estimated"] and out["first_estimate"]
    assert 50 <= out["pct"] < 99 and out["eta_seconds"] > 0
    assert nightly.run_progress("9", {"stage": "analysis"})["estimated"] is False


def test_the_estimate_becomes_exact_once_the_tree_is_read(monkeypatch):
    monkeypatch.setattr(perf, "_RUN", None)      # restored afterwards: no recorder leaks out
    perf.start("9")
    try:
        perf.set_inputs({"a": [snap("2026-01-01T00:00:00Z", 1000, 100)] * 3}, first=True)
        assert perf.active().estimate["exact"] is False
        perf.count(tree_entries=2500)
        assert perf.active().estimate["exact"] and perf.active().estimate["entries"] == [2500, 2500]
    finally:
        perf.active().stop()
