"""Search's CSV export carries the table's columns, and works under the Stored/Frees sort (it used to
fail with a binder error: the sort named a ranked column the export never joined)."""

from __future__ import annotations

import duckdb

from app.index import queries, rankpaths

SEP = chr(92)


def _store(monkeypatch, ranked: dict[str, dict[int, int]]):
    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, 'me@pc:H:" + SEP + "'), (2, 'me@pc:H:" + SEP + "Games')")
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    con.execute("INSERT INTO cid VALUES (1, 'A'), (2, 'B')")
    con.execute("""CREATE TABLE pathx(path_id INTEGER, src_id INTEGER, cid_id INTEGER, full_path VARCHAR,
                   name VARCHAR, ext VARCHAR, size BIGINT, physical BIGINT, versions INTEGER,
                   first_seen TIMESTAMP, last_seen TIMESTAMP, in_latest BOOLEAN, exclusive BIGINT)""")
    big = "H:" + SEP + "Games" + SEP + "big.iso"
    small = "H:" + SEP + "small.txt"
    con.execute("INSERT INTO pathx VALUES "
                "(1, 1, 1, ?, 'big.iso', 'iso', 900, 800, 3, '2026-01-01', '2026-02-01', true, 0),"
                "(2, 2, 1, ?, 'big.iso', 'iso', 900, 800, 3, '2026-01-01', '2026-02-01', true, 0),"
                "(3, 1, 2, ?, 'small.txt', 'txt', 10, 10, 1, '2026-01-01', '2026-02-02', false, 0)",
                [big, big, small])

    class _Cur:
        def __getattr__(self, name):
            return getattr(con, name)

    monkeypatch.setattr(queries, "read_cursor", lambda repo: _Cur())
    monkeypatch.setattr(rankpaths, "ranked_values", lambda repo, kind: ranked.get(kind, {}))
    return big, small


def _rows(**kw):
    cur, registered = queries.search_export("1", **kw)
    rows = cur.fetchall()
    for name in registered:
        cur.unregister(name)
    return [dict(zip(queries.EXPORT_HEADER, r)) for r in rows]


def test_the_stored_sort_exports_ranked_rows_first_with_their_values(monkeypatch):
    big, small = _store(monkeypatch, {"stored": {3: 50}, "frees": {3: 40}})   # both keyed by path
    rows = _rows(sort="stored", direction="desc")
    assert [r["Path"] for r in rows][0] == small          # the only ranked row leads
    assert (rows[0]["Stored (all versions)"], rows[0]["Frees (all versions)"]) == (50, 40)
    assert rows[1]["Stored (all versions)"] is None        # outside the ranked list: empty


def test_src_and_copies_count_sources_and_distinct_paths(monkeypatch):
    big, _small = _store(monkeypatch, {})
    rows = [r for r in _rows(sort="size") if r["Path"] == big]
    # one path held by two nested sources: two sources, one copy
    assert {(r["Src"], r["Copies"], r["On disk"]) for r in rows} == {(2, 1, "yes")}


def test_the_first_list_is_cached_per_sort(monkeypatch):
    calls = []
    monkeypatch.setattr(queries, "search_paths", lambda repo, **kw: calls.append(kw["sort"]) or {"sort": kw["sort"]})
    queries._LANDING.clear()
    assert queries.landing("1", sort="size")["sort"] == "size"
    assert queries.landing("1", sort="stored")["sort"] == "stored"   # never the size-sorted list
    assert queries.landing("1", sort="stored")["sort"] == "stored"
    assert calls == ["size", "stored"]


def test_deduped_rows_count_their_sources(monkeypatch):
    big, _small = _store(monkeypatch, {})
    rows = queries.search_paths("1", dedupe=True, sort="size")["rows"]
    assert {r["full_path"]: r["sources"] for r in rows}[big] == 2   # the table read 1 here
