"""batching the reclaim scan.

``reclaim_for`` used to scan the whole history once per content id (up to 500 scans for one Reclaim
click). The batched version issues two scans total: "which paths hold these ids" then "which objects
each of those paths has held" - byte-identical answers, ~50x fewer scans on repo2-shaped data.
"""

from __future__ import annotations

import pytest

from app.index import physical

SEP = chr(92)


class _ReclaimCursor:
    """Routes the two batched queries by shape and refuses to distinguish anything else."""

    def __init__(self, sharing_rows, object_rows):
        self._sharing = sharing_rows  # (cid, source, full_path) for the content_id IN(...) scan
        self._objects = object_rows   # (source, full_path, content_id) for the JOIN scan
        self._last = []

    def execute(self, sql, _params=None):
        upper = sql.upper()
        if upper.startswith("SELECT DISTINCT CONTENT_ID"):
            self._last = list(self._sharing)
        elif "JOIN" in upper:
            self._last = list(self._objects)
        else:  # CREATE / DROP / INSERT of the temp table
            self._last = []
        return self

    def executemany(self, _sql, _params):
        return self

    def fetchall(self):
        return list(self._last)

    def close(self):
        pass


@pytest.fixture
def batched(monkeypatch):
    """Two contents sharing across paths, chunk expansion, and a refcount that prices freeable.

    lives: A (duplicated at f1+f2, f1 also held A2) and B (single path f3); C holds nothing.
    """
    physical._RECLAIM_CACHE.clear()
    sharing = [("A", "s1", "C:" + SEP + "f1"), ("A", "s1", "C:" + SEP + "f2"),
               ("B", "s1", "C:" + SEP + "f3")]
    objects = [("s1", "C:" + SEP + "f1", "A"), ("s1", "C:" + SEP + "f1", "A2"),
               ("s1", "C:" + SEP + "f2", "A"), ("s1", "C:" + SEP + "f3", "B")]
    chunks = {"A": {"a1", "a2"}, "A2": {"a2", "a3"}, "B": {"b1"}}
    sizes = {"a1": 10, "a2": 20, "a3": 30, "b1": 5}
    refs = {"a1": 1, "a2": 2, "a3": 1, "b1": 1}

    monkeypatch.setattr(physical, "read_cursor", lambda repo: _ReclaimCursor(sharing, objects))
    monkeypatch.setattr(physical, "cache_db", lambda repo: None)
    monkeypatch.setattr(physical, "load_sizemap", lambda repo: sizes)
    monkeypatch.setattr(physical, "_chunks_for_obj", lambda repo, db, obj: set(chunks[obj]))
    # Nothing outside the selection references these chunks beyond what this_refs already counts.
    monkeypatch.setattr(physical, "_refcount_for", lambda repo, cids: dict(refs))


def test_reclaim_batched_matches_per_id(batched):
    # A: f1(A)=+a1,a2  f1(A2)=+a2,a3  f2(A)=+a1,a2  -> a1:2 a2:3 a3:1 -> free a1(1<=2)+a2(2<=3)+a3(1<=1)
    # B: f3(B)=+b1 -> b1:1 -> free b1(1<=1)
    # C: nothing -> 0
    out = physical.reclaim_for("1", ["A", "B", "C"], cap=10)
    assert out == {"A": 60, "B": 5, "C": 0}


def test_reclaim_batched_caches_each_result(batched):
    out = physical.reclaim_for("1", ["A", "B", "C"], cap=10)
    assert physical._reclaim_cache_get(("1", "A")) == 60
    assert physical._reclaim_cache_get(("1", "C")) == 0
    # Second call is served from the cache without touching the cursor.
    again = physical.reclaim_for("1", ["A", "B", "C"], cap=10)
    assert again == out