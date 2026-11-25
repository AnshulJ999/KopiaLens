"""Anchored pricing and the ignore-file artifact.

A rule list that lives on a folder inside a source is anchored there: it prices only that
subtree and matches text relative to the folder. The ignore-file stage discovers those files,
reads the changed ones in the worker, and prices each file's rules over every walk that reaches
its folder.
"""

from __future__ import annotations

import duckdb
import pytest

from app.core import rulelayers as RL
from app.index import policypricing
from app.tests._tree_fixture import add_tree_tables

SEP = chr(92)
UH = "Anshul@anshul-desktop"
SRC = f"{UH}:C:{SEP}Src"
TEMP = f"C:{SEP}Src{SEP}Temp"
NESTED = f"{TEMP}{SEP}nested"

_FILES = [
    (f"C:{SEP}Src{SEP}keep.txt", "keep", "txt", 100),
    (f"{TEMP}{SEP}a.tmp", "a", "tmp", 200),
    (f"{TEMP}{SEP}b.tmp", "b", "tmp", 300),
    (f"{NESTED}{SEP}c.tmp", "c", "tmp", 400),
    (f"C:{SEP}Src{SEP}other{SEP}x.js", "x", "js", 500),
]


def _build():
    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id SMALLINT, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, ?)", [SRC])
    con.execute("CREATE TABLE cid AS SELECT NULL::INTEGER AS cid_id, NULL::VARCHAR AS content_id WHERE false")
    rows = ", ".join(
        f"({i + 1}::INTEGER, 1::SMALLINT, NULL::INTEGER, '{name}', '{ext}', {size}::UBIGINT, "
        f"1::INTEGER, TIMESTAMP '2025-01-01', TIMESTAMP '2025-01-01', true, NULL::INTEGER, "
        f"0::UBIGINT, NULL::UBIGINT, '{fp}')"
        for i, (fp, name, ext, size) in enumerate(_FILES))
    con.execute(f"CREATE TABLE pathx AS SELECT * FROM (VALUES {rows}) t("
                "path_id, src_id, dir_id, name, ext, size, versions, first_seen, last_seen, "
                "in_latest, cid_id, physical, exclusive, full_path)")
    add_tree_tables(con, [SRC], [(1, fp) for fp, _n, _e, _s in _FILES])
    con.execute("CREATE TABLE snap AS SELECT NULL::INTEGER AS snap_id, NULL::VARCHAR AS snapshot_id, "
                "NULL::TIMESTAMP AS start_time, NULL::SMALLINT AS src_id WHERE false")
    con.execute("CREATE TABLE filev AS SELECT NULL::SMALLINT AS src_id, NULL::BIGINT AS path_id, "
                "NULL::INTEGER AS snap_id, NULL::INTEGER AS cid_id, NULL::UBIGINT AS size, "
                "NULL::TIMESTAMP AS mtime, NULL::BIGINT AS ordinal WHERE false")
    return con


@pytest.fixture
def priced(monkeypatch):
    con = _build()

    class _Gen:
        pass

    class _Cursor:
        def __init__(self, con):
            self._con = con
            self.generation = _Gen()

        def execute(self, *a, **kw):
            return self._con.execute(*a, **kw)

        def close(self):
            pass

    monkeypatch.setattr(policypricing, "read_cursor", lambda repo: _Cursor(con))
    monkeypatch.setattr(policypricing, "_CACHE", {})
    return con


def test_unanchored_price_counts_the_whole_source(priced):
    out = policypricing.price_patterns("1", SRC, ["*.tmp"], limit=10, use_cache=False)
    assert out["results"][0]["files"] == 3 and out["results"][0]["bytes"] == 900


def test_anchored_price_counts_only_the_subtree(priced):
    out = policypricing.price_patterns("1", SRC, ["*.tmp"], limit=10, use_cache=False,
                                       anchor=NESTED)
    res = out["results"][0]
    assert res["files"] == 1 and res["bytes"] == 400
    assert [e["path"] for e in res["examples"]] == [f"{NESTED}{SEP}c.tmp"]


def test_anchored_examples_stay_inside_the_subtree(priced):
    out = policypricing.price_patterns("1", SRC, ["*.js"], limit=10, use_cache=False,
                                       anchor=TEMP)
    assert out["results"][0]["files"] == 0


