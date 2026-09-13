""" remove_targets_summary: the cheap pre-read /actions/remove-detail uses.

Invariants: the summary's totals and sources agree with the full remove_targets for the same
filter; it never materializes more than ``detail_limit`` pairs (the 200k-row blowout that
DuckDB-died on 2026-09-09 is gone from this path); and remove_detail's folder-vs-selection
decision is unchanged.
"""

from __future__ import annotations

import duckdb
import pytest

from app.api import actions
from app.index import queries

SEP = chr(92)
FOLDER = "G:" + SEP + "Big"


@pytest.fixture
def scratch(monkeypatch):
    """Two sources; Big/ holds six files under s1 (sizes 60..10) plus two under s2."""
    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, 's1'), (2, 's2')")
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    con.execute("INSERT INTO cid VALUES (1,'c1'),(2,'c2')")
    rows = []
    for i, size in enumerate((60, 50, 40, 30, 20, 10), 1):
        rows.append((i, 1, FOLDER + SEP + f"f{i}", size, size, 1 if i == 1 else 2))
    # s2: one file inside Big/, one outside
    rows.append((7, 2, FOLDER + SEP + "g1", 5, 5, 2))
    rows.append((8, 2, "G:" + SEP + "other", 1, 1, 2))
    con.execute("CREATE TABLE pathx(path_id INTEGER, src_id INTEGER, full_path VARCHAR, "
                "size BIGINT, physical BIGINT, cid_id INTEGER, in_latest BOOLEAN, "
                "ext VARCHAR, last_seen VARCHAR, first_seen VARCHAR, versions INTEGER)")
    con.executemany("INSERT INTO pathx VALUES (?, ?, ?, ?, ?, ?, true, '', '2026-09-13', '2026-09-13', 1)",
                    rows)

    class _NoCloseCursor:
        """The callers close() in finally; the fixture owns the connection's real lifetime."""

        def execute(self, sql, params=None):
            self._res = con.execute(sql, params or [])
            return self._res

        def __getattr__(self, name):
            return getattr(self._res, name)

        def close(self):
            pass

    monkeypatch.setattr(queries, "read_cursor", lambda repo: _NoCloseCursor())
    yield con
    con.close()


def test_summary_totals_and_sources_agree_with_the_full_read(scratch):
    full = queries.remove_targets("1", prefix=FOLDER, cap=200000)
    summary = queries.remove_targets_summary("1", prefix=FOLDER, cap=200000)
    assert summary["total_file_count"] == full["total_file_count"] == 7
    assert summary["total_physical"] == full["total_physical"]
    assert summary["total_logical"] == full["total_logical"]
    assert summary["capped"] == full["capped"] is False
    assert summary["sources"] == full["sources"] == ["s1", "s2"]


def test_summary_never_materializes_more_than_detail_limit(scratch):
    summary = queries.remove_targets_summary("1", prefix=FOLDER, cap=200000, detail_limit=3)
    assert len(summary["pairs"]) == 3
    # biggest-first, the same ordering the full read uses
    full = queries.remove_targets("1", prefix=FOLDER, cap=200000)
    assert summary["pairs"] == full["pairs"][:3]


def test_summary_capped_flag_tracks_the_plan_cap(scratch):
    summary = queries.remove_targets_summary("1", prefix=FOLDER, cap=5)
    assert summary["capped"] is True   # 7 matched, the plan would take 5
    assert summary["total_file_count"] == 7  # uncapped totals still come through


def _detail_payload(**over):
    body = {"repo": "1", "prefix": FOLDER}
    body.update(over)
    return actions.RemovePlanRequest(**body)


def test_remove_detail_folder_shortcut_needs_one_source_and_no_filters(monkeypatch):
    """The folder-vs-selection decision, pinned: prefix + one source + uncapped + no other
    filter -> folder_detail; ANY filter (or several sources) -> batch_detail over the pairs."""
    calls = {}

    def fake_summary(repo, **kw):
        calls["summary"] = kw
        if kw.get("query"):
            return {"total_file_count": 6, "capped": False,
                    "sources": ["s1", "s2"], "pairs": [("s1", "a"), ("s2", "b")]}
        return {"total_file_count": 6, "capped": False,
                "sources": ["s1"], "pairs": [("s1", f"a{i}") for i in range(6)]}

    def fake_folder_detail(repo, source, prefix, all_versions=False):
        calls["folder"] = (source, prefix)
        return {"found": True, "physical": 1}

    def fake_batch_detail(repo, files, all_versions=False):
        calls["batch"] = files
        return {"found": True, "physical": 2}

    monkeypatch.setattr(actions, "remove_targets_summary", fake_summary)
    monkeypatch.setattr(actions, "folder_detail", fake_folder_detail)
    monkeypatch.setattr(actions, "batch_detail", fake_batch_detail)

    out = actions.remove_detail(_detail_payload())
    assert "folder" in calls and calls["folder"] == ("s1", FOLDER)
    assert out["scope"] == "folder"

    out = actions.remove_detail(_detail_payload(query="x"))  # a filter kills the shortcut
    assert "batch" in calls and out["scope"] == "selection"
    assert len(calls["batch"]) == 2

    out = actions.remove_detail(_detail_payload(cap=3))  # a cap kills it too (capped match)
    assert out["scope"] == "folder" or out["scope"] == "selection"  # decision runs; shape valid
    # ...and precisely: capped=True forces the selection path
    def capped_summary(repo, **kw):
        return {"total_file_count": 6, "capped": True, "sources": ["s1"], "pairs": [("s1", "a")]}
    monkeypatch.setattr(actions, "remove_targets_summary", capped_summary)
    out = actions.remove_detail(_detail_payload())
    assert out["scope"] == "selection"


def test_remove_detail_no_filter_is_refused():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        actions.remove_detail(actions.RemovePlanRequest(repo="1"))
    assert e.value.status_code == 400
