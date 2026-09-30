""" the filename collateral on a plan, and [B4] the tree holders route."""

from __future__ import annotations

from app.api import actions, tree

SEP = chr(92)
FOLDER = f"G:{SEP}Apps"


# ---- DEL1 --------------------------------------------------------------------------------------

def _plan(**kw):
    return actions.RemovePlanRequest(repo="3", prefix=FOLDER, by="filename", **kw)


TARGETS = {"names": ["config.json"], "pairs": [(f"u@h:G:{SEP}", FOLDER + SEP + "config.json")],
           "files": [], "object_ids": ["o1"], "sources": [f"u@h:G:{SEP}"],
           "file_count": 1, "total_file_count": 1, "total_physical": 10, "total_logical": 1,
           "capped": False, "logical": 1, "physical": 10}
SWEEP = {"rows": [{"source": f"u@h:G:{SEP}", "full_path": FOLDER + SEP + "config.json",
                   "size": 1, "physical": 10, "in_latest": True},
                  {"source": f"u@h:D:{SEP}", "full_path": f"D:{SEP}other{SEP}config.json",
                   "size": 2, "physical": 20, "in_latest": True},
                  {"source": f"u@h:D:{SEP}", "full_path": f"D:{SEP}x{SEP}config.json",
                   "size": 3, "physical": 30, "in_latest": True}],
         "total": 3, "logical": 6, "physical": 60}


def test_a_filename_plan_reports_what_rides_along(monkeypatch):
    monkeypatch.setattr(actions, "_remove_targets", lambda payload: TARGETS)
    monkeypatch.setattr(actions, "remove_files_chunked_commands", lambda *a, **k: [["kopia", "x"]])
    monkeypatch.setattr(actions.audit, "audit", lambda *a, **k: None)
    monkeypatch.setattr(actions, "paths_named", lambda repo, sources, names, cap, biggest_first=False: SWEEP)

    out = actions.remove_plan(_plan())
    assert out["filename_match_total"] == 3
    assert out["filename_extra_count"] == 2                       # the plan's own file is not "extra"
    assert [r["full_path"] for r in out["filename_extra"]] == [f"D:{SEP}other{SEP}config.json",
                                                              f"D:{SEP}x{SEP}config.json"]
    assert out["filename_extra_capped"] is False
    assert (out["filename_extra_logical"], out["filename_extra_physical"]) == (5, 50)


def test_a_filename_plan_prices_every_same_named_file(monkeypatch):
    """What it frees covers everything --filename removes, not only the plan's own files."""
    monkeypatch.setattr(actions, "_remove_targets", lambda payload: TARGETS)
    asked = {}

    def fake_named(repo, sources, names, cap, biggest_first=False):
        asked.update(cap=cap, biggest_first=biggest_first)
        return SWEEP

    def fake_detail(repo, files, all_versions=False):
        asked.update(files=[f["full_path"] for f in files], all_versions=all_versions)
        return {"freeable": 1}

    monkeypatch.setattr(actions, "paths_named", fake_named)
    monkeypatch.setattr(actions, "batch_detail", fake_detail)
    out = actions.remove_detail(_plan(all_versions=False))
    assert asked["files"] == [r["full_path"] for r in SWEEP["rows"]]
    assert asked["biggest_first"] is True and asked["cap"] == 5000
    assert asked["all_versions"] is True          # kopia's --filename rewrites every snapshot
    assert out["scope"] == "filename" and out["capped"] is False


def test_filename_matching_is_case_sensitive_like_kopia(monkeypatch):
    """kopia matches --filename with Go's path.Match: Config.json is not config.json."""
    import duckdb
    from app.index import queries
    src = f"u@h:G:{SEP}"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE src AS SELECT * FROM (VALUES (1::SMALLINT, '{src}')) t(src_id, source)")
    con.execute(f"CREATE TABLE pathx AS SELECT * FROM (VALUES "
                f"(1::SMALLINT, 'config.json', 'G:{SEP}a{SEP}config.json', 1::UBIGINT, 10::UBIGINT, true), "
                f"(1, 'Config.json', 'G:{SEP}b{SEP}Config.json', 2::UBIGINT, 20::UBIGINT, true)) "
                f"t(src_id, name, full_path, size, physical, in_latest)")
    monkeypatch.setattr(queries, "read_cursor", lambda repo: con.cursor())
    out = queries.paths_named("3", [src], ["config.json"])
    assert out["total"] == 1 and [r["full_path"] for r in out["rows"]] == [f"G:{SEP}a{SEP}config.json"]


def test_an_object_id_plan_carries_empty_collateral_fields(monkeypatch):
    monkeypatch.setattr(actions, "_remove_targets", lambda payload: {
        "names": [], "pairs": [], "files": [], "object_ids": [], "sources": [],
        "file_count": 0, "total_file_count": 0, "total_physical": 0, "total_logical": 0,
        "capped": False, "logical": 0, "physical": 0})
    out = actions.remove_plan(actions.RemovePlanRequest(repo="3", prefix=FOLDER))
    assert out["filename_match_total"] is None and out["filename_extra_count"] == 0


# ---- B4 ----------------------------------------------------------------------------------------

def test_holders_resolve_per_path_without_detail(monkeypatch):
    monkeypatch.setattr(tree, "holders_for_paths",
                        lambda repo, paths: {p: ["a@h:C:" + SEP, "b@h:C:" + SEP] for p in paths})
    out = tree.tree_holders(tree.HoldersBody(repo="3", full_paths=[f"C:{SEP}x.txt"]))
    assert out["capped"] is False
    assert [h["source"] for h in out["paths"][f"C:{SEP}x.txt"]] == ["a@h:C:" + SEP, "b@h:C:" + SEP]


def test_holders_detail_counts_snapshots_and_caps_the_path_count(monkeypatch):
    monkeypatch.setattr(tree, "holders_for_paths", lambda repo, paths: {p: ["a@h:C:" + SEP] for p in paths})
    monkeypatch.setattr(tree, "holding_snapshots",
                        lambda repo, src, fps, snaps=None: {fps[0]: [{"start_time": "2026-09-20"}] * 3})
    out = tree.tree_holders(tree.HoldersBody(repo="3", full_paths=[f"C:{SEP}x.txt"], detail=True))
    assert out["paths"][f"C:{SEP}x.txt"] == [{"source": "a@h:C:" + SEP, "snapshots": 3,
                                              "latest": "2026-09-20"}]
    many = tree.tree_holders(tree.HoldersBody(repo="3", detail=True,
                                              full_paths=[f"C:{SEP}f{i}" for i in range(30)]))
    assert many["capped"] is True and all(not v for v in many["paths"].values())


def test_holders_route_is_registered():
    paths = {r.path for r in tree.router.routes}
    assert "/tree/holders" in paths and "/tree" in paths
