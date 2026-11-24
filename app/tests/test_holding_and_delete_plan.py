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

    def fake_children(repo, oids, cursor=None):
        calls.append(list(oids))
        return {o: TREE.get(o, []) for o in oids}

    monkeypatch.setattr(holding.entries, "children_of", fake_children)
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


def test_holders_stay_on_the_selected_files_machine(monkeypatch):
    mine, nested, other = "me@pc:C:\\", "me@pc:C:\\Users", "me@laptop:C:\\"
    fp = "C:\\Users\\notes.txt"
    monkeypatch.setattr(deleteplan, "holders_for_paths", lambda repo, fps: {fp: [mine, nested, other]})
    seen = []

    def fake_versions(repo, pairs):
        seen.extend(src for src, _fp in pairs)
        return {(src, _fp): [{"content_id": "c-" + src}] for src, _fp in pairs}

    monkeypatch.setattr(deleteplan, "versions_many", fake_versions)   # Batched lookup
    facts = deleteplan.selection_facts("3", [(mine, fp)])
    assert facts["holders"] == [[mine, nested]]
    assert facts["holder_union"] == [mine, nested]
    assert other not in seen and "c-" + other not in facts["oids"]


def test_selection_facts_find_every_version_through_the_real_index(monkeypatch):
    """The panel's default Exact-content plan starts from these content ids. A lookup handed
    source KEYS where the resolver wants integer ids matched nothing, so the plan came back empty."""
    import duckdb
    from app.index import queries
    fp = "C:\\Data\\notes.txt"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE src AS SELECT * FROM (VALUES (1::SMALLINT, '{SRC}')) t(src_id, source)")
    con.execute(f"CREATE TABLE pathx AS SELECT * FROM (VALUES (7::BIGINT, 1::SMALLINT, '{fp}')) "
                "t(path_id, src_id, full_path)")
    con.execute("CREATE TABLE snap AS SELECT * FROM (VALUES "
                "(1::INTEGER, 's1', TIMESTAMP '2026-01-01'), (2, 's2', TIMESTAMP '2026-02-01')) "
                "t(snap_id, snapshot_id, start_time)")
    con.execute("CREATE TABLE cid AS SELECT * FROM (VALUES (10::INTEGER, 'c1'), (20, 'c2')) "
                "t(cid_id, content_id)")
    con.execute("CREATE TABLE filev AS SELECT * FROM (VALUES "
                "(7::BIGINT, 1::INTEGER, 10::UBIGINT, 10::INTEGER), (7, 2, 12::UBIGINT, 20::INTEGER)) "
                "t(path_id, snap_id, size, cid_id)")
    monkeypatch.setattr(queries, "read_cursor", lambda repo: con.cursor())
    monkeypatch.setattr(deleteplan, "holders_for_paths", lambda repo, fps: {fp: [SRC]})
    facts = deleteplan.selection_facts("3", [(SRC, fp)])
    assert facts["oids"] == ["c2", "c1"] and facts["versions"] == 2


def test_snapshot_plan_copies_respect_ticked_backups(monkeypatch):
    src, copy_src, unticked = "me@pc:C:\\", "me@pc:D:\\", "me@pc:E:\\"
    monkeypatch.setattr(deleteplan, "selection_facts", lambda repo, files: {
        "holders": [[src]], "holder_union": [src], "oids": ["c1"], "names": ["a.txt"], "versions": 1})
    monkeypatch.setattr(deleteplan, "sources_for_content", lambda repo, oids: [src, copy_src, unticked])
    asked = {}

    def fake_paths(repo, oids, sources=None, cap=500):
        asked["sources"] = sources
        rows = [{"source": s, "full_path": s.split(":", 1)[1] + "copy.txt"} for s in (sources or [])]
        return {"rows": rows, "total": len(rows)}

    monkeypatch.setattr(deleteplan, "paths_for_contents", fake_paths)
    monkeypatch.setattr(deleteplan, "sources_index", lambda repo: {})
    monkeypatch.setattr(deleteplan, "holding_snapshots", lambda repo, s, fps, snaps=None: {f: [] for f in fps})
    body = deleteplan.PlanBody(repo="3", files=[{"source": src, "full_path": "C:\\a.txt"}],
                               method="snapshots", copies=True, sources=[src, copy_src])
    out = deleteplan.delete_plan(body)
    assert asked["sources"] == [src, copy_src]
    assert set(out["candidates"]) == {src, copy_src, unticked}
    assert out["capped"] is False
