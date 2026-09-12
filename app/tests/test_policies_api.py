"""The Policies API surface - routes called directly with fakes at the kopia boundary.

Pins the contract the tab builds against:
inheritance state in overview/detail, the fail-closed save, and diff validation (an inherited
rule is NOT removable on the source - the global policy owns it).
"""

from __future__ import annotations

import pytest

from app.api import policies as api
from app.core.policybackup import BackupError

SEP = chr(92)
SRC = f"Anshul@anshul-desktop:C:{SEP}Src"
OTHER = f"Anshul@anshul-desktop:C:{SEP}Other"
DEFINED = {
    "(global)": {"files": {"ignore": ["*.log", "node_modules"], "noParentIgnore": False}},
    SRC: {"files": {"ignore": ["/Temp/**"], "noParentIgnore": False}},
}
EFFECTIVE = {
    SRC: {"files": {"ignore": ["/Temp/**"], "noParentIgnore": False},
          "scheduling": {"intervalSeconds": 21600}, "retention": {}, "compression": {}},
    OTHER: {"files": {"ignore": ["*.log", "node_modules"], "noParentIgnore": False},
            "scheduling": {}, "retention": {}, "compression": {}},
}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(api, "kopia_repos", lambda: ["1", "2"])
    monkeypatch.setattr(api, "repo_kind", lambda r: "backblaze" if r == "bz" else "kopia")
    monkeypatch.setattr(api, "load_policies", lambda repo: {"policies": EFFECTIVE})
    monkeypatch.setattr(api, "load_defined_policies", lambda repo: {"policies": DEFINED})
    monkeypatch.setattr(api, "refresh_defined_policies",
                        lambda repo, force=False: ({"policies": DEFINED}, True))
    monkeypatch.setattr(api.policypricing, "price_patterns", _fake_price)


def _fake_price(repo, source_key, patterns, limit=10, use_cache=True):
    return {"source": source_key, "slow_path": False,
            "results": [{"pattern": p, "files": 3, "bytes": 700, "dirs": 1,
                         "examples": [{"path": f"{source_key}\\x", "size": 700}],
                         "error": None, "negated": False} for p in patterns]}


def test_overview_reports_inheritance_state(wired):
    out = api.overview(repo="1")
    assert out["stale"] is False
    assert out["global"]["rule_count"] == 2
    by_key = {s["source"]: s for s in out["sources"]}
    assert by_key[SRC]["effective_origin"] == "own"
    assert by_key[SRC]["global_shadowed"] is True
    assert by_key[OTHER]["effective_origin"] == "global"
    assert by_key[OTHER]["global_shadowed"] is False
    assert by_key[OTHER]["effective_rule_count"] == 2


def test_overview_refuses_a_non_kopia_repo(wired):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api.overview(repo="bz")
    assert e.value.status_code == 400


def test_detail_marks_inherited_rules_with_their_origin(wired):
    out = api.detail(repo="1", target=OTHER)
    assert out["rules"]["own"] == []
    assert out["rules"]["inherited"] == ["*.log", "node_modules"]
    assert out["rules"]["inherited_origin"] == "global"
    assert out["rules"]["inherited_active"] is True
    assert out["summarized"]["schedule"] == "inherited"  # OTHER defines no schedule


def test_detail_of_a_source_with_own_rules(wired):
    out = api.detail(repo="1", target=SRC)
    assert out["rules"]["own"] == ["/Temp/**"]
    assert out["rules"]["inherited"] == []
    assert out["is_global"] is False


def test_price_passes_through_with_totals(wired):
    out = api.price(api.PriceBody(repo="1", target=SRC, patterns=["/Temp/**"], limit=1))
    res = out["results"][0]
    assert res["files"] == 3 and res["bytes"] == 700
    assert res["total_examples"] == 3
    assert res["capped"] is True and len(res["examples"]) == 1


def test_diff_rejects_removing_an_inherited_rule(wired):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api.diff(api.DiffBody(repo="1", target=OTHER,
                              changes=api.Changes(remove=["*.log"])))
    assert e.value.status_code == 400
    assert "inherited" in e.value.detail


def test_diff_impact_and_command(wired):
    out = api.diff(api.DiffBody(repo="1", target=SRC,
                                changes=api.Changes(add=["/saves/**"], remove=["/Temp/**"])))
    assert out["current_rules"] == ["/Temp/**"]
    assert out["resulting_rules"] == ["/saves/**"]
    assert [r["pattern"] for r in out["impact"]["added"]] == ["/saves/**"]
    assert [r["pattern"] for r in out["impact"]["removed"]] == ["/Temp/**"]
    assert "--add-ignore" in out["command"] and "--remove-ignore" in out["command"]
    assert out["will_create_policy"] is False
    assert out["backup_dir"].endswith("policy-backups" + SEP + "repo1")


def test_save_refuses_without_confirmation(wired):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api.save(api.SaveBody(repo="1", target=SRC, changes=api.Changes(add=["/x"])))
    assert e.value.status_code == 422


