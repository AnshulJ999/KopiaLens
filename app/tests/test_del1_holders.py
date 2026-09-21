""" the filename collateral on a plan, and [B4] the tree holders route."""

from __future__ import annotations

from app.api import actions, tree

SEP = chr(92)
FOLDER = f"G:{SEP}Apps"


# ---- DEL1 --------------------------------------------------------------------------------------

def _plan(**kw):
    return actions.RemovePlanRequest(repo="3", prefix=FOLDER, by="filename", **kw)


def test_a_filename_plan_reports_what_rides_along(monkeypatch):
    targets = {"names": ["config.json"], "pairs": [(f"u@h:G:{SEP}", FOLDER + SEP + "config.json")],
               "files": [], "object_ids": ["o1"], "sources": [f"u@h:G:{SEP}"],
               "file_count": 1, "total_file_count": 1, "total_physical": 0, "total_logical": 0,
               "capped": False, "logical": 0, "physical": 0}
    monkeypatch.setattr(actions, "_remove_targets", lambda payload: targets)
    monkeypatch.setattr(actions, "remove_files_chunked_commands", lambda *a, **k: [["kopia", "x"]])
    monkeypatch.setattr(actions.audit, "audit", lambda *a, **k: None)
    sweep = {"rows": [{"source": f"u@h:G:{SEP}", "full_path": FOLDER + SEP + "config.json",
                       "size": 1, "in_latest": True},
                      {"source": f"u@h:D:{SEP}", "full_path": f"D:{SEP}other{SEP}config.json",
                       "size": 2, "in_latest": True},
                      {"source": f"u@h:D:{SEP}", "full_path": f"D:{SEP}x{SEP}config.json",
                       "size": 3, "in_latest": True}],
             "total": 3}
    monkeypatch.setattr(actions, "paths_named", lambda repo, sources, names, cap: sweep)

    out = actions.remove_plan(_plan())
    assert out["filename_match_total"] == 3
    assert out["filename_extra_count"] == 2                       # the plan's own file is not "extra"
    assert [r["full_path"] for r in out["filename_extra"]] == [f"D:{SEP}other{SEP}config.json",
                                                              f"D:{SEP}x{SEP}config.json"]
    assert out["filename_extra_capped"] is False


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
