"""Exclusion targets: Smart picks, before/after coverage per source, both kopia rule layers.

Fixture: G:\\ owns the rule list; G:\\GitHub and G:\\GitHub\\Example-Repo are sources that inherit
it. The file lives under all three.
"""

from __future__ import annotations

import pytest

from app.api import policies as api
from app.core import policytargets
from app.core.policytargets import TargetError, resolve_targets

SEP = chr(92)
UH = "Anshul@anshul-desktop"
G = f"{UH}:G:{SEP}"
GITHUB = f"{UH}:G:{SEP}GitHub"
PS = f"{GITHUB}{SEP}Example-Repo"
LAPTOP = f"other@laptop:G:{SEP}GitHub"
FILE = f"G:{SEP}GitHub{SEP}Example-Repo{SEP}Stash{SEP}model.safetensors"
STASH = f"G:{SEP}GitHub{SEP}Example-Repo{SEP}Stash"

DEFINED = {
    "(global)": {"files": {"ignore": ["g1-rule"]}},
    G: {"files": {"ignore": ["Thumbs.db", "/Temp/**"]}},
    f"{UH}:C:{SEP}": {"files": {"ignore": ["c-rule"]}},
}
SOURCES = [G, GITHUB, PS, f"{UH}:C:{SEP}", LAPTOP]


def _wire(monkeypatch, defined, sources=SOURCES):
    monkeypatch.setattr(policytargets, "sources_index", lambda repo: {k: [] for k in sources})
    import app.core.policy as pol
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})


@pytest.fixture
def wired(monkeypatch):
    _wire(monkeypatch, DEFINED)
    import app.index.policypricing as pp
    impact = {G: (1, 40_000), GITHUB: (2, 500_000), PS: (3, 900_000)}

    def fake_price(repo, source, patterns, limit=0, use_cache=True):
        files, byts = impact.get(source, (1, 10))
        return {"source": source, "slow_path": False,
                "results": [{"pattern": p, "files": files, "bytes": byts, "dirs": 0,
                             "examples": [], "error": None, "negated": False} for p in patterns]}

    monkeypatch.setattr(pp, "price_patterns", fake_price)


def _targets(out):
    return {t["target"]: t for t in out["targets"]}


def _covers(t):
    return {c["source"]: c["matches"] for c in t["covers"]}


def test_smart_picks_the_owner_and_lists_children_and_global(wired):
    out = resolve_targets("1", FILE, source=PS)
    t = _targets(out)
    assert set(t) == {G, GITHUB, PS, "global"}
    assert t[G]["role"] == "owner" and t[G]["smart"] is True and t[G]["uses_count"] == 3
    for child in (GITHUB, PS):
        assert t[child]["role"] == "child" and t[child]["smart"] is False
        assert t[child]["inherits_from"] == G
        assert t[child]["inherited_rules"] == ["Thumbs.db", "/Temp/**"]
    assert t["global"]["role"] == "global" and t["global"]["smart"] is False
    assert out["targets"][0]["target"] == G   # Smart picks first
    assert [s["origin"] for s in out["sources"]] == ["own", "inherited", "inherited"]


def test_anchored_rule_at_the_owner_only_reaches_its_own_root(wired):
    """Inherited rules re-anchor at each inheriting source's root, so an exact path written at
    G:\\ keeps the file out of G:\\ only."""
    t = _targets(resolve_targets("1", FILE, source=PS, shape="exact"))
    assert t[G]["pattern"] == "/GitHub/Example-Repo/Stash/model.safetensors"
    assert _covers(t[G]) == {G: True, GITHUB: False, PS: False}


def test_name_rule_at_the_owner_reaches_every_inheritor(wired):
    t = _targets(resolve_targets("1", FILE, source=PS, shape="name"))
    assert t[G]["pattern"] == "model.safetensors"
    assert _covers(t[G]) == {G: True, GITHUB: True, PS: True}


def test_smart_shape_switches_to_name_when_exact_misses_sources(wired):
    out = resolve_targets("1", FILE, source=PS, shape="exact", suggest=True)
    assert out["smart_shape"] == "name"


