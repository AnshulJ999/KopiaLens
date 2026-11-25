"""`entries.children_of` - the tree read with the kopia fallback.

The tree is the store for every folder the last sync saw. This pins the three rules that matter:
entries come from the tree, a folder the tree does not hold comes from kopia, and a read that
FAILED is never returned as an empty folder (that rule is why a transient failure never became
permanent state).
"""

from __future__ import annotations

import json

import duckdb
import pytest

from app.index import entries, tree

SEP = chr(92)
ROOT = "kroot"
SUB = "ksub"
NS = 1735689600 * 10**9        # 2025-01-01T00:00:00Z in nanoseconds


class _Cur:
    """A stand-in for a generation cursor: `resolve` only needs execute/fetchall, and `children_of`
    must not close a cursor it did not open."""

    generation = None

    def __init__(self, con):
        self.con = con

    def execute(self, sql, params=None):
        return self.con.execute(sql, params) if params is not None else self.con.execute(sql)

    def close(self):
        pass


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    tree.create_tables(c)
    c.execute("INSERT INTO cid VALUES (1, ?), (2, 'c1'), (3, ?), (4, 'c2')", [ROOT, SUB])
    c.execute("INSERT INTO tdir VALUES (10, 1, 0, 2, false), (11, 3, 0, 1, false)")
    c.execute("INSERT INTO tent (dir_id, pos, name, etype, mode, size, mtime, cid_id, "
              "s_size, s_files) VALUES "
              "(10, 0, 'a.txt', 0, 0, 100, ?, 2, NULL, NULL), "
              "(10, 1, 'sub',   1, 1, NULL, NULL, 3, 5000, 7), "
              "(11, 0, 'b.txt', 0, 0, 200, ?, 4, NULL, NULL)", [NS, NS])
    yield c
    c.close()


@pytest.fixture(autouse=True)
def clean_lru():
    entries._LRU.clear()
    entries._LRU_BYTES.clear()
    yield
    entries._LRU.clear()
    entries._LRU_BYTES.clear()


@pytest.fixture
def patched(con, monkeypatch):
    monkeypatch.setattr(entries, "read_cursor", lambda repo: _Cur(con))
    return con


def test_entries_come_from_the_tree(patched):
    got = entries.children_of("1", [ROOT])
    assert [e["name"] for e in got[ROOT]] == ["a.txt", "sub"]
    f, d = got[ROOT]
    assert f["type"] == "f" and f["obj"] == "c1" and f["size"] == 100
    assert f["mtime"].startswith("2025-01-01T00:00:00")
    assert d["type"] == "d" and d["obj"] == SUB
    assert d["summ"]["size"] == 5000 and d["summ"]["files"] == 7


def test_a_folder_the_tree_lacks_comes_from_kopia(patched, monkeypatch):
    calls = []

    def fake_kopia(repo, args, **kw):
        calls.append(args)
        if args[:2] == ["content", "show"]:
            return type("R", (), {"stdout": json.dumps(
                {"entries": [{"name": "new.txt", "type": "f", "obj": "c9", "size": 5}]}),
                "stderr": "", "returncode": 0})()
        return type("R", (), {"stdout": "", "stderr": "nope", "returncode": 1})()

    monkeypatch.setattr(entries, "kopia", fake_kopia)
    got = entries.children_of("1", ["kbrandnew"])
    assert [e["name"] for e in got["kbrandnew"]] == ["new.txt"]
    assert calls and calls[0][:2] == ["content", "show"]
    # and it is held, so the next click in the same area does not call kopia again
    calls.clear()
    again = entries.children_of("1", ["kbrandnew"])
    assert [e["name"] for e in again["kbrandnew"]] == ["new.txt"]
    assert calls == []


def test_a_failed_read_is_never_an_empty_folder(patched, monkeypatch):
    monkeypatch.setattr(entries, "kopia", lambda repo, args, **kw: type(
        "R", (), {"stdout": "", "stderr": "content not found", "returncode": 1})())
    got = entries.children_of("1", ["kbroken"])
    assert got == {}                       # absent, NOT an empty list
    assert "kbroken" not in entries._LRU.get("1", {})
