"""Churn rollup invariants: nesting detection and the three views built from one base rollup.

Pure-logic tests (no DuckDB) - the SQL that produces the base rows is exercised separately in
test_churn_sql.py against an in-memory database.
"""

from __future__ import annotations

from app.index.churnview import build_views, nested_map

SEP = chr(92)


def _src(path: str) -> str:
    return "Anshul@pc:" + path


def _row(source: str, folder: str, ext: str, bytes_: int, versions: int, files: int) -> dict:
    return {"source": source, "folder": folder, "ext": ext,
            "bytes": bytes_, "versions": versions, "files": files}


def test_nested_map_finds_the_containing_source():
    keys = [_src("C:" + SEP), _src("C:" + SEP + "Users" + SEP + "A" + SEP + "AppData"), _src("D:" + SEP)]
    nested = nested_map(keys)
    assert nested[_src("C:" + SEP + "Users" + SEP + "A" + SEP + "AppData")] == _src("C:" + SEP)
    assert _src("C:" + SEP) not in nested   # a drive root is not inside anything
    assert _src("D:" + SEP) not in nested   # a different drive is not nested


def test_nested_map_picks_the_closest_parent():
    """With C:\\ and C:\\Users both present, C:\\Users\\A must report the DEEPER one."""
    a, b, c = _src("C:" + SEP), _src("C:" + SEP + "Users"), _src("C:" + SEP + "Users" + SEP + "A")
    assert nested_map([a, b, c])[c] == b


def test_nested_map_ignores_prefix_lookalikes():
    """C:\\Apps must not count as the parent of C:\\AppsData - only a real path boundary counts."""
    parent, sibling = _src("C:" + SEP + "Apps"), _src("C:" + SEP + "AppsData")
    assert sibling not in nested_map([parent, sibling])


def test_views_roll_up_consistently():
    rows = [
        _row(_src("C:" + SEP), "C:" + SEP + "Chrome", "dat", 300, 30, 2),
        _row(_src("C:" + SEP), "C:" + SEP + "Music", "mp3", 100, 1, 1),
        _row(_src("G:" + SEP + "Apps"), "C:" + SEP + "Chrome", "dat", 50, 5, 1),
    ]
    v = build_views(rows, limit=10)
    total = sum(r["bytes"] for r in rows)
    # every view must account for exactly the same bytes - they are three cuts of one rollup
    for key in ("by_source", "by_folder", "by_ext"):
        assert sum(r["bytes"] for r in v[key]) == total, key
    assert v["by_source"][0]["bytes"] == 400          # sorted by bytes desc
    assert v["by_ext"][0]["ext"] == "dat"


def test_by_source_reports_its_biggest_folder():
    rows = [
        _row(_src("C:" + SEP), "C:" + SEP + "Chrome", "dat", 300, 30, 2),
        _row(_src("C:" + SEP), "C:" + SEP + "Music", "mp3", 100, 1, 1),
    ]
    row = build_views(rows, limit=10)["by_source"][0]
    assert row["top_folder"] == "C:" + SEP + "Chrome"
    assert row["top_folder_bytes"] == 300


def test_folder_in_two_sources_is_flagged():
    rows = [
        _row(_src("G:" + SEP), "G:" + SEP + "Apps" + SEP + "X", "bin", 10, 1, 1),
        _row(_src("G:" + SEP + "Apps"), "G:" + SEP + "Apps" + SEP + "X", "bin", 10, 1, 1),
    ]
    folder = build_views(rows, limit=10)["by_folder"][0]
    assert folder["source_count"] == 2
    assert len(folder["sources"]) == 2


def test_nesting_uses_the_full_source_list_not_just_churning_ones():
    """Regression: nesting was computed only over sources that appeared in the rollup, so a quiet
    parent meant its child was never flagged as nested."""
    child = _src("G:" + SEP + "Apps")
    parent = _src("G:" + SEP)
    rows = [_row(child, "G:" + SEP + "Apps" + SEP + "Ollama", "bin", 50, 5, 1)]

    assert build_views(rows, limit=10)["by_source"][0]["nested_in"] is None
    with_all = build_views(rows, limit=10, all_sources=[child, parent])
    assert with_all["by_source"][0]["nested_in"] == parent


def test_limit_truncates_each_view():
    rows = [_row(_src(f"C:{SEP}s{i}"), f"C:{SEP}f{i}", "x", i, 1, 1) for i in range(1, 20)]
    v = build_views(rows, limit=5)
    assert len(v["by_source"]) == 5
    assert v["by_source"][0]["bytes"] == 19  # biggest first
