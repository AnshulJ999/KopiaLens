"""Policy pricing against the index - synthetic artifacts, the real SQL + matcher paths.

Pins the "keeping out" numbers end to end on a fabricated generation: a pattern's file/byte
counts come from the source's CURRENT (in_latest) files, a matched directory prunes its whole
subtree, matching is case-sensitive, and negated rules go through kopia's ignorefs state machine
in rule order (the slow path).
"""

from __future__ import annotations

import os

import duckdb
import pytest

from app.index import policypricing, sidecache, store

SEP = chr(92)
SRC = f"Anshul@anshul-desktop:C:{SEP}Src"

# in_latest files under C:\Src (+ one in_latest=False row that pricing must ignore)
_FILES = [
    ("keep.txt", 100, True, f"C:{SEP}Src{SEP}keep.txt"),
    ("a.tmp", 200, True, f"C:{SEP}Src{SEP}Temp{SEP}a.tmp"),
    ("b.tmp", 300, True, f"C:{SEP}Src{SEP}Temp{SEP}b.tmp"),
    ("c.tmp", 400, True, f"C:{SEP}Src{SEP}Temp{SEP}nested{SEP}c.tmp"),
    ("keep.tmp", 50, True, f"C:{SEP}Src{SEP}Temp{SEP}keep.tmp"),
    ("x.js", 500, True, f"C:{SEP}Src{SEP}other{SEP}node_modules{SEP}x.js"),
    ("old.tmp", 999, False, f"C:{SEP}Src{SEP}old.tmp"),
]
_DIRS = [
    (f"C:", 1), (f"C:{SEP}Src", 2), (f"C:{SEP}Src{SEP}Temp", 3),
    (f"C:{SEP}Src{SEP}Temp{SEP}nested", 4), (f"C:{SEP}Src{SEP}other", 3),
    (f"C:{SEP}Src{SEP}other{SEP}node_modules", 4),
]


def _build(tmp_path: str):
    con = duckdb.connect()
    con.execute("CREATE TABLE src AS SELECT * FROM (VALUES "
                f"(1::SMALLINT, '{SRC}')) t(src_id, source)")
    con.execute("CREATE TABLE cid AS SELECT 1::INTEGER AS cid_id, 'o' AS content_id WHERE false")
    con.execute("CREATE TABLE snap AS SELECT 1::INTEGER AS snap_id, 's' AS snapshot_id, "
                "TIMESTAMP '2025-01-01' AS start_time, 1::SMALLINT AS src_id WHERE false")
    rows = ", ".join(
        f"({i + 1}::INTEGER, 1::BIGINT, {i + 1}::INTEGER, '{name.rsplit('.', 1)[0]}', "
        f"'{name.rsplit('.', 1)[1]}', {size}::UBIGINT, 1::INTEGER, "
        f"TIMESTAMP '2025-01-01', TIMESTAMP '2025-01-01', {str(in_latest).lower()}, "
        f"NULL::INTEGER, 0::UBIGINT, NULL::UBIGINT, '{fp}')"
        for i, (name, size, in_latest, fp) in enumerate(_FILES))
    con.execute(f"CREATE TABLE pathx AS SELECT * FROM (VALUES {rows}) t("
                "path_id, src_id, dir_id, name, ext, size, versions, first_seen, last_seen, "
                "in_latest, cid_id, physical, exclusive, full_path)")
    drows = ", ".join(
        f"({i + 1}::INTEGER, 1::SMALLINT, '{p}', {d}::SMALLINT, "
        f"'{p.split(SEP)[-1]}', NULL)" for i, (p, d) in enumerate(_DIRS))
    con.execute(f"CREATE TABLE dir AS SELECT * FROM (VALUES {drows}) t("
                "dir_id, src_id, dir_path, depth, name, parent_path)")
    con.execute("CREATE TABLE filev AS SELECT 1::BIGINT AS src_id, 1::INTEGER AS path_id, "
                "1::INTEGER AS snap_id, 1::INTEGER AS cid_id, 1::UBIGINT AS size, "
                "TIMESTAMP '2025-01-01' AS mtime, 0::BIGINT AS ordinal WHERE false")
    con.execute("CREATE TABLE anc AS SELECT 1::INTEGER AS dir_id, 1::INTEGER AS ancestor_dir_id "
                "WHERE false")
    con.execute("CREATE TABLE csize AS SELECT 'o' AS content_id, 1::UBIGINT AS packed WHERE false")
    con.execute("CREATE TABLE crefs AS SELECT 'o' AS content_id, 1::INTEGER AS refs WHERE false")
    con.execute("CREATE TABLE iphys AS SELECT 'o' AS oid, 1::UBIGINT AS packed WHERE false")
    con.execute("CREATE TABLE sanalysis(repo VARCHAR, source VARCHAR, snapshot_id VARCHAR, "
                "churn UBIGINT, physical UBIGINT, reclaimable UBIGINT, snaphash VARCHAR, "
                "computed_at TIMESTAMP)")
    con.execute("CREATE TABLE isnaps(repo VARCHAR, snapshot_id VARCHAR, source VARCHAR, "
                "start_time TIMESTAMP, root_obj VARCHAR, indexed_at TIMESTAMP, file_count BIGINT)")
    d = str(tmp_path)
    artifacts = {}
    for name in ("src", "cid", "snap", "pathx", "dir", "filev", "anc", "csize", "crefs",
                 "iphys", "sanalysis", "isnaps"):
        p = os.path.join(d, name + ".parquet")
        con.execute(f"COPY {name} TO '{p.replace(SEP, chr(47))}' (FORMAT parquet)")
        artifacts[name] = [p]
    con.close()
    return artifacts