def test_smart_shape_stays_exact_when_it_already_reaches_everything(monkeypatch):
    dsrc = f"{UH}:D:{SEP}"
    _wire(monkeypatch, {"(global)": {}, dsrc: {"files": {"ignore": ["x"]}}}, [dsrc])
    out = resolve_targets("1", f"D:{SEP}a{SEP}b.txt", source=dsrc, suggest=True)
    assert out["smart_shape"] == "exact"


def test_child_policy_rules_apply_inside_parent_snapshots(wired):
    """Layer 2: a rule on the PS policy also keeps the file out of G:\\GitHub's and G:\\'s
    snapshots (the walk adds PS's rules when it enters Example-Repo), anchored at PS."""
    t = _targets(resolve_targets("1", FILE, source=PS, shape="exact"))
    assert t[PS]["pattern"] == "/Stash/model.safetensors"
    assert _covers(t[PS]) == {G: True, GITHUB: True, PS: True}


def test_already_excluded_by_a_nested_policy(wired, monkeypatch):
    """PS owns "*.safetensors": the G:\\ and G:\\GitHub walks apply it inside Example-Repo."""
    _wire(monkeypatch, {**DEFINED, PS: {"files": {"ignore": ["*.safetensors"]}}})
    out = resolve_targets("1", FILE, source=PS, shape="exact")
    assert all(s["already_excluded"] for s in out["sources"])
    assert all(t["protects"] == 0 and t["smart"] is False for t in out["targets"])


def test_nested_policy_does_not_reach_files_above_it(monkeypatch):
    """A rule on a subfolder policy never applies above that folder."""
    a = f"{UH}:J:{SEP}A"
    b = f"{a}{SEP}B"
    _wire(monkeypatch, {"(global)": {}, b: {"files": {"ignore": ["drop.txt"]}}}, [a])
    out_below = resolve_targets("1", f"J:{SEP}A{SEP}B{SEP}drop.txt", source=a)
    out_above = resolve_targets("1", f"J:{SEP}A{SEP}drop.txt", source=a)
    assert out_below["sources"][0]["already_excluded"] is True
    assert out_above["sources"][0]["already_excluded"] is False


def test_global_owner_for_global_inheritors(monkeypatch):
    dsrc = f"{UH}:D:{SEP}"
    _wire(monkeypatch, {"(global)": {"files": {"ignore": ["g1-rule"]}}}, [dsrc])
    t = _targets(resolve_targets("1", f"D:{SEP}report.txt", source=dsrc, shape="exact"))
    assert set(t) == {"global", dsrc}
    assert t[dsrc]["role"] == "child" and t[dsrc]["inherits_from"] == "global"
    g = t["global"]
    assert g["role"] == "owner" and g["smart"] is True and g["is_global"] is True
    assert g["pattern"] == "/report.txt"
    assert g["existing_rule_count"] == 1


def test_no_rules_anywhere_defaults_to_the_source_with_global_offered(monkeypatch):
    dsrc = f"{UH}:D:{SEP}"
    _wire(monkeypatch, {"(global)": {}}, [dsrc])
    t = _targets(resolve_targets("1", f"D:{SEP}report.txt", source=dsrc, shape="exact"))
    assert t[dsrc]["role"] == "start" and t[dsrc]["smart"] is True
    assert t[dsrc]["will_create_policy"] is True
    assert t["global"]["smart"] is False
    assert _covers(t["global"]) == {dsrc: True}   # the source would inherit a Global rule


@pytest.mark.parametrize("level", [UH, "@anshul-desktop"])
def test_user_host_and_host_levels_own_the_list(monkeypatch, level):
    dsrc = f"{UH}:D:{SEP}"
    _wire(monkeypatch, {"(global)": {"files": {"ignore": ["g1"]}},
                        level: {"files": {"ignore": ["lvl"]}}}, [dsrc])
    out = resolve_targets("1", f"D:{SEP}x{SEP}report.txt", source=dsrc, shape="exact")
    assert out["sources"][0]["origin"] == "inherited" and out["sources"][0]["owner"] == level
    t = _targets(out)
    assert t[level]["role"] == "owner" and t[level]["smart"] is True
    assert t[level]["pattern"] == "/x/report.txt"
    assert _covers(t[level]) == {dsrc: True}
    assert _covers(t["global"]) == {dsrc: False}   # Global is shadowed by the host-level list


