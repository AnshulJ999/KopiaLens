"""batching the reclaim scan.

``reclaim_for`` used to scan the whole history once per content id (up to 500 scans for one Reclaim
click). The batched version issues a fixed handful of scans regardless of id count: batched id
resolution, "which paths hold these ids", batched path resolution, then "which objects each of
those paths has held" - byte-identical answers, ~50x fewer scans on repo2-shaped data.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index import physical

SEP = chr(92)


class _CountingCursor:
    """Counts queries so the batching pin survives the rewrite: a per-id loop regression
    would multiply the scans while the answers stay correct (a value-only gate can't see it)."""

    def __init__(self, con):
        self._con = con
        self.count = 0

    def execute(self, sql, params=None):
        self.count += 1
        return self._con.execute(sql, params or [])

    def close(self):
        pass  # the counter outlives the call; the fixture owns the connection

    def __getattr__(self, name):
        return getattr(self._con, name)


@pytest.fixture
def batched(monkeypatch):
    """Two contents sharing across paths, chunk expansion, and a refcount that prices freeable.

    lives: A (duplicated at f1+f2, f1 also held A2) and B (single path f3); C holds nothing.
    """
    physical._RECLAIM_CACHE.clear()
    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, 's1')")
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    con.execute("INSERT INTO cid VALUES (1,'A'),(2,'A2'),(3,'B')")  # no C: it holds nothing
    con.execute("CREATE TABLE pathx(path_id INTEGER, src_id INTEGER, full_path VARCHAR)")
    con.execute("INSERT INTO pathx VALUES (1, 1, ?), (2, 1, ?), (3, 1, ?)",
                ["C:" + SEP + "f1", "C:" + SEP + "f2", "C:" + SEP + "f3"])
    con.execute("CREATE TABLE filev(path_id INTEGER, cid_id INTEGER)")
    con.execute("INSERT INTO filev VALUES (1, 1), (1, 2), (2, 1), (3, 3)")
    chunks = {"A": {"a1", "a2"}, "A2": {"a2", "a3"}, "B": {"b1"}}
    sizes = {"a1": 10, "a2": 20, "a3": 30, "b1": 5}
    refs = {"a1": 1, "a2": 2, "a3": 1, "b1": 1}

    counter = _CountingCursor(con)
    monkeypatch.setattr(physical, "read_cursor", lambda repo: counter)
    monkeypatch.setattr(physical, "cache_db", lambda repo: None)
    monkeypatch.setattr(physical, "load_sizemap", lambda repo: sizes)
    monkeypatch.setattr(physical, "_chunks_for_obj", lambda repo, db, obj, fetch=True: set(chunks[obj]))
    # Nothing outside the selection references these chunks beyond what this_refs already counts.
    monkeypatch.setattr(physical, "_refcount_for", lambda repo, cids: dict(refs))
    return counter


def test_reclaim_batched_matches_per_id(batched):
    # A: f1(A)=+a1,a2  f1(A2)=+a2,a3  f2(A)=+a1,a2  -> a1:2 a2:3 a3:1 -> free a1(1<=2)+a2(2<=3)+a3(1<=1)
    # B: f3(B)=+b1 -> b1:1 -> free b1(1<=1)
    # C: nothing -> 0
    out = physical.reclaim_for("1", ["A", "B", "C"], cap=10)
    assert out == {"A": 60, "B": 5, "C": 0}
    assert batched.count <= 8, f"reclaim_for issued {batched.count} queries - batching regressed"


def test_reclaim_batched_caches_each_result(batched):
    out = physical.reclaim_for("1", ["A", "B", "C"], cap=10)
    assert physical._reclaim_cache_get(("1", "A")) == 60
    assert physical._reclaim_cache_get(("1", "C")) == 0
    # Second call is served from the cache without touching the cursor.
    again = physical.reclaim_for("1", ["A", "B", "C"], cap=10)
    assert again == out
    assert batched.count <= 8, "second call hit the cursor - the cache did not cover it"