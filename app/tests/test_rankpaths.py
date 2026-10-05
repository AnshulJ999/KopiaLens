"""The nightly ranking: exact top lists, and the Search sort that reads them.

Fixture: a tiny generation with a plainly-priced file, a two-version file whose versions share a
chunk (Stored must count it once), and a duplicate pair (Frees counts every copy).
"""

from __future__ import annotations

import duckdb
import pytest

from app.index import queries, rankpaths

SEP = chr(92)
SRC = f"Anshul@anshul-desktop:C:{SEP}Src"
P1 = f"C:{SEP}Src{SEP}a.bin"      # plain, 100 packed
P2 = f"C:{SEP}Src{SEP}b.bin"      # split, two versions sharing chunk c2 -> stored 10
P3 = f"C:{SEP}Src{SEP}dup.bin"    # same content as P4 -> frees counts both
P4 = f"C:{SEP}Src{SEP}dup2.bin"


def _build(tmp_path, monkeypatch):
    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id SMALLINT, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, ?)", [SRC])
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    for i, name in enumerate(("a", "Ivid1", "Ivid2", "p1", "p2", "p3", "dup"), start=1):
        con.execute("INSERT INTO cid VALUES (?, ?)", [i, name])
    # csize: every content's packed size. iphys: a split object's total (the sum of its pieces).
    con.execute("CREATE TABLE csize AS SELECT * FROM (VALUES (1::INTEGER, 100::UBIGINT), "
                "(4::INTEGER, 5::UBIGINT), (5::INTEGER, 5::UBIGINT), (6::INTEGER, 5::UBIGINT), "
                "(7::INTEGER, 40::UBIGINT)) t(cid_id, packed)")
    con.execute("CREATE TABLE iphys AS SELECT * FROM (VALUES (2::INTEGER, 10::UBIGINT), "
                "(3::INTEGER, 10::UBIGINT)) t(cid_id, packed)")
    con.execute("CREATE TABLE crefs AS SELECT * FROM (VALUES (1::INTEGER, 1::INTEGER), "
                "(4::INTEGER, 1::INTEGER), (5::INTEGER, 1::INTEGER), (6::INTEGER, 1::INTEGER), "
                "(7::INTEGER, 1::INTEGER)) t(cid_id, refs)")
    # crefo: piece 4 is used by both of b.bin's split objects (two (source, object) pairs)
    con.execute("CREATE TABLE crefo AS SELECT * FROM (VALUES (4::INTEGER, 2::INTEGER), "
                "(5::INTEGER, 1::INTEGER), (6::INTEGER, 1::INTEGER)) t(cid_id, refs)")
    # b.bin's two versions: split objects 2 and 3, sharing piece 4 -> stored 15, bound 20
    con.execute("CREATE TABLE tpart AS SELECT * FROM (VALUES (2::INTEGER, 4::INTEGER), "
                "(2::INTEGER, 5::INTEGER), (3::INTEGER, 4::INTEGER), (3::INTEGER, 6::INTEGER)) "
                "t(iobj_cid, part_cid)")
    con.execute("CREATE TABLE filev(src_id SMALLINT, path_id INTEGER, snap_id INTEGER, "
                "cid_id INTEGER, size UBIGINT)")
    for r in [(1, 1, 1, 1, 100),        # a.bin: one plain version
              (1, 2, 1, 2, 5),          # b.bin: version 1
              (1, 2, 2, 3, 5),          # b.bin: version 2 (shares a piece with version 1)
              (1, 3, 1, 7, 40),         # dup.bin
              (1, 4, 1, 7, 40)]:        # dup2.bin - same content
        con.execute("INSERT INTO filev VALUES (?, ?, ?, ?, ?)", list(r))
    con.execute("CREATE TABLE pathx(path_id INTEGER, src_id SMALLINT, dir_id INTEGER, "
                "name VARCHAR, ext VARCHAR, size UBIGINT, versions INTEGER, "
                "first_seen TIMESTAMP, last_seen TIMESTAMP, in_latest BOOLEAN, cid_id INTEGER, "
                "physical UBIGINT, exclusive UBIGINT, full_path VARCHAR)")
    for pid, (fp, size, cid) in enumerate([(P1, 100, 1), (P2, 5, 3), (P3, 40, 7), (P4, 40, 7)],
                                          start=1):
        con.execute("INSERT INTO pathx VALUES (?, 1, NULL, ?, '', ?, 1, TIMESTAMP '2025-01-01', "
                    "TIMESTAMP '2025-01-01', true, ?, 0, NULL, ?)",
                    [pid, fp.split(SEP)[-1], size, cid, fp])
    class _Gen:
        pass

    class _Cursor:
        def __init__(self, con):
            self._con = con
            self.generation = _Gen()
            self._res = None

        def execute(self, *a, **kw):
            self._res = self._con.execute(*a, **kw)
            return self._res

        def register(self, name, table):
            return self._con.register(name, table)

        def unregister(self, name):
            return self._con.unregister(name)

        @property
        def description(self):
            return self._res.description

        def fetchall(self):
            return self._res.fetchall()

        def close(self):
            pass

    def _factory(repo):
        return _Cursor(con)

    monkeypatch.setattr(rankpaths, "read_cursor", _factory)
    monkeypatch.setattr(rankpaths, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(rankpaths, "_MEMO", {})
    import app.index.store as store_mod
    monkeypatch.setattr(store_mod, "read_manifest", lambda repo: {"generation": 7})
    return con


def test_precompute_ranks_the_exact_all_versions_numbers(tmp_path, monkeypatch):
    con = _build(tmp_path, monkeypatch)
    payload = rankpaths.precompute("1", n=10)

    assert payload["exact"] is True and payload["generation"] == 7
    stored = {int(k): int(v) for k, v in payload["stored"]}
    assert stored[1] == 100                  # a.bin: one plain object
    assert stored[2] == 15                   # b.bin: {p1,p2} + {p1,p3} = three 5-byte chunks
    assert stored[3] == 40 and stored[4] == 40
    frees = {int(k): int(v) for k, v in payload["frees"]}
    assert frees[1] == 100                   # a's content, held by one path
    assert frees[3] == 15                    # b's latest content: both versions of the one holder
    assert frees[7] == 40                    # the duplicated content


def test_ranked_values_are_not_served_from_an_inexact_payload(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch)
    import app.index.sidecache as sidecache
    monkeypatch.setattr(sidecache, "read", lambda name, base=None: {
        "version": rankpaths._VERSION, "ts": 1.0, "n": 2, "exact": False,
        "generation": 7, "stored": [[1, 5]], "frees": [[6, 5]]})
    assert rankpaths.ranked_values("1", "stored") == {}


def test_search_sorts_by_the_ranked_list_and_marks_unranked_rows(tmp_path, monkeypatch):
    con = _build(tmp_path, monkeypatch)
    rankpaths.precompute("1", n=10)
    monkeypatch.setattr(queries, "read_cursor", rankpaths.read_cursor)

    out = queries.search_paths("1", sort="stored", direction="desc", limit=4)
    assert out["ranked"] == {"available": True, "kind": "stored", "count": 4}
    values = [r["stored_all_versions"] for r in out["rows"]]
    assert values == sorted(values, reverse=True)
    assert values[0] == 100 and values[-1] == 15

    free = queries.search_paths("1", sort="frees", direction="desc", limit=4)
    assert [r["frees_all_versions"] for r in free["rows"]][0] == 100


def test_search_falls_back_to_physical_when_nothing_is_ranked(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch)
    monkeypatch.setattr(queries, "read_cursor", rankpaths.read_cursor)
    monkeypatch.setattr(rankpaths, "stored", lambda repo: None)
    monkeypatch.setattr(rankpaths, "_MEMO", {})

    out = queries.search_paths("1", sort="stored", direction="desc", limit=2)
    assert out["ranked"]["available"] is False
    assert all(r.get("stored_all_versions") is None for r in out["rows"])


def test_ranked_values_are_dropped_when_the_id_chain_restarted(tmp_path, monkeypatch):
    """A from-scratch rebuild (data/repoN deleted) restarts the generation and the id counters,
    so a payload from the old chain is ignored; a later publish in the same chain keeps it."""
    _build(tmp_path, monkeypatch)                      # manifest generation 7
    import app.index.sidecache as sidecache
    import app.index.store as store_mod
    monkeypatch.setattr(sidecache, "read", lambda name, base=None: {
        "version": rankpaths._VERSION, "ts": 1.0, "n": 2, "exact": True,
        "generation": 43, "stored": [[1, 5]], "frees": [[6, 5]]})
    monkeypatch.setattr(rankpaths, "_MEMO", {})
    assert rankpaths.ranked_values("1", "stored") == {}          # gen 7 < 43: rebuilt chain

    monkeypatch.setattr(store_mod, "read_manifest", lambda repo: {"generation": 50})
    monkeypatch.setattr(rankpaths, "_MEMO", {})
    assert rankpaths.ranked_values("1", "stored") == {1: 5}      # same chain, ids still valid