@pytest.fixture
def priced(tmp_path, monkeypatch):
    """Bind the synthetic generation and route policypricing's reads at it."""
    artifacts = _build(str(tmp_path))

    class _FakeGen:
        pass

    class _Cursor:
        """DuckDB connections reject attribute assignment; resolve.py wants a .generation."""

        def __init__(self, con):
            self._con = con
            self.generation = _FakeGen()

        def execute(self, *a, **kw):
            return self._con.execute(*a, **kw)

        def close(self):
            pass  # a no-op: the real read_cursor hands out per-call cursors, this shares one con

    con = duckdb.connect()
    store._bind_generation(con, "1", artifacts)

    monkeypatch.setattr(policypricing, "read_cursor", lambda repo: _Cursor(con))
    monkeypatch.setattr(policypricing, "_CACHE", {})
    return con


def _by_pattern(out: dict) -> dict:
    return {r["pattern"]: r for r in out["results"]}


def test_suffix_pattern_counts_current_files_only(priced):
    out = policypricing.price_patterns("1", SRC, ["*.tmp"], limit=10, use_cache=False)
    r = _by_pattern(out)["*.tmp"]
    assert not out["slow_path"]
    assert r["error"] is None
    assert r["files"] == 4          # a, b, c, keep - NOT the in_latest=False old.tmp
    assert r["bytes"] == 200 + 300 + 400 + 50
    assert r["dirs"] == 0
    assert r["total_examples"] == 4
    biggest = r["examples"][0]
    assert biggest["path"] == f"C:{SEP}Src{SEP}Temp{SEP}nested{SEP}c.tmp" and biggest["size"] == 400


def test_anchored_doublestar_prunes_the_subtree(priced):
    out = policypricing.price_patterns("1", SRC, ["/Temp/**"], limit=10, use_cache=False)
    r = _by_pattern(out)["/Temp/**"]
    assert r["files"] == 4
    assert r["bytes"] == 950
    assert r["dirs"] == 1           # /Temp/nested matches; /Temp itself does not (a/** != a)


def test_bare_name_matches_the_directory_anywhere(priced):
    out = policypricing.price_patterns("1", SRC, ["node_modules"], limit=10, use_cache=False)
    r = _by_pattern(out)["node_modules"]
    assert r["files"] == 1
    assert r["bytes"] == 500
    assert r["dirs"] == 1


