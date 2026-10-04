"""Free/Pro: a Free install is refused every Pro action and is never sent the commands or ids that
rebuild one; demo repos and a valid licence unlock everything."""

from __future__ import annotations

import ast
import filecmp
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import actions, deleteplan
from app.api import jobs as jobs_api
from app.licensing import features, licence
from app.licensing.licensekit import NO_LICENCE

REAL_CURRENT = licence.current   # captured before conftest's per-test Pro default

# LicenseKit's known-answer vector (test-vectors/licensekit-known-answers.json, key t1).
TEST_KEY = {"t1": "k4.public.6cI1myYTs-J_4927dmvMgRiTHSSryiitduLjFf0g46k"}
LIFETIME = ("v4.public.eyJ2IjoxLCJpZCI6IlRFU1QtTElGRS0wMDAxIiwidG8iOiJUZXN0IEJ1eWVyIiwiZW0iOiJidXllckBleGFtcGxlLmNvbSIsInAiOnsia29waWFsZW5zIjpbInJlY2xhaW0iXX0sInBsYW4iOiJQcm8gwrcgTGlmZXRpbWUiLCJpYXQiOiIyMDI2LTEwLTAxVDAwOjAwOjAwWiIsInVwZCI6Miwic3JjIjoibWFudWFsIn1U_QQse4zqYLwI-WdmIwasOFcG1VRhz_vJU1HlHTZtfWTrQwr_L7HIUPlVCRnzh5Ip-4tmGQ1Ml5iPlSafz-gJ.eyJraWQiOiJ0MSJ9")


@pytest.fixture
def free(monkeypatch):
    monkeypatch.setattr(licence, "current", lambda: NO_LICENCE)
    monkeypatch.setattr(features, "DEMO_INSTANCE", False)
    monkeypatch.setattr(features, "is_demo", lambda repo: False)


def _forbidden(fn, *args, **kwargs) -> str:
    with pytest.raises(HTTPException) as exc:
        fn(*args, **kwargs)
    assert exc.value.status_code == 403
    return exc.value.headers["X-KopiaLens-Pro"]


FILES = actions.RemoveFilesRequest(repo="1", source="u@h:C:\\d", full_path="C:\\d\\a.bin")
PLAN = actions.RemovePlanRequest(repo="1", prefix="C:\\d")


@pytest.mark.parametrize("route,body", [
    (actions.remove_files, FILES),
    (actions.remove_files_dry_run_route, FILES),
    (actions.remove_bat, PLAN),
    (actions.remove_plan_dry_run, PLAN),
])
def test_free_is_refused_every_remove_route(free, route, body):
    assert _forbidden(route, body) == "remove"


@pytest.mark.parametrize("kind,params,feature", [
    ("remove_files", {"form": "files", "dry": True}, "remove"),
    ("policy_write", {"writes": [{"target": "global", "add": ["*.tmp"]}],
                      "then": {"kind": "remove_files", "params": {}}}, "exclude_delete"),
    ("policy_write", {"writes": [{"target": "global", "add": ["*.tmp"]}], "shape": "extension"}, "exclude_smart"),
])
def test_free_cannot_start_pro_jobs(free, kind, params, feature):
    body = jobs_api.JobStartRequest(kind=kind, repo="1", params=params)
    assert _forbidden(jobs_api.start, body) == feature


def test_free_can_start_free_jobs(free, monkeypatch):
    """Exact-item exclude rules, plain snapshot delete and maintenance stay Free."""
    started = []
    monkeypatch.setattr(jobs_api, "_resolve_params", lambda kind, repo, params: params)
    monkeypatch.setattr(jobs_api.jobs, "start_job", lambda kind, *a, **k: started.append(kind) or {"job_id": "x"})
    monkeypatch.setattr(jobs_api.jobs, "job_status", lambda *a, **k: {})
    for kind, params in [("policy_write", {"writes": [{"target": "global", "add": ["/a"]}], "shape": "exact"}),
                         ("snapshot_delete", {"snapshot_ids": ["k1"]}), ("maintenance", {})]:
        jobs_api.start(jobs_api.JobStartRequest(kind=kind, repo="1", params=params))
    assert started == ["policy_write", "snapshot_delete", "maintenance"]


