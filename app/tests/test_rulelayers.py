"""The shared rule layer (/): the rule lists that apply inside a folder of a source.

Pins the kopia semantics verified against ``fs/ignorefs/ignorefs.go`` and a live repo:
root list + folder policies + dot-ignore files, evaluated level by level (a deeper level can
negate an ancestor), and the dot-ignore LIST being replaced by any policy at the folder - an
unset value blanks it, which is why a ``.kopiaignore`` under a policy folder can be inert.
"""

from __future__ import annotations

import duckdb
import pytest

from app.core import rulelayers as RL

SEP = chr(92)
UH = "Anshul@anshul-desktop"
G = f"{UH}:G:{SEP}"
GITHUB = f"{UH}:G:{SEP}GitHub"
PS = f"{GITHUB}{SEP}Example-Repo"
LAPTOP = f"other@laptop:G:{SEP}GitHub"
SRC = f"{UH}:G:{SEP}Src"
SUB = f"{SRC}{SEP}Sub"


def _wire(monkeypatch, sources, defined):
    monkeypatch.setattr(RL, "sources_index", lambda repo: {k: [] for k in sources})
    import app.core.policy as pol
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})
    monkeypatch.setattr(RL, "_MAP_MEMO", {})


# ---- dot-ignore file names ----------------------------------------------------------------

def test_dot_names_come_from_the_default_and_the_chain(monkeypatch):
    defined = {"(global)": {"files": {"ignoreDotFiles": [".gitignore", ".kopiaignore"]}}}
    _wire(monkeypatch, [SRC], defined)
    assert RL.dot_names_at("1", SRC, f"G:{SEP}Src", defined) == [".gitignore", ".kopiaignore"]


def test_a_policy_without_dot_ignore_files_blanks_the_list_below_it(monkeypatch):
    """Verified on repo 3: entering a folder whose policy does not set ignoreDotFiles leaves the
    list empty, so a `.kopiaignore` under it is never read."""
    defined = {SRC: {"files": {"ignore": ["/x"]}},
               SUB: {"files": {"keepLatest": 3}}}
    _wire(monkeypatch, [SRC], defined)
    assert RL.dot_names_at("1", SRC, f"G:{SEP}Src", defined) == [RL.DEFAULT_DOT_IGNORE]
    assert RL.dot_names_at("1", SRC, SUB.split(":", 1)[1], defined) == []
    # and the value is replaced, not merged, when a policy does set one
    defined[SUB] = {"files": {"ignoreDotFiles": [".customignore"]}}
    assert RL.dot_names_at("1", SRC, SUB.split(":", 1)[1], defined) == [".customignore"]


# ---- layers and evaluation ----------------------------------------------------------------

def _map(folder_path, name, rules, source=SRC):
    """An ignore-file map entry; ``folder_path`` is the plain path (no user@host prefix)."""
    return {"version": RL._MAP_VERSION, "ts": 0.0, "names": [name],
            "files": [{"source": source, "path": folder_path + SEP + name, "name": name,
                       "size": 10, "content_id": "c1", "from": "index", "rules": rules,
                       "error": None}]}


def test_layers_carry_root_policy_and_in_effect_ignore_files(monkeypatch):
    defined = {"(global)": {"files": {"ignoreDotFiles": [".kopiaignore"]}},
               SRC: {"files": {"ignore": ["/Temp/**"]}},
               f"{SRC}{SEP}Deep": {"files": {"ignore": ["*.bak"],
                                             "ignoreDotFiles": [".kopiaignore"]}}}
    _wire(monkeypatch, [SRC], defined)
    ig = _map(f"G:{SEP}Src{SEP}Deep", ".kopiaignore", ["*.log"])
    layers = RL.layers_for_source("1", SRC, defined=defined, ignore_map=ig)
    assert [(l["kind"], l["rel"]) for l in layers] == [("root", ""), ("policy", "Deep"),
                                                       ("ignore_file", "Deep")]
    root, pol, fil = layers
    assert root["rules"] == ["/Temp/**"] and pol["rules"] == ["*.bak"]
    assert fil["rules"] == ["*.log"] and fil["anchor"] == f"G:{SEP}Src{SEP}Deep"


