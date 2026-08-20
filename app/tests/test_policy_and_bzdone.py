"""Policy summarising + the bz_done day parser.

The bz_done tests exist because of a real miss: Backblaze pads these files with its own marker
rows whose "size" is the literal hex string 0123456789abcdef. Summed naively, one such row adds
~8.1e17 bytes and turns a day into 491 million GB - which is exactly what the first pass produced.
"""

from __future__ import annotations

from app.core.policy import describe_retention, describe_schedule, summarize
from app.backblaze.parsers import parse_bz_done_day_totals

SEP = chr(92)


# ---- policy formatting ----------------------------------------------------------------
def test_schedule_interval_is_humanised():
    assert describe_schedule({"intervalSeconds": 21600}) == "every 6h"
    assert describe_schedule({"intervalSeconds": 86400}) == "daily"
    assert describe_schedule({"intervalSeconds": 172800}) == "every 2d"
    assert describe_schedule({"intervalSeconds": 900}) == "every 15m"


def test_schedule_combines_interval_time_and_cron():
    out = describe_schedule({
        "intervalSeconds": 21600,
        "timeOfDay": [{"hour": 2, "min": 30}],
        "cron": [" 0 22 * * *   # Every day at 10 PM"],
    })
    assert "every 6h" in out and "02:30" in out
    assert "cron 0 22 * * *" in out
    assert "#" not in out  # the trailing comment is stripped


def test_schedule_manual_and_empty():
    assert describe_schedule({"manual": True}) == "manual only"
    assert describe_schedule({}) == "inherited"
    assert describe_schedule({"timeOfDay": []}) == "no schedule"


def test_retention_lists_only_the_buckets_in_use():
    out = describe_retention({"keepLatest": 200, "keepDaily": 360, "keepAnnual": 5, "keepWeekly": 0})
    assert out == "200 latest / 360d / 5y"
    assert describe_retention({}) == "inherited"


def test_summarize_shape_matches_a_real_policy():
    """Field names taken from an actual `kopia policy show --json` on this machine."""
    raw = {
        "retention": {"keepLatest": 200, "keepHourly": 360, "ignoreIdenticalSnapshots": True},
        "files": {"ignore": ["/NONE", "ComfyUI"], "ignoreCacheDirs": True, "maxFileSize": 21474836480},
        "scheduling": {"intervalSeconds": 21600},
        "compression": {"compressorName": "zstd"},
    }
    s = summarize(raw)
    assert s["schedule"] == "every 6h"
    assert s["retention"] == "200 latest / 360h"
    assert s["ignore"] == ["/NONE", "ComfyUI"]
    assert s["compression"] == "zstd"
    assert s["ignore_identical"] is True
    assert s["max_file_size"] == 21474836480


def test_summarize_survives_an_empty_policy():
    s = summarize({})
    assert s["ignore"] == [] and s["compression"] == "none"


# ---- bz_done day totals ---------------------------------------------------------------
def _line(kind: str, stamp: str, size_hex: str, path: str) -> str:
    """A bz_done row: 14 tab-separated fields, timestamp at 3, hex size at 6, path last."""
    fields = [kind, "-", "---", stamp, "r_h", "u--", size_hex, "k-", "-", "-", "-", "-", "0", path]
    return "\t".join(fields)


def test_day_totals_sum_real_rows(tmp_path):
    f = tmp_path / "bz_done_20260817_0.dat"
    f.write_text("\n".join([
        _line("5", "20260817142906", "00000000003fed8e", "E:" + SEP + "a.f"),
        _line("5", "20260817150000", "0000000000000064", "E:" + SEP + "b.f"),
        _line("5", "20260818010000", "0000000000000010", "E:" + SEP + "c.f"),
    ]), encoding="utf-8")
    out = parse_bz_done_day_totals(str(f))
    assert out["2026-08-17"] == {"files": 2, "bytes": 0x3fed8e + 0x64}
    assert out["2026-08-18"] == {"files": 1, "bytes": 0x10}


def test_padding_rows_are_ignored(tmp_path):
    """The bug this test exists for: one marker row otherwise adds ~8.1e17 bytes."""
    f = tmp_path / "bz_done_20260717_0.dat"
    f.write_text("\n".join([
        _line("5", "20260717120000", "0000000000000064", "E:" + SEP + "real.f"),
        _line("#", "20260718101112", "0123456789abcdef", "C:" + SEP + "zzz_bloat_yyy" + SEP + "x"),
    ]), encoding="utf-8")
    out = parse_bz_done_day_totals(str(f))
    assert out == {"2026-07-17": {"files": 1, "bytes": 0x64}}
    assert "2026-07-18" not in out


def test_malformed_rows_are_skipped_not_fatal(tmp_path):
    f = tmp_path / "bz_done_20260101_0.dat"
    f.write_text("\n".join([
        "short\trow",
        _line("5", "notadate", "0000000000000064", "E:" + SEP + "a.f"),
        _line("5", "20260101120000", "nothex", "E:" + SEP + "b.f"),
        _line("5", "20260101120000", "0000000000000064", "E:" + SEP + "c.f"),
        "",
    ]), encoding="utf-8")
    assert parse_bz_done_day_totals(str(f)) == {"2026-01-01": {"files": 1, "bytes": 0x64}}


def test_missing_file_returns_empty(tmp_path):
    assert parse_bz_done_day_totals(str(tmp_path / "nope.dat")) == {}