def test_matching_is_case_sensitive_like_kopia(priced):
    out = policypricing.price_patterns("1", SRC, ["*.TMP", "*.tmp"], limit=0, use_cache=False)
    r = _by_pattern(out)
    assert r["*.TMP"]["files"] == 0
    assert r["*.tmp"]["files"] == 4


def test_dir_only_pattern_keeps_out_through_the_ancestor(priced):
    out = policypricing.price_patterns("1", SRC, ["other/"], limit=10, use_cache=False)
    r = _by_pattern(out)["other/"]
    assert r["files"] == 1
    assert r["bytes"] == 500
    assert r["dirs"] == 1


def test_examples_cap_is_honest(priced):
    out = policypricing.price_patterns("1", SRC, ["*.tmp"], limit=2, use_cache=False)
    r = _by_pattern(out)["*.tmp"]
    assert len(r["examples"]) == 2
    assert r["total_examples"] == 4
    assert r["capped"] is True


def test_negated_rules_take_the_slow_path_and_reinclude(priced):
    """The machine in rule order: *.tmp ignores the file, then !/Temp/keep.tmp re-includes it -
    so in sequence context the *.tmp rule honestly keeps out only the files it still excludes
    (keep.tmp was re-included by the later negated rule)."""
    out = policypricing.price_patterns(
        "1", SRC, ["*.tmp", "!/Temp/keep.tmp"], limit=10, use_cache=False)
    assert out["slow_path"] is True
    r = _by_pattern(out)
    assert r["*.tmp"]["files"] == 3
    assert r["*.tmp"]["bytes"] == 900
    assert r["!/Temp/keep.tmp"]["files"] == 0   # it re-includes; it keeps nothing out
    assert r["!/Temp/keep.tmp"]["negated"] is True


def test_unknown_source_prices_at_zero(priced):
    out = policypricing.price_patterns("1", f"nobody@elsewhere:X:{SEP}Nope", ["*.tmp"],
                                       limit=5, use_cache=False)
    assert out["src_found"] is True
    assert _by_pattern(out)["*.tmp"]["files"] == 0


def test_invalid_pattern_reports_an_error(priced):
    out = policypricing.price_patterns("1", SRC, ["bad\\"], limit=5, use_cache=False)
    r = _by_pattern(out)["bad\\"]
    assert r["error"]
    assert r["files"] == 0


def test_cache_serves_repeat_calls(priced, monkeypatch):
    calls = {"n": 0}
    real = policypricing._compute

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(policypricing, "_compute", counting)
    first = policypricing.price_patterns("1", SRC, ["*.tmp"], limit=5)
    second = policypricing.price_patterns("1", SRC, ["*.tmp"], limit=5)
    assert calls["n"] == 1
    assert first["results"][0]["files"] == second["results"][0]["files"] == 4


def test_invalidate_clears_the_cache(priced, monkeypatch):
    calls = {"n": 0}
    real = policypricing._compute

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(policypricing, "_compute", counting)
    policypricing.price_patterns("1", SRC, ["*.tmp"], limit=5)
    policypricing.invalidate("1")
    policypricing.price_patterns("1", SRC, ["*.tmp"], limit=5)
    assert calls["n"] == 2


# ---- FILTER-batched fast path + the nightly stored stage ------------------------

def test_filter_batch_matches_known_counts_across_a_batch_boundary(priced):
    """One scan per _BATCH patterns: 45 patterns forces two batches, and the known-count
    patterns must stay exact across the boundary."""
    known = {"*.tmp": (4, 950), "/Temp/**": (4, 950), "node_modules": (1, 500),
             "*.TMP": (0, 0), "other/": (1, 500)}
    filler = [f"zzz_nonexistent_{i}.bin" for i in range(39)]
    pats = list(known) + filler           # 44 patterns -> crosses the 40-pattern batch edge
    out = policypricing.price_patterns("1", SRC, pats, limit=0, use_cache=False)
    by = {r["pattern"]: r for r in out["results"]}
    for pat, (files, byts) in known.items():
        assert by[pat]["files"] == files and by[pat]["bytes"] == byts, pat
    for pat in filler:
        assert by[pat]["files"] == 0, pat
    assert out["slow_path"] is False


