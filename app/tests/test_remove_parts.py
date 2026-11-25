"""Several folders/files in one removal plan: nested parts collapse, results merge, the cap holds."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import actions

A = "me@pc:G:\\"


def _t(pairs, oids, physical=10, capped=False):
    files = [{"host": "pc", "source": s, "sources": [s], "full_path": fp, "physical": physical}
             for s, fp in pairs]
    return {"file_count": len(pairs), "total_file_count": len(pairs), "total_physical": physical * len(pairs),
            "total_logical": 0, "capped": capped, "logical": 0, "physical": physical * len(pairs),
            "object_ids": oids, "names": [fp.rsplit("\\", 1)[-1] for _, fp in pairs], "pairs": pairs,
            "files": files, "sources": sorted({s for s, _ in pairs})}


def test_nested_parts_collapse_into_the_outer_folder():
    parts = actions._remove_parts([
        {"source": A, "prefix": "G:\\Work"},
        {"source": A, "prefix": "G:\\Work\\notes.txt"},
        {"source": None, "prefix": "G:\\Other\\"},
        {"source": A, "prefix": "G:\\Other\\x"},
        {"source": A, "prefix": "G:\\Workbench"},
    ])
    assert parts == [(A, "G:\\Work"), (None, "G:\\Other"), (A, "G:\\Workbench")]


def test_merge_dedupes_pairs_and_objects(monkeypatch):
    monkeypatch.setattr(actions, "_plan_cap", lambda: 100)
    merged = actions._merge_targets([
        _t([(A, "G:\\a\\1"), (A, "G:\\a\\2")], ["o1", "o2"]),
        _t([(A, "G:\\b\\2"), (A, "G:\\a\\2")], ["o2", "o3"]),
    ])
    assert merged["file_count"] == 3
    assert merged["object_ids"] == ["o1", "o2", "o3"]
    assert merged["sources"] == [A]


def test_merge_refuses_over_the_plan_cap(monkeypatch):
    monkeypatch.setattr(actions, "_plan_cap", lambda: 2)
    with pytest.raises(HTTPException):
        actions._merge_targets([_t([(A, "G:\\a\\1"), (A, "G:\\a\\2")], ["o1"]), _t([(A, "G:\\b\\1")], ["o2"])])
