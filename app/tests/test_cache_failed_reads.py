"""The failed-read rule in cache.get_children: a kopia read that fails or parses as garbage is
NEVER written to the walk cache - not to the sqlite rows, not to the in-process memo. A cached
"empty folder" from a failed read would hide that subtree from every later build (the KL5 class
of bug), so the read is skipped loudly instead and retried by the next build."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core import cache
from app.core.kopia import KopiaResult


def _res(rc: int, stdout: str = "", stderr: str = "") -> KopiaResult:
    return KopiaResult(args=["kopia"], returncode=rc, stdout=stdout, stderr=stderr, elapsed=0.0)


def _entries_json(names: list[str]) -> str:
    return json.dumps({"entries": [{"name": n, "type": "f", "obj": f"obj_{n}"} for n in names]})


@pytest.fixture
def db(monkeypatch):
    def _reset() -> None:
        cache.DIR_MEMO.clear()
        with cache._FAILED_LOCK:
            cache._FAILED_READS.clear()
            cache._FAILED_SAMPLE.clear()

    _reset()
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE dir_children (oid TEXT PRIMARY KEY, children TEXT)")
    con.execute("CREATE TABLE indirect (oid TEXT PRIMARY KEY, parts TEXT)")
    yield con
    con.close()
    _reset()


def test_failed_show_is_never_cached(db, monkeypatch):
    """A failed single-object kopia show must not cache an 'empty folder': the subtree stays
    missing for THIS build only (skip loudly), and a later successful read populates it."""
    oid = "Iindirect1"
    monkeypatch.setattr(cache, "kopia", lambda repo, args, **kw: _res(1, "", "boom"))
    out = cache.get_children("1", db, [oid])
    assert oid not in out
    assert db.execute("SELECT count(*) FROM dir_children WHERE oid=?", [oid]).fetchone()[0] == 0
    assert cache.DIR_MEMO.get(oid) is None
    stats = cache.read_failure_stats("1")
    assert stats and stats["count"] == 1 and stats["sample"] == [oid]
    # recovery: a good read populates and caches normally
    monkeypatch.setattr(cache, "kopia", lambda repo, args, **kw: _res(0, _entries_json(["a.txt"])))
    out = cache.get_children("1", db, [oid])
    assert [e["name"] for e in out[oid]] == ["a.txt"]
    assert db.execute("SELECT count(*) FROM dir_children WHERE oid=?", [oid]).fetchone()[0] == 1


def test_unparseable_output_is_never_cached(db, monkeypatch):
    oid = "Iindirect2"
    monkeypatch.setattr(cache, "kopia", lambda repo, args, **kw: _res(0, "garbage {{{"))
    out = cache.get_children("1", db, [oid])
    assert oid not in out
    assert db.execute("SELECT count(*) FROM dir_children WHERE oid=?", [oid]).fetchone()[0] == 0
    assert cache.DIR_MEMO.get(oid) is None
    assert cache.read_failure_stats("1")["count"] == 1


def test_failed_batch_keeps_parsed_prefix_skips_rest(db, monkeypatch):
    """`kopia content show` aborts at the first unreadable content: the printed prefix is still
    positionally paired (valid answers, cacheable), while the unanswered tail is neither treated
    as empty nor cached."""
    good, bad, tail = "kdir1", "kdir2", "kdir3"

    def fake(repo, args, **kw):
        assert args[:2] == ["content", "show"]
        return _res(3, _entries_json(["x.txt"]) + "\n", "unreadable content")

    monkeypatch.setattr(cache, "kopia", fake)
    out = cache.get_children("1", db, [good, bad, tail])
    assert [e["name"] for e in out[good]] == ["x.txt"]
    assert bad not in out and tail not in out
    assert db.execute("SELECT count(*) FROM dir_children").fetchone()[0] == 1
    stats = cache.read_failure_stats("1")
    assert stats["count"] == 2 and set(stats["sample"]) == {bad, tail}


def test_genuinely_empty_dir_is_still_cacheable(db, monkeypatch):
    """A successful read that lists nothing is a real answer and stays cached - the rule is about
    failed reads, not empty ones."""
    oid = "kempty"
    monkeypatch.setattr(cache, "kopia", lambda repo, args, **kw: _res(0, json.dumps({"entries": []})))
    out = cache.get_children("1", db, [oid])
    assert out[oid] == []
    assert db.execute("SELECT count(*) FROM dir_children WHERE oid=?", [oid]).fetchone()[0] == 1
    assert cache.read_failure_stats("1") is None