def test_an_ignore_file_under_a_policy_folder_is_not_in_effect(monkeypatch):
    defined = {SRC: {"files": {"ignore": ["/Temp/**"]}},
               f"{SRC}{SEP}Deep": {"files": {"ignore": ["*.bak"], "keepLatest": 1}}}
    _wire(monkeypatch, [SRC], defined)
    ig = _map(f"G:{SEP}Src{SEP}Deep", ".kopiaignore", ["*.log"])
    kinds = [l["kind"] for l in RL.layers_for_source("1", SRC, defined=defined, ignore_map=ig)]
    assert kinds == ["root", "policy"]


def test_excluded_in_uses_the_deepest_level_first_and_a_deeper_negation_wins(monkeypatch):
    """The hierarchical decision: a root rule ignores *.log, a folder policy's "!keep.log"
    re-includes one file under that folder."""
    defined = {SRC: {"files": {"ignore": ["*.log"]}},
               f"{SRC}{SEP}Sub": {"files": {"ignore": ["!keep.log"]}}}
    _wire(monkeypatch, [SRC], defined)
    assert RL.excluded_in("1", SRC, "Sub/a.log", False, defined=defined) is True
    assert RL.excluded_in("1", SRC, "Sub/keep.log", False, defined=defined) is False
    assert RL.excluded_in("1", SRC, "other/a.log", False, defined=defined) is True


def test_excluded_in_counts_ignore_file_rules_only_when_in_effect(monkeypatch):
    defined = {"(global)": {"files": {"ignoreDotFiles": [".kopiaignore"]}},
               SRC: {"files": {"ignore": []}}}
    _wire(monkeypatch, [SRC], defined)
    ig = _map(f"G:{SEP}Src{SEP}Sub", ".kopiaignore", ["*.tmp"])
    assert RL.excluded_in("1", SRC, "Sub/x.tmp", False, defined=defined, ignore_map=ig) is True
    assert RL.excluded_in("1", SRC, "Sub/x.txt", False, defined=defined, ignore_map=ig) is False
    # a policy at the folder that blanks the list makes the same file inert
    defined[f"{SRC}{SEP}Sub"] = {"files": {"keepLatest": 2}}
    assert RL.excluded_in("1", SRC, "Sub/x.tmp", False, defined=defined, ignore_map=ig) is False


def test_applies_at_reports_the_layers_for_the_folder(monkeypatch):
    defined = {"(global)": {"files": {"ignoreDotFiles": [".kopiaignore"]}},
               SRC: {"files": {"ignore": ["/Temp/**"]}},
               f"{SRC}{SEP}Sub": {"files": {"ignore": ["*.bak"]}}}
    _wire(monkeypatch, [SRC], defined)
    got = RL.applies_at("1", SRC, "Sub", defined=defined, ignore_map={"files": []})
    assert [g["kind"] for g in got] == ["root", "policy"]
    assert got[1]["anchor"] == f"G:{SEP}Src{SEP}Sub"


# ---- pricing scopes ------------------------------------------------------------------------

def test_scope_for_target_covers_inheritors_and_the_walk_through_sources(monkeypatch):
    defined = {G: {"files": {"ignore": ["Thumbs.db"]}}}
    _wire(monkeypatch, [G, GITHUB, PS, LAPTOP], defined)
    scope = RL.scope_for_target("1", GITHUB, defined)
    assert scope == [{"source": GITHUB, "anchor": None, "layer": "root"},
                     {"source": G, "anchor": f"G:{SEP}GitHub", "layer": "policy"}]
    # a source on another host never appears, even when its path matches
    assert LAPTOP not in [e["source"] for e in scope]