def _fake_plan(monkeypatch):
    t = {"file_count": 1, "total_file_count": 1, "total_physical": 5, "total_logical": 5, "capped": False,
         "logical": 5, "physical": 5, "object_ids": ["k1abc"], "names": ["a.bin"], "pairs": [("u@h:C:\\d", "C:\\d\\a.bin")],
         "files": [{"source": "u@h:C:\\d", "full_path": "C:\\d\\a.bin", "physical": 5, "content_id": "k1abc"}],
         "sources": ["u@h:C:\\d"]}
    monkeypatch.setattr(actions, "_remove_targets", lambda p: t)
    monkeypatch.setattr(actions, "index_generation", lambda repo: 7)
    monkeypatch.setattr(actions, "remove_files_chunked_commands",
                        lambda repo, items, **kw: [["kopia", "snapshot", "fix", "remove-files", *items]])


def test_free_removal_plan_keeps_the_preview_but_not_the_ingredients(free, monkeypatch):
    _fake_plan(monkeypatch)
    out = actions.remove_plan(PLAN)
    assert out["file_count"] == 1 and out["physical"] == 5 and out["preview"][0]["full_path"] == "C:\\d\\a.bin"
    assert out["commit"] == "" and out["dry_run"] == "" and out["plan_token"] is None
    assert "k1abc" not in repr(out)


def test_pro_removal_plan_carries_commands_and_ticket(monkeypatch):
    _fake_plan(monkeypatch)
    out = actions.remove_plan(PLAN)
    assert "k1abc" in out["commit"] and out["plan_token"]


def test_free_purge_plan_lists_snapshots_without_ids(free, monkeypatch):
    held = [{"snapshot_id": "k9snap", "retention": ["latest"], "pinned": False, "start_time": "2026-10-01T00:00:00Z"}]
    monkeypatch.setattr(actions, "snapshots_for_path", lambda *a: held)
    out = actions.purge_plan("1", source="u@h:C:\\d", full_path="C:\\d\\a.bin")
    assert len(out["items"]) == 1 and out["items"][0]["start_time"] == "2026-10-01T00:00:00Z"
    assert "k9snap" not in repr(out)


def _fake_delete_plan(monkeypatch):
    facts = {"oids": ["k1abc"], "names": ["a.bin"], "holders": [["u@h:C:\\d"]], "holder_union": ["u@h:C:\\d"], "versions": 1}
    monkeypatch.setattr(deleteplan, "selection_facts", lambda repo, files: facts)
    monkeypatch.setattr(deleteplan, "sources_for_content", lambda repo, oids: ["u@h:C:\\d"])
    monkeypatch.setattr(deleteplan, "resolve_scope", lambda *a: ["u@h:C:\\d"])
    monkeypatch.setattr(deleteplan, "paths_for_contents", lambda *a, **k: {
        "rows": [{"source": "u@h:C:\\d", "full_path": "C:\\d\\a.bin", "size": 5, "in_latest": True}], "total": 1})
    monkeypatch.setattr(deleteplan, "remove_files_chunked_commands", lambda repo, items, **kw: [["kopia", *items]])
    monkeypatch.setattr(deleteplan, "sources_index", lambda repo: {})
    monkeypatch.setattr(deleteplan, "holding_snapshots", lambda repo, src, fps, snaps: {fps[0]: [
        {"snapshot_id": "k9snap", "start_time": "2026-10-01T00:00:00Z", "pinned": False, "retention": [], "obj": "k1abc"}]})
    monkeypatch.setattr(deleteplan, "snapshot_delete_commands", lambda repo, ids, batch: [["kopia", "snapshot", "delete", *ids]])