def test_user_host_beats_host_level(monkeypatch):
    from app.core.policy import effective_ignore_origin
    defined = {UH: {"files": {"ignore": ["u"]}}, "@anshul-desktop": {"files": {"ignore": ["h"]}},
               "(global)": {"files": {"ignore": ["g"]}}}
    assert effective_ignore_origin("1", f"{UH}:D:{SEP}", defined) == ("inherited", UH)


def test_no_host_filter_picks_up_other_hosts(wired):
    t = _targets(resolve_targets("1", FILE, shape="name"))
    assert _covers(t["global"])[LAPTOP] is True   # the laptop source inherits Global


def test_price_counts_only_newly_matched_covers(wired):
    t = _targets(resolve_targets("1", FILE, source=PS, shape="exact", price=True))
    assert t[G]["impact"]["by_source"] == [{"source": G, "files": 1, "bytes": 40_000}]
    assert all(t2["impact"] is None or isinstance(t2["impact"], dict) for t2 in t.values())


def test_targets_are_not_priced_by_default(wired):
    out = resolve_targets("1", FILE, source=PS, shape="exact")
    assert all(t["impact"] is None for t in out["targets"])
    assert out["smart_shape"] is None


def test_unknown_path_returns_empty_with_note(wired):
    out = resolve_targets("1", f"X:{SEP}nowhere{SEP}f.txt", source=f"{UH}:X:{SEP}")
    assert out["targets"] == [] and "no configured source" in out["note"]


def test_folder_exact_shape_uses_the_doublestar(wired):
    t = _targets(resolve_targets("1", STASH, source=PS, is_folder=True, shape="exact"))
    assert t[PS]["pattern"] == "/Stash/**"


def test_folder_name_shape_is_the_trailing_slash(wired):
    """A folder name rule must be 'Name/' (any depth, prunes the subtree), never 'Name/**'."""
    t = _targets(resolve_targets("1", STASH, source=PS, is_folder=True, shape="name"))
    assert t[G]["pattern"] == "Stash/" and t[G]["pattern_anchored"] is False
    assert _covers(t[G]) == {G: True, GITHUB: True, PS: True}
    t2 = _targets(resolve_targets("1", FILE, source=PS, shape="name"))
    assert t2[G]["pattern"] == "model.safetensors"


def test_dir_only_pattern_matching_rules():
    from app.core.policytargets import _matches
    assert _matches("Stash/", "a/Stash", True) is True
    assert _matches("Stash/", "a/b/deep/Stash", True) is True
    assert _matches("Stash/", "a/Stash", False) is False
    assert _matches("Stash/**", "Stash", True) is True
    assert _matches("Stash/**", "a/Stash", True) is False


def test_already_excluded_evaluates_rules_as_an_ordered_list(monkeypatch):
    """Rules apply in order, last match wins: a later '!name' re-includes the file."""
    _wire(monkeypatch, {**DEFINED, G: {"files": {"ignore": ["*.safetensors", "!model.safetensors"]}}})
    out = resolve_targets("1", FILE, source=PS, shape="extension")
    assert not any(s["already_excluded"] for s in out["sources"])
    _wire(monkeypatch, {**DEFINED, G: {"files": {"ignore": ["!other.dll"]}}})
    out = resolve_targets("1", FILE, source=PS, shape="extension")
    assert not any(s["already_excluded"] for s in out["sources"])


def test_already_excluded_catches_a_file_under_a_dir_only_rule(monkeypatch):
    _wire(monkeypatch, {**DEFINED, G: {"files": {"ignore": ["Stash/"]}}})
    out = resolve_targets("1", FILE, source=PS, shape="exact")
    assert all(s["already_excluded"] for s in out["sources"])
    assert all(t["protects"] == 0 for t in out["targets"])


def test_extension_shape_and_folder_not_applicable(wired):
    t = _targets(resolve_targets("1", FILE, source=PS, shape="extension"))
    assert t[G]["pattern"] == "*.safetensors"
    out_f = resolve_targets("1", STASH, source=PS, is_folder=True, shape="extension")
    assert all(t2["pattern"] is None and t2["smart"] is False for t2 in out_f["targets"])