def test_scope_for_target_without_rules_still_covers_the_target_and_its_walkers(monkeypatch):
    """The nightly skips rule-less targets; the live panel still prices a rule written into one
    against both the target's own walk root and every walk that passes through it."""
    _wire(monkeypatch, [G, GITHUB], {})
    got = [(e["source"], e["anchor"]) for e in RL.scope_for_target("1", GITHUB, {})]
    assert got == [(GITHUB, None), (G, f"G:{SEP}GitHub")]


def test_folder_scope_is_every_walk_reaching_the_folder(monkeypatch):
    _wire(monkeypatch, [G, GITHUB, PS], {})
    got = [e["source"] for e in RL.folder_scope("1", f"G:{SEP}GitHub")]
    assert got == [G, GITHUB]


# ---- discovery ------------------------------------------------------------------------------

def _index_fixture(monkeypatch):
    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, ?)", [SRC])
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    con.execute("INSERT INTO cid VALUES (7, 'deadbeef')")
    con.execute("CREATE TABLE pathx(path_id INTEGER, src_id INTEGER, dir_id INTEGER, name VARCHAR,"
                " ext VARCHAR, size BIGINT, versions INTEGER, first_seen TIMESTAMP,"
                " last_seen TIMESTAMP, in_latest BOOLEAN, cid_id INTEGER, physical BIGINT,"
                " exclusive BIGINT, full_path VARCHAR)")
    con.execute("INSERT INTO pathx VALUES (1, 1, NULL, '.kopiaignore', '', 10, 1, "
                "TIMESTAMP '2025-01-01', TIMESTAMP '2025-01-01', true, 7, 0, NULL, ?)",
                [f"G:{SEP}Src{SEP}.kopiaignore"])
    con.execute("INSERT INTO pathx VALUES (2, 1, NULL, 'other.txt', 'txt', 10, 1, "
                "TIMESTAMP '2025-01-01', TIMESTAMP '2025-01-01', true, 7, 0, NULL, ?)",
                [f"G:{SEP}Src{SEP}other.txt"])
    import app.index.store as store_mod

    class _Cursor:
        generation = None

        def execute(self, *a, **kw):
            return con.execute(*a, **kw)

        def close(self):
            pass

    monkeypatch.setattr(store_mod, "read_cursor", lambda repo: _Cursor())
    return con


def test_discover_index_finds_only_the_declared_names(monkeypatch):
    _index_fixture(monkeypatch)
    rows = RL.discover_index("1", [".kopiaignore"])
    assert [r["path"] for r in rows] == [f"G:{SEP}Src{SEP}.kopiaignore"]
    assert rows[0]["content_id"] == "deadbeef" and rows[0]["from"] == "index"
    assert RL.discover_index("1", [".gitignore"]) == []


def test_build_map_reads_only_changed_files_and_keeps_the_rest(monkeypatch, tmp_path):
    _index_fixture(monkeypatch)
    _wire(monkeypatch, [SRC], {})
    monkeypatch.setattr(RL, "discover_disk", lambda repo, names, source: [])
    import app.core.policy as pol
    monkeypatch.setattr(pol, "load_defined_policies",
                        lambda repo: {"policies": {"(global)": {"files": {
                            "ignoreDotFiles": [".kopiaignore"]}}}})
    monkeypatch.setattr(RL, "_write_map", lambda repo, payload: True)
    monkeypatch.setattr(RL, "cached_map", lambda repo: prev[0])
    monkeypatch.setattr(RL, "_MAP_MEMO", {})
    calls = []
    monkeypatch.setattr(RL, "read_rules",
                        lambda repo, cid: (calls.append(cid), (["*.tmp"], None))[1])

    prev = [None]
    first = RL.build_map("1", names=[".kopiaignore"])
    assert calls == ["deadbeef"] and first["files"][0]["rules"] == ["*.tmp"]

    prev[0] = first                      # same content id -> no re-read
    second = RL.build_map("1", names=[".kopiaignore"])
    assert calls == ["deadbeef"] and second["files"][0]["rules"] == ["*.tmp"]
    assert second["read"] == 0


