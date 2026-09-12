"""Exclusion-target resolution ( Option A) - owning policies, matched covers, ranking.

The fixture mirrors a real example: model.safetensors sits under three sources where two
own rule lists and the third inherits from one of them. The naive picker would offer all three;
the resolver must offer two, and - for anchored shapes - must NOT claim the write covers the
inheriting source (inherited rules re-anchor per walk root), while unanchored shapes do.
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

DEFINED = {
    "(global)": {"files": {"ignore": ["g1-rule"]}},
    G: {"files": {"ignore": ["GitHub"]}},
    GITHUB: {"files": {"ignore": ["*.safetensors"]}},
    f"{UH}:C:{SEP}": {"files": {"ignore": ["c-rule"]}},
}
SOURCES = [G, GITHUB, PS, f"{UH}:C:{SEP}", LAPTOP]


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(policytargets, "sources_index", lambda repo: {k: [] for k in SOURCES})
    import app.core.policy as pol
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": DEFINED})
    impact = {G: (1, 40_000), GITHUB: (3, 900_000)}

    def fake_price(repo, source, patterns, limit=0, use_cache=True):
        files, byts = impact.get(source, (1, 10))
        return {"source": source, "slow_path": False,
                "results": [{"pattern": p, "files": files, "bytes": byts, "dirs": 0,
                             "examples": [], "error": None, "negated": False} for p in patterns]}

    import app.index.policypricing as pp
    monkeypatch.setattr(pp, "price_patterns", fake_price)


def _targets(out):
    return {t["target"]: t for t in out["targets"]}


def test_owners_only_deduped(wired):
    out = resolve_targets("1", FILE, source=GITHUB)
    t = _targets(out)
    # three covering sources -> two owning targets; the inheriting source is NOT offered
    assert set(t) == {G, GITHUB}
    assert out["sources"] == [
        {"source": G, "root": G[len(UH) + 1:], "origin": "own", "owner": G},
        {"source": GITHUB, "root": GITHUB[len(UH) + 1:], "origin": "own", "owner": GITHUB},
        {"source": PS, "root": PS[len(UH) + 1:], "origin": "inherited", "owner": GITHUB},
    ]


def test_anchored_exact_does_not_claim_the_inheriting_source(wired):
    """The re-anchoring semantic: '/Example-Repo/...' written at G:\\GitHub matches the
    file in G:\\GitHub's walk but NOT in Example-Repo's (where it would mean
    ...\\Example-Repo\\Example-Repo\\...)."""
    t = _targets(resolve_targets("1", FILE, source=GITHUB, shape="exact", price=True))
    gh = t[GITHUB]
    assert gh["pattern"] == "/Example-Repo/Stash/model.safetensors"
    covers = {c["source"]: c["matches"] for c in gh["covers"]}
    assert covers == {GITHUB: True, PS: False}
    # impact only counts the matching cover
    assert gh["impact"]["by_source"] == [{"source": GITHUB, "files": 3, "bytes": 900_000}]


def test_unanchored_name_shape_covers_every_inheritor(wired):
    t = _targets(resolve_targets("1", FILE, source=GITHUB, shape="name", price=True))
    gh = t[GITHUB]
    assert gh["pattern"] == "model.safetensors"
    assert gh["pattern_anchored"] is False
    covers = {c["source"]: c["matches"] for c in gh["covers"]}
    assert covers == {GITHUB: True, PS: True}
    assert gh["impact"]["bytes"] == 900_010  # both covers priced


def test_targets_are_not_priced_by_default(wired):
    """the dialog ranks by covers count - the default call must stay a pure
    resolution with zero index scans."""
    out = resolve_targets("1", FILE, source=GITHUB, shape="exact")
    assert all(t["impact"] is None for t in out["targets"])
    assert out["ranked_by"] == "sources-covered"


def test_drive_root_target_prices_its_own_scope(wired):
    t = _targets(resolve_targets("1", FILE, source=GITHUB, shape="exact"))
    root = t[G]
    assert root["pattern"] == "/GitHub/Example-Repo/Stash/model.safetensors"
    assert [c["source"] for c in root["covers"]] == [G]
    assert root["covers"][0]["matches"] is True


def test_ranking_by_matched_reach(wired):
    """rank by sources covered (free), matched covers first. G:\\GitHub
    reaches two sources (one matching) vs G:\\'s one."""
    out = resolve_targets("1", FILE, source=GITHUB, shape="name")
    ranked = [t["target"] for t in out["targets"]]
    assert ranked[0] == GITHUB and ranked[1] == G