def test_bad_shape_is_a_target_error(wired):
    with pytest.raises(TargetError):
        resolve_targets("1", FILE, shape="bogus")


# ---- the API route ---------------------------------------------------------------------

def test_route_wiring_and_validation(wired, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(api, "kopia_repos", lambda: ["1", "2"])
    monkeypatch.setattr(api, "repo_kind", lambda r: "backblaze" if r == "bz" else "kopia")
    out = api.exclusion_targets(api.TargetsBody(repo="1", path=FILE, source=PS, suggest=True))
    assert out["targets"][0]["target"] == G and out["smart_shape"] == "name"
    with pytest.raises(HTTPException) as e:
        api.exclusion_targets(api.TargetsBody(repo="1", path=FILE, shape="bogus"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        api.exclusion_targets(api.TargetsBody(repo="bz", path=FILE))
    assert e.value.status_code == 400


def test_route_refresh_re_exports_policies_only_when_asked(wired, monkeypatch):
    """A policy created outside KopiaLens must be visible when the dialog opens; shape changes
    reuse that export."""
    monkeypatch.setattr(api, "kopia_repos", lambda: ["1"])
    monkeypatch.setattr(api, "repo_kind", lambda r: "kopia")
    calls = []
    monkeypatch.setattr(api, "refresh_defined_policies",
                        lambda repo, force=False: calls.append((repo, force)) or ({"policies": DEFINED}, True))
    api.exclusion_targets(api.TargetsBody(repo="1", path=FILE, source=PS, refresh=True))
    assert calls == [("1", True)]
    api.exclusion_targets(api.TargetsBody(repo="1", path=FILE, source=PS, shape="name"))
    assert calls == [("1", True)]


def test_pricing_scope_skips_a_host_level_target(monkeypatch):
    """A user@host target prices over the sources whose root list it owns - and nothing else."""
    dsrc = f"{UH}:D:{SEP}"
    defined = {UH: {"files": {"ignore": ["u"]}}}
    import app.core.policy as pol
    import app.core.rulelayers as rl
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})
    monkeypatch.setattr(rl, "sources_index", lambda repo: {dsrc: []})
    assert [e["source"] for e in api._pricing_scope("1", UH)] == [dsrc]


def test_folder_that_is_a_source_root_counts_as_left_out_by_its_own_rule(wired):
    """Excluding G:\\GitHub (itself a source): '/**' on its own policy empties that backup, and
    the G:\\ walk applies it inside GitHub too."""
    t = _targets(resolve_targets("1", f"G:{SEP}GitHub", source=GITHUB, is_folder=True, shape="exact"))
    assert t[GITHUB]["pattern"] == "/**"
    assert _covers(t[GITHUB])[GITHUB] is True
    assert _covers(t[GITHUB])[G] is True


def test_path_status_names_the_rule_list_and_whether_it_matches(monkeypatch):
    from app.core.policytargets import path_status
    _wire(monkeypatch, {**DEFINED, G: {"files": {"ignore": ["*.safetensors"]}}})
    s = path_status("1", PS, FILE)
    assert s["origin"] == "inherited" and s["owner"] == G and s["rule_count"] == 1
    assert s["excluded"] is True
    s2 = path_status("1", G, f"G:{SEP}GitHub{SEP}readme.md")
    assert s2["origin"] == "own" and s2["excluded"] is False
    with pytest.raises(TargetError):
        path_status("1", PS, f"D:{SEP}elsewhere.txt")


# ---- rule variants: levels, scopes, similar names --------------------------------------

@pytest.mark.parametrize("name,folder,want", [
    ("ws-2026-09-21.log", False, "ws-*.log"),
    ("Crash 2026-09-21 21-10-44.txt", False, "Crash *.txt"),
    ("data_12", False, "data_*"),
    ("config.json", False, "config*.json"),
    ("cache-2026", True, "cache-*"),
])
def test_similar_leaf(name, folder, want):
    assert policytargets.similar_leaf(name, folder) == want


@pytest.mark.parametrize("shape,level,where,want", [
    ("exact", 0, "any", "/a/b/c/f.log"),
    ("exact", 1, "any", "/a/b/c/**"),
    ("exact", 3, "any", "/a/**"),
    ("exact", 4, "any", "/**"),
    ("exact", 5, "any", None),
    ("parent", 0, "any", "/a/b/c/**"),
    ("name", 0, "any", "f.log"),
    ("name", 2, "under", "/a/b/**/f.log"),
    ("extension", 0, "here", "/a/b/c/*.log"),
    ("extension", 1, "under", "/a/b/c/**/*.log"),
    ("extension", 4, "under", "/**/*.log"),
    ("similar", 0, "any", "f*.log"),
])
def test_pattern_levels_and_scopes(shape, level, where, want):
    assert policytargets._pattern_for_shape(shape, "a/b/c/f.log", False, level, where) == want


def test_parent_shape_at_the_root_is_still_not_applicable():
    assert policytargets._pattern_for_shape("parent", "f.log", False) is None
    assert policytargets._pattern_for_shape("exact", "f.log", False, level=1) == "/**"


def test_folder_levels_resolve_per_target(wired):
    out = resolve_targets("1", FILE, source=PS, shape="exact", level=2)
    t = _targets(out)
    assert t[PS]["pattern"] == "/**"
    assert t[G]["pattern"] == "/GitHub/Example-Repo/**"
    assert [lv["name"] for lv in out["levels"]] == ["model.safetensors", "Stash", "Example-Repo"]
    assert out["levels"][-1]["root"] is True
    assert out["similar_default"] == "model*.safetensors"


def test_global_writes_one_anchored_rule_per_backup_root(monkeypatch):
    d, sub = f"{UH}:D:{SEP}", f"{UH}:D:{SEP}sub"
    _wire(monkeypatch, {"(global)": {"files": {"ignore": ["g1"]}}}, [d, sub])
    path = f"D:{SEP}sub{SEP}a{SEP}f.txt"
    g = _targets(resolve_targets("1", path, source=sub, shape="exact"))["global"]
    assert g["patterns"] == ["/sub/a/f.txt", "/a/f.txt"]
    assert _covers(g) == {d: True, sub: True}
    g2 = _targets(resolve_targets("1", path, source=sub, shape="name"))["global"]
    assert g2["patterns"] == ["f.txt"] and g2["pattern_roots"] == []


def test_smart_picks_the_deepest_folder_policy_that_covers_every_backup(monkeypatch):
    """A folder policy with rules on the path applies inside every walk through it, so one rule
    there keeps the file out of both backups; Smart writes only that one."""
    j, ja = f"{UH}:J:{SEP}", f"{UH}:J:{SEP}A"
    jab = f"{UH}:J:{SEP}A{SEP}B"
    _wire(monkeypatch, {"(global)": {"files": {"ignore": ["g1"]}},
                        ja: {"files": {"ignore": ["a1"]}},
                        jab: {"files": {"ignore": ["b1"]}}}, [j, ja])
    t = _targets(resolve_targets("1", f"J:{SEP}A{SEP}B{SEP}c{SEP}f.txt", source=ja, shape="exact"))
    assert t[jab]["role"] == "folder" and t[jab]["pattern"] == "/c/f.txt"
    assert _covers(t[jab]) == {j: True, ja: True}
    assert [k for k, v in t.items() if v["smart"]] == [jab]


def test_variants_list_rules_for_the_smart_policy(wired, monkeypatch):
    monkeypatch.setattr(api, "kopia_repos", lambda: ["1"])
    monkeypatch.setattr(api, "repo_kind", lambda r: "kopia")
    out = api.exclusion_variants(api.VariantsBody(repo="1", path=FILE, source=PS))
    rows = {r["label"]: r for r in out["variants"]}
    assert rows["Same name anywhere"]["patterns"] == ["model.safetensors"]
    assert rows["Same extension anywhere"]["patterns"] == ["*.safetensors"]
    assert rows["Similar names anywhere"]["spec"]["shape"] == "similar"
    assert rows["This file"]["target"] == G