def test_a_root_ignore_file_is_evaluated_before_a_deeper_policy(monkeypatch):
    """Kopia walks directory by directory: a root-level ignore file comes before a policy on a
    folder below it, so the policy's negation can overturn it (the reverse of grouping by kind)."""
    defined = {SRC: {"files": {"ignore": []}},
               f"{SRC}{SEP}Deep": {"files": {"ignore": ["!keep.log"]}}}
    _wire(monkeypatch, [SRC], defined)
    ig = _map(f"G:{SEP}Src", ".kopiaignore", ["*.log"])
    assert [l["kind"] for l in RL.layers_for_source("1", SRC, defined=defined, ignore_map=ig)] \
        == ["root", "ignore_file", "policy"]
    assert RL.excluded_in("1", SRC, "Deep/keep.log", False, defined=defined, ignore_map=ig) is False

    # The mirror: a negated root file cannot re-include what a deeper policy ignores.
    defined2 = {SRC: {"files": {"ignore": []}},
                f"{SRC}{SEP}Deep": {"files": {"ignore": ["*.log"]}}}
    ig2 = _map(f"G:{SEP}Src", ".kopiaignore", ["!keep.log"])
    assert RL.excluded_in("1", SRC, "Deep/keep.log", False, defined=defined2, ignore_map=ig2) is True


def test_ignore_file_names_match_by_the_sources_case_rule(monkeypatch):
    """kopia asks the filesystem for the dot-ignore name: Windows finds .KOPIAIGNORE, a POSIX
    source does not."""
    posix = f"{UH}:/home/u/src"
    defined = {"(global)": {"files": {"ignoreDotFiles": [".kopiaignore"]}}}
    _wire(monkeypatch, [SRC, posix], defined)
    upper = {"version": RL._MAP_VERSION, "ts": 0.0, "names": [".KOPIAIGNORE"],
             "files": [{"source": SRC, "path": f"G:{SEP}Src{SEP}.KOPIAIGNORE",
                        "name": ".KOPIAIGNORE", "size": 10, "content_id": "c1",
                        "from": "index", "rules": ["*.tmp"], "error": None},
                       {"source": posix, "path": "/home/u/src/.KOPIAIGNORE",
                        "name": ".KOPIAIGNORE", "size": 10, "content_id": "c2",
                        "from": "index", "rules": ["*.tmp"], "error": None}]}
    assert RL.excluded_in("1", SRC, "x.tmp", False, defined=defined, ignore_map=upper) is True
    assert RL.excluded_in("1", posix, "x.tmp", False, defined=defined, ignore_map=upper) is False


def test_the_same_ignore_file_from_two_sources_is_one_layer(monkeypatch):
    defined = {"(global)": {"files": {"ignoreDotFiles": [".kopiaignore"]}}}
    inner = f"{SRC}{SEP}inner"
    _wire(monkeypatch, [SRC, inner], defined)
    path = f"G:{SEP}Src{SEP}inner{SEP}.kopiaignore"
    ig = {"version": RL._MAP_VERSION, "ts": 0.0, "names": [".kopiaignore"],
          "files": [{"source": SRC, "path": path, "name": ".kopiaignore", "size": 10,
                     "content_id": "c1", "from": "index", "rules": ["*.tmp"], "error": None},
                    {"source": inner, "path": path, "name": ".kopiaignore", "size": 10,
                     "content_id": "c1", "from": "disk", "rules": ["*.tmp"], "error": None}]}
    layers = RL.layers_for_source("1", SRC, defined=defined, ignore_map=ig)
    assert [l["kind"] for l in layers] == ["root", "ignore_file"]