def test_global_target_for_global_inheritors(monkeypatch):
    """A covering source that inherits global dedupes into ONE global target; its anchored
    pattern is framed relative to the dialog's own source."""
    dfile = f"D:{SEP}report.txt"
    dsrc = f"{UH}:D:{SEP}"
    monkeypatch.setattr(policytargets, "sources_index", lambda repo: {dsrc: []})
    import app.core.policy as pol
    monkeypatch.setattr(pol, "load_defined_policies",
                        lambda repo: {"policies": {"(global)": {"files": {"ignore": ["g1-rule"]}}}})
    import app.index.policypricing as pp
    monkeypatch.setattr(pp, "price_patterns",
                        lambda repo, source, patterns, limit=0, use_cache=True:
                        {"source": source, "slow_path": False,
                         "results": [{"pattern": p, "files": 1, "bytes": 10, "dirs": 0,
                                      "examples": [], "error": None, "negated": False}
                                     for p in patterns]})
    out = resolve_targets("1", dfile, source=dsrc, shape="exact")
    t = _targets(out)
    assert set(t) == {"global"}
    g = t["global"]
    assert g["pattern"] == "/report.txt"          # relative to D:\, the dialog's source
    assert g["covers"] == [{"source": dsrc, "root": "D:\\", "rel_path": "report.txt",
                            "matches": True}]
    assert g["existing_rule_count"] == 1
    assert "own lists are NOT affected" in g["note"]


def test_none_origin_offers_the_source_as_will_create(wired, monkeypatch):
    """Edge case: no rules anywhere (global empty too) -> the source itself is the
    only safe target; creating its list shadows nothing."""
    import app.core.policy as pol
    dsrc = f"{UH}:D:{SEP}"
    dfile = f"D:{SEP}report.txt"
    empty_global = {"(global)": {}}
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": empty_global})
    monkeypatch.setattr(policytargets, "sources_index",
                        lambda repo: {**{k: [] for k in SOURCES}, dsrc: []})
    out = resolve_targets("1", dfile, source=dsrc, shape="exact")
    t = _targets(out)
    assert set(t) == {dsrc}
    assert t[dsrc]["will_create_policy"] is True
    assert "shadows nothing" in t[dsrc]["note"]


def test_no_host_filter_picks_up_other_hosts(wired):
    out = resolve_targets("1", FILE)  # no source -> the laptop source covers too
    t = _targets(out)
    # other@laptop:G:\GitHub defines nothing and has no defined ancestor -> global owns it
    assert "global" in t
    gh_laptop = [c for c in t["global"]["covers"] if c["source"] == LAPTOP]
    assert gh_laptop and gh_laptop[0]["matches"] is True


def test_unknown_path_returns_empty_with_note(wired):
    out = resolve_targets("1", f"X:{SEP}nowhere{SEP}f.txt", source=f"{UH}:X:{SEP}")
    assert out["targets"] == [] and "no configured source" in out["note"]


def test_folder_exact_shape_uses_the_doublestar(wired):
    t = _targets(resolve_targets("1", f"G:{SEP}GitHub{SEP}Example-Repo{SEP}Stash",
                                 source=GITHUB, is_folder=True, shape="exact"))
    assert t[GITHUB]["pattern"] == "/Example-Repo/Stash/**"


def test_extension_shape_and_folder_not_applicable(wired):
    out = resolve_targets("1", FILE, source=GITHUB, shape="extension")
    assert _targets(out)[GITHUB]["pattern"] == "*.safetensors"
    out_f = resolve_targets("1", f"G:{SEP}GitHub{SEP}Example-Repo{SEP}Stash",
                            source=GITHUB, is_folder=True, shape="extension")
    assert all(t["pattern"] is None and t["impact"] is None for t in out_f["targets"])


def test_bad_shape_is_a_target_error(wired):
    with pytest.raises(TargetError):
        resolve_targets("1", FILE, shape="bogus")


# ---- the API route ---------------------------------------------------------------------

def test_route_wiring_and_validation(wired, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(api, "kopia_repos", lambda: ["1", "2"])
    monkeypatch.setattr(api, "repo_kind", lambda r: "backblaze" if r == "bz" else "kopia")
    out = api.exclusion_targets(api.TargetsBody(repo="1", path=FILE, source=GITHUB))
    assert {t["target"] for t in out["targets"]} == {G, GITHUB}
    with pytest.raises(HTTPException) as e:
        api.exclusion_targets(api.TargetsBody(repo="1", path=FILE, shape="bogus"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        api.exclusion_targets(api.TargetsBody(repo="bz", path=FILE))
    assert e.value.status_code == 400
