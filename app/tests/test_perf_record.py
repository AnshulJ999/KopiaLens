"""The index worker's performance record (app/index/perf.py)."""

import json

from app.core import kopia as kopia_mod
from app.index import perf


def _snap(start, files, dirs, size):
    return {"startTime": start, "rootEntry": {"summ": {"files": files, "dirs": dirs, "size": size}}}


def test_hooks_are_noops_without_a_run():
    assert perf.active() is None
    with perf.phase("index"):
        perf.lap("tree")
        perf.count(paths=1)
        perf.level(level=1)
    assert perf.finish({"ok": True}) is None


def test_listing_inputs_take_latest_as_floor_and_sum_as_ceiling():
    srcs = {"a": [_snap("2026-09-01T00:00:00Z", 10, 3, 100), _snap("2026-09-02T00:00:00Z", 12, 4, 120)],
            "b": [_snap("2026-09-01T00:00:00Z", 5, 1, 50)]}
    got = perf.inputs_from_listing(srcs)
    assert got == {"sources": 2, "snapshots": 3, "latest_files": 17, "latest_dirs": 5,
                   "latest_bytes": 170, "all_files": 27, "all_dirs": 8, "max_snaps_per_source": 2}


def test_record_nests_laps_counts_kopia_and_keeps_the_last_runs(tmp_path, monkeypatch):
    path = tmp_path / "perf_repo9.jsonl"
    monkeypatch.setattr(perf, "records_path", lambda repo: str(path))
    path.write_text("\n".join(json.dumps({"old": i}) for i in range(perf._KEEP_RUNS)) + "\n")

    perf.start("9")
    with perf.phase("index"):
        perf.lap("tree")
        with perf.phase("tree/read from kopia"):
            kopia_mod.KOPIA_CALLS += 3
            kopia_mod.KOPIA_TIME += 1.5
        perf.lap("maps")
        perf.count(paths=42, ignored=None)
    rec = perf.finish({"ok": True, "full": True, "elapsed": 1.0})

    index = rec["phases"][0]
    assert index["name"] == "index" and index["kopia_calls"] == 3
    assert [lap["name"] for lap in index["laps"]] == ["tree", "maps"]
    assert index["laps"][0]["laps"][0]["name"] == "tree/read from kopia"
    assert index["laps"][0]["laps"][0]["kopia_secs"] == 1.5
    assert rec["counts"] == {"paths": 42}
    lines = path.read_text().splitlines()
    assert len(lines) == perf._KEEP_RUNS and json.loads(lines[-1])["repo"] == "9"
    assert perf.active() is None