def test_precompute_stores_per_source_and_global_rollup(priced, tmp_path, monkeypatch):
    import app.core.policy as pol

    glob_rules = ["gtmp1", "gtmp2"]
    defined = {"(global)": {"files": {"ignore": list(glob_rules)}},
               SRC: {"files": {"ignore": ["*.tmp"]}}}
    effective = {SRC: {"files": {"ignore": ["*.tmp"]}},
                 f"x@y:D:{SEP}": {"files": {"ignore": glob_rules}}}
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})
    monkeypatch.setattr(pol, "load_policies", lambda repo: {"policies": effective})
    monkeypatch.setattr(pol, "refresh_defined_policies",
                        lambda repo, force=False: ({"policies": defined}, True))
    monkeypatch.setattr(policypricing, "DATA_DIR", str(tmp_path))

    payload = policypricing.precompute("1")
    assert policypricing.stored("1") is not None
    assert payload["rules"][SRC]["*.tmp"]["files"] == 4

    mine = policypricing.stored_for_target("1", SRC)
    assert mine["stored"] is True and mine["results"][0]["pattern"] == "*.tmp"
    assert mine["results"][0]["files"] == 4

    # D:\ has no index rows -> the global rollup sums to zeros but the shape is right
    roll = policypricing.stored_for_target("1", "global")
    assert {r["pattern"] for r in roll["results"]} == set(glob_rules)
    assert all(r["files"] == 0 for r in roll["results"])


def test_stored_for_target_returns_none_before_the_first_nightly(priced, tmp_path, monkeypatch):
    monkeypatch.setattr(policypricing, "DATA_DIR", str(tmp_path))
    assert policypricing.stored("1") is None
    assert policypricing.stored_for_target("1", "global") is None


# ---- precompute skip-when-unchanged (fingerprints) ------------------------------

SRC2 = f"x@y:D:{SEP}"


