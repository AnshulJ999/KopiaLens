"""Snapshots holding a path (walk-cache resolution, not the delta index) and the Delete
panel's scope rules."""

from __future__ import annotations

from app.api import deleteplan
from app.index import holding

SRC = "me@pc:C:\\Data"

# dir objects: root -> Work -> notes.txt (content differs between snapshots)
TREE = {
    "r1": [{"name": "Work", "type": "d", "obj": "w1"}],
    "r2": [{"name": "Work", "type": "d", "obj": "w2"}],
    "r3": [{"name": "Other", "type": "d", "obj": "o3"}],
    "w1": [{"name": "notes.txt", "type": "f", "obj": "c1", "size": 10}],
    "w2": [{"name": "NOTES.txt", "type": "f", "obj": "c2", "size": 12}],
    "o3": [],
}

SNAPS = [
    {"id": "s1", "startTime": "2026-01-01T00:00:00Z", "rootEntry": {"obj": "r1"}, "pins": ["keep"]},
    {"id": "s2", "startTime": "2026-02-01T00:00:00Z", "rootEntry": {"obj": "r2"}, "retention": ["latest-1"]},
    {"id": "s3", "startTime": "2026-03-01T00:00:00Z", "rootEntry": {"obj": "r3"}},
]


def _patch(monkeypatch):
    calls = []

    def fake_children(repo, db, oids):
        calls.append(list(oids))
        return {o: TREE.get(o, []) for o in oids}

    monkeypatch.setattr(holding, "cache_db", lambda repo: None)
    monkeypatch.setattr(holding, "get_children", fake_children)
    return calls


def test_holding_lists_only_snapshots_that_contain_the_path(monkeypatch):
    calls = _patch(monkeypatch)
    rows = holding.holding_snapshots("3", SRC, ["C:\\Data\\Work\\notes.txt"], snaps=SNAPS)
    got = rows["C:\\Data\\Work\\notes.txt"]
    assert [r["snapshot_id"] for r in got] == ["s2", "s1"]          # s3 lacks the path; newest first
    assert got[0]["obj"] == "c2" and got[1]["pinned"] is True        # case-insensitive source matched NOTES.txt
    assert got[0]["retention"] == ["latest-1"]
    assert calls == [["r1", "r2", "r3"], ["w1", "w2"]]               # one batched lookup per depth


def test_holding_outside_source_root_is_empty(monkeypatch):
    _patch(monkeypatch)
    assert holding.holding_snapshots("3", SRC, ["D:\\x.txt"], snaps=SNAPS) == {"D:\\x.txt": []}


def test_filename_scope_is_the_paths_holders_not_content_sources(monkeypatch):
    facts = {"holder_union": ["A"], "oids": ["c1"], "names": ["notes.txt"]}
    monkeypatch.setattr(deleteplan, "sources_for_content", lambda repo, oids: ["A", "B"])
    assert deleteplan.resolve_scope("3", "filename", None, facts) == ["A"]
    assert deleteplan.resolve_scope("3", "object-id", None, facts) == ["A", "B"]
    assert deleteplan.resolve_scope("3", "filename", ["B", "A"], facts) == ["A", "B"]