def test_save_is_fail_closed_on_a_backup_failure(wired, monkeypatch):
    from fastapi import HTTPException

    def boom(repo, trigger):
        raise BackupError("export truncated")

    executed = {"called": False}

    def fail_execute(*a, **kw):
        executed["called"] = True
        raise AssertionError("the write must not run")

    monkeypatch.setattr(api.policybackup, "create_backup", boom)
    monkeypatch.setattr(api, "policy_set_execute", fail_execute)
    with pytest.raises(HTTPException) as e:
        api.save(api.SaveBody(repo="1", target=SRC, changes=api.Changes(add=["/x"]),
                              confirmed=True))
    assert e.value.status_code == 500
    assert "fail-closed" in e.value.detail
    assert executed["called"] is False


def test_save_runs_backup_write_then_invalidation(wired, monkeypatch):
    order = []
    monkeypatch.setattr(api.policybackup, "create_backup",
                        lambda repo, trigger: order.append(f"backup:{trigger}") or {
                            "name": "x_pre-write.json", "path": "p", "verified": True})
    monkeypatch.setattr(api, "policy_set_execute",
                        lambda repo, target=None, global_=False, add_ignore=None,
                        remove_ignore=None: order.append("write") or {
                            "returncode": 0, "stdout": "done", "stderr": "",
                            "command": ["kopia", "policy", "set"]})
    monkeypatch.setattr(api, "invalidate_after_write",
                        lambda repo, targets=None: order.append(f"invalidate:{targets}"))
    out = api.save(api.SaveBody(repo="1", target=SRC, changes=api.Changes(add=["/saves/**"]),
                                confirmed=True))
    assert order[0] == "backup:pre-write"
    assert order[1] == "write"
    assert order[2] == f"invalidate:{[SRC]}"  # exactly the saved target, nothing else
    assert out["ok"] is True and out["returncode"] == 0
    assert out["backup"]["verified"] is True
    assert out["diff"]["resulting_rules"] == ["/Temp/**", "/saves/**"]


def test_restore_endpoint_flows_through_the_backup_module(wired, monkeypatch):
    seen = {}

    def fake_restore(repo, name, mirror=False):
        seen.update({"repo": repo, "name": name, "mirror": mirror})
        return {"returncode": 0, "stdout": "", "stderr": "", "command": ["kopia", "policy", "import"],
                "pre_restore_backup": {"name": "pre.json", "path": "p", "verified": True}}

    monkeypatch.setattr(api.policybackup, "restore_backup", fake_restore)
    out = api.restore(api.RestoreBody(repo="1", name="20260912_040404_manual.json",
                                      mode="mirror", confirmed=True))
    assert seen == {"repo": "1", "name": "20260912_040404_manual.json", "mirror": True}
    assert out["ok"] is True
    assert out["pre_restore_backup"]["name"] == "pre.json"


def test_backup_file_404s_for_unknown_names(wired, monkeypatch):
    from fastapi import HTTPException

    def raise_missing(repo, name):
        raise FileNotFoundError(f"unknown backup {name!r}")

    monkeypatch.setattr(api.policybackup, "read_backup", raise_missing)
    with pytest.raises(HTTPException) as e:
        api.backup_file(repo="1", name="nope.json")
    assert e.value.status_code == 404


def test_restore_cmd_carries_the_mirror_flag(wired, monkeypatch):
    monkeypatch.setattr(api.policybackup, "_validated_path",
                        lambda repo, name: rf"C:{SEP}backup{SEP}{name}")
    out = api.restore_cmd(repo="1", name="20260912_040404_manual.json", mode="mirror")
    assert "--delete-other-policies" in out["command"]


# ----: live-price cap, opt-in target pricing, stored numbers ---------------------

def test_live_pricing_caps_large_rule_sets(wired):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api.price(api.PriceBody(repo="1", target=SRC,
                                patterns=[f"p{i}" for i in range(33)]))
    assert e.value.status_code == 400
    assert "stored-prices" in e.value.detail
    # at the cap exactly, it still prices
    out = api.price(api.PriceBody(repo="1", target=SRC, patterns=[f"p{i}" for i in range(32)]))
    assert len(out["results"]) == 32


def test_targets_default_to_no_pricing(wired):
    out = api.exclusion_targets(api.TargetsBody(repo="1", path=SRC + SEP + "x" + SEP + "a.tmp", source=SRC))
    assert all(t["impact"] is None for t in out["targets"])
    assert out["ranked_by"] == "sources-covered"


def test_stored_prices_route_serves_the_nightly_file(wired, monkeypatch):
    monkeypatch.setattr(api.policypricing, "stored_for_target", lambda repo, target: {
        "target": target, "stored": True, "ts": 123.0,
        "results": [{"pattern": "*.tmp", "files": 4, "bytes": 950, "dirs": 0,
                     "examples": [], "total_examples": 4, "capped": False,
                     "error": None, "negated": False}]})
    out = api.stored_prices(repo="1", target="global")
    assert out["stored"] is True and out["results"][0]["files"] == 4
    assert out["repo"] == "1"


def test_stored_prices_route_reports_absence(wired, monkeypatch):
    monkeypatch.setattr(api.policypricing, "stored_for_target", lambda repo, target: None)
    out = api.stored_prices(repo="1", target="global")
    assert out["stored"] is False and out["results"] == []