def _skip_env(priced, tmp_path, monkeypatch, glob_rules, snapsets):
    """The stored-stage environment: two policies (SRC own rules, SRC2 global-inheriting),
    redirect the store at tmp_path, pin the snapshot-set fingerprints, reset the TTL cache."""
    import app.core.policy as pol

    defined = {"(global)": {"files": {"ignore": list(glob_rules)}},
               SRC: {"files": {"ignore": ["*.tmp"]}}}
    effective = {SRC: {"files": {"ignore": ["*.tmp"]}},
                 SRC2: {"files": {"ignore": glob_rules}}}
    monkeypatch.setattr(pol, "load_defined_policies", lambda repo: {"policies": defined})
    monkeypatch.setattr(pol, "load_policies", lambda repo: {"policies": effective})
    monkeypatch.setattr(pol, "refresh_defined_policies",
                        lambda repo, force=False: ({"policies": defined}, True))
    monkeypatch.setattr(policypricing, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(policypricing, "_snapset_hashes", lambda repo: dict(snapsets))
    monkeypatch.setattr(policypricing, "_CACHE", {})
    return defined, effective


def _count_fresh_prices(monkeypatch) -> dict:
    calls = {"n": 0}
    real = policypricing._compute

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(policypricing, "_compute", counting)
    return calls


def test_precompute_skips_everything_unchanged_on_the_second_run(priced, tmp_path, monkeypatch):
    snaps = {SRC: "aa", SRC2: "bb"}
    _skip_env(priced, tmp_path, monkeypatch, ["gtmp1", "gtmp2"], snaps)
    calls = _count_fresh_prices(monkeypatch)

    first = policypricing.precompute("1")
    assert calls["n"] == 2                      # own rules + one global-scope contribution

    monkeypatch.setattr(policypricing, "_CACHE", {})   # the TTL cache must not mask the skip
    second = policypricing.precompute("1")
    assert calls["n"] == 2                      # nothing repriced
    assert second["rules"] == first["rules"]    # and the numbers are byte-identical


def test_precompute_reprices_only_the_changed_source(priced, tmp_path, monkeypatch):
    snaps = {SRC: "aa", SRC2: "bb"}
    _skip_env(priced, tmp_path, monkeypatch, ["gtmp1", "gtmp2"], snaps)
    calls = _count_fresh_prices(monkeypatch)
    first = policypricing.precompute("1")

    monkeypatch.setattr(policypricing, "_CACHE", {})
    monkeypatch.setattr(policypricing, "_snapset_hashes",
                        lambda repo: {SRC: "CHANGED", SRC2: "bb"})
    second = policypricing.precompute("1")
    assert calls["n"] == 3                      # only SRC's own rules repriced
    assert second["rules"][SRC] == first["rules"][SRC]
    assert second["rules"]["global"] == first["rules"]["global"]   # its contribution was reused
    assert second["fp"]["src"][SRC]["snaps"] == "CHANGED"


def test_precompute_global_rule_change_reprices_contributions_but_keeps_own(priced, tmp_path,
                                                                           monkeypatch):
    snaps = {SRC: "aa", SRC2: "bb"}
    defined, effective = _skip_env(priced, tmp_path, monkeypatch, ["gtmp1", "gtmp2"], snaps)
    calls = _count_fresh_prices(monkeypatch)
    first = policypricing.precompute("1")

    defined["(global)"]["files"]["ignore"] = ["gtmp9"]
    effective[SRC2]["files"]["ignore"] = ["gtmp9"]
    monkeypatch.setattr(policypricing, "_CACHE", {})
    second = policypricing.precompute("1")
    assert calls["n"] == 3                      # only the global contribution repriced
    assert second["rules"][SRC] == first["rules"][SRC]             # own rules still reused
    assert set(second["rules"]["global"]) == {"gtmp9"}


def test_precompute_without_a_fingerprint_block_prices_fresh(priced, tmp_path, monkeypatch):
    """A stored file from BEFORE the fingerprint era (current version, no fp block) is still
    readable; everything prices once and the new file carries the fp block for the next night."""
    import json
    snaps = {SRC: "aa", SRC2: "bb"}
    _skip_env(priced, tmp_path, monkeypatch, ["gtmp1", "gtmp2"], snaps)
    calls = _count_fresh_prices(monkeypatch)
    sidecache.write(policypricing.stored_name("1"),
                    {"version": policypricing._STORE_VERSION, "ts": 0.0, "rules": {}},
                    base=policypricing.DATA_DIR)

    payload = policypricing.precompute("1")
    assert calls["n"] == 2
    assert payload["fp"]["src"][SRC]["own"] == policypricing._rules_hash(["*.tmp"])


def test_stored_file_from_an_older_code_version_reads_as_absent(priced, tmp_path, monkeypatch):
    """The version bump is the guard against fingerprint-reusing prices written by older pricing
    code (KL70/KL71 changed the semantics): a v1 file must be ignored entirely, forcing the
    first nightly after the merge into one full reprice."""
    import json
    snaps = {SRC: "aa", SRC2: "bb"}
    _skip_env(priced, tmp_path, monkeypatch, ["gtmp1", "gtmp2"], snaps)
    calls = _count_fresh_prices(monkeypatch)
    sidecache.write(policypricing.stored_name("1"),
                    {"version": 1, "ts": 0.0, "rules": {"stale": True}, "fp": {"stale": True}},
                    base=policypricing.DATA_DIR)

    assert policypricing.stored("1") is None
    payload = policypricing.precompute("1")
    assert calls["n"] == 2
    assert "stale" not in payload["rules"]
    assert payload["fp"]["src"][SRC]["snaps"] == "aa"