@pytest.mark.parametrize("method", ["remove", "snapshots"])
def test_free_delete_panel_preview_has_no_command_or_ids(free, monkeypatch, method):
    _fake_delete_plan(monkeypatch)
    body = deleteplan.PlanBody(repo="1", files=[{"source": "u@h:C:\\d", "full_path": "C:\\d\\a.bin"}], method=method)
    out = deleteplan.delete_plan(body)
    assert out["command"] == "" and "k9snap" not in repr(out.get("snapshots"))
    assert out.get("match_total") == 1 or len(out["snapshots"]) == 1


def test_demo_repos_unlock_everything(free, monkeypatch):
    monkeypatch.setattr(features, "is_demo", lambda repo: repo == "demo")
    assert all(features.allowed(f, "demo") for f in features.PRO)
    assert not any(features.allowed(f, "1") for f in features.PRO)
    assert features.locked() == list(features.PRO)


def test_a_feature_dropped_from_pro_is_free_everywhere(free, monkeypatch):
    monkeypatch.delitem(features.PRO, "remove")
    features.require("remove", "1")   # no longer raises
    assert "remove" not in features.locked()


def test_adding_and_removing_a_licence(monkeypatch, tmp_path):
    monkeypatch.setattr(licence, "current", REAL_CURRENT)
    monkeypatch.setattr(licence, "PUBLIC_KEYS", TEST_KEY)
    monkeypatch.setattr(licence, "SHARED_DIR", tmp_path / "shared")
    monkeypatch.delenv("KOPIALENS_LICENSE", raising=False)
    monkeypatch.delenv("KOPIALENS_LICENSE_FILE", raising=False)
    monkeypatch.setattr(features, "DEMO_INSTANCE", False)
    assert licence.status()["state"] == "none" and not features.allowed("remove", "1")
    with pytest.raises(licence.LicenceError):
        licence.add(LIFETIME[:-6] + "AAAAAA")
    licence.add("\n" + LIFETIME + "\r\n")
    st = licence.status()
    assert (st["state"], st["plan"], st["name"], st["stored"]) == ("active", "Pro · Lifetime", "Test Buyer", True)
    assert features.allowed("remove", "1")
    licence.remove()
    assert licence.status()["state"] == "none" and not features.allowed("remove", "1")


def test_licence_from_an_unknown_key_is_refused(monkeypatch, tmp_path):
    """The shipped build trusts only its own keys: a validly signed licence from another key is not Pro."""
    monkeypatch.setattr(licence, "current", REAL_CURRENT)
    monkeypatch.setattr(licence, "SHARED_DIR", tmp_path / "shared")
    with pytest.raises(licence.LicenceError):
        licence.add(LIFETIME)
    assert licence.status()["state"] == "none"


# Builders whose output is a delete command: any route reaching one must check the Free/Pro list.
_SENSITIVE = {"remove_files_chunked_commands", "remove_files_dry_run", "remove_files_script"}
_API = Path(__file__).resolve().parents[1] / "api"


def _calls(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def test_every_route_that_builds_a_remove_command_checks_pro():
    unguarded = []
    for path in _API.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name, fn in funcs.items():
            if not any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post", "put", "delete")
                       for d in fn.decorator_list):
                continue
            reach, todo = set(), [name]
            while todo:   # in-module helpers the route calls, transitively
                cur = todo.pop()
                if cur in reach:
                    continue
                reach.add(cur)
                todo.extend(c for c in _calls(funcs[cur]) if c in funcs)
            builds = set().union(*(_calls(funcs[r]) for r in reach)) & _SENSITIVE
            checks = {"require", "allowed"} & set().union(*(_calls(funcs[r]) for r in reach))
            if builds and not checks:
                unguarded.append(f"{path.name}:{name}")
    assert unguarded == []


def test_vendored_licensekit_matches_licensekit():
    src = Path(__file__).resolve().parents[4] / "LicenseKit" / "licensekit"
    if not src.is_dir():
        pytest.skip("LicenseKit source not next to KopiaLens")
    dst = Path(licence.__file__).parent / "licensekit"
    names = sorted(p.name for p in src.glob("*.py"))
    assert names == sorted(p.name for p in dst.glob("*.py"))
    assert [n for n in names if not filecmp.cmp(src / n, dst / n, shallow=False)] == []