def test_price_scope_merges_and_breaks_down_per_anchor(priced):
    scope = [{"source": SRC, "anchor": None, "layer": "root"},
             {"source": SRC, "anchor": TEMP, "layer": "policy"}]
    out = policypricing.price_scope("1", scope, ["*.tmp"], limit=10, use_cache=False)
    assert out["results"][0]["files"] == 6 and out["results"][0]["bytes"] == 1800
    assert [b["anchor"] for b in out["breakdown"]] == [None, TEMP]
    assert out["breakdown"][1]["results"]["*.tmp"] == {"files": 3, "bytes": 900, "dirs": 0}
    assert out["scope"]["sources"] == [SRC]      # one entry per BACKUP, not per anchor
    assert out["scope"]["layers"]["folder"] == [SRC]


def test_ignore_file_stage_prices_the_rules_over_the_walking_sources(priced, tmp_path, monkeypatch):
    defined = {"(global)": {"files": {"ignoreDotFiles": [".kopiaignore"]}},
               SRC: {"files": {"ignore": ["/keep/**"]}}}
    import app.core.policy as pol
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})
    monkeypatch.setattr(RL, "sources_index", lambda repo: {SRC: []})
    monkeypatch.setattr(RL, "build_map", lambda repo: {
        "version": RL._MAP_VERSION, "ts": 0.0, "names": [".kopiaignore"],
        "files": [{"source": SRC, "path": f"{TEMP}{SEP}.kopiaignore", "name": ".kopiaignore",
                   "size": 20, "content_id": "c1", "from": "index", "rules": ["*.tmp"],
                   "error": None}]})
    monkeypatch.setattr(policypricing, "DATA_DIR", str(tmp_path))

    payload = policypricing.precompute_ignore_files("1")
    item = payload["files"][0]
    assert item["in_effect"] is True and item["priced"] is True
    assert item["files"] == 3 and item["bytes"] == 900
    assert item["by_source"][0]["source"] == SRC and item["by_source"][0]["anchor"] == TEMP
    assert policypricing.stored_ignore_files("1") is not None


def test_ignore_file_not_in_effect_is_listed_but_not_priced(priced, tmp_path, monkeypatch):
    defined = {SRC: {"files": {"ignore": ["/keep/**"]}},
               f"{UH}:{TEMP}": {"files": {"ignore": ["*.bak"]}}}      # blanks the dot-ignore list
    import app.core.policy as pol
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})
    monkeypatch.setattr(RL, "sources_index", lambda repo: {SRC: []})
    monkeypatch.setattr(RL, "build_map", lambda repo: {
        "version": RL._MAP_VERSION, "ts": 0.0, "names": [".kopiaignore"],
        "files": [{"source": SRC, "path": f"{TEMP}{SEP}.kopiaignore", "name": ".kopiaignore",
                   "size": 20, "content_id": "c1", "from": "index", "rules": ["*.tmp"],
                   "error": None}]})
    monkeypatch.setattr(policypricing, "DATA_DIR", str(tmp_path))

    item = policypricing.precompute_ignore_files("1")["files"][0]
    assert item["in_effect"] is False and item["files"] == 0 and item["rules"] == ["*.tmp"]


def test_ignore_files_for_filters_by_target_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(policypricing, "DATA_DIR", str(tmp_path))
    assert policypricing.ignore_files_for("1")["stored"] is False
    payload = {"version": policypricing._IGNORE_VERSION, "ts": 1.0, "budget_hit": False,
               "files": [{"path": f"{TEMP}{SEP}.kopiaignore", "name": ".kopiaignore",
                          "folder": TEMP, "source": SRC, "rules": ["*.tmp"], "in_effect": True,
                          "files": 3, "bytes": 900, "by_source": []},
                         {"path": f"C:{SEP}Other{SEP}.kopiaignore", "name": ".kopiaignore",
                          "folder": f"C:{SEP}Other", "source": f"{UH}:C:{SEP}Other",
                          "rules": ["*.log"], "in_effect": True, "files": 1, "bytes": 1,
                          "by_source": []}]}
    import app.index.sidecache as sidecache
    monkeypatch.setattr(policypricing.sidecache, "read",
                        lambda name, base=None: payload if "ignore" in name else None)
    under_src = policypricing.ignore_files_for("1", SRC)
    assert under_src["stored"] is True and under_src["count"] == 1
    assert under_src["files"][0]["folder"] == TEMP
    assert policypricing.ignore_files_for("1")["count"] == 2      # whole repo
    assert sidecache.read is not None
