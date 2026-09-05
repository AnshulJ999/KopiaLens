"""Walker: a directory object that sits at more than one path, and the indirect
chunk-list cache.

Kopia stores identical directories ONCE. The walker's "already visited" set used to be keyed on
the directory object alone, so only the first location of a shared directory was ever indexed -
and because a renamed directory keeps its object, a rename lost the earlier path entirely.

The delta optimization these tests must NOT break: walking a source's snapshots newest-first with
one accumulating seen set emits each unchanged directory once in total, not once per snapshot.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core import cache, walk


@pytest.fixture(autouse=True)
def clean_memo():
    """DIR_MEMO is process-global and keyed on the object id alone, so a directory object seeded
    by one test would otherwise be served to the next one from RAM instead of the fixture's own
    database - these tests deliberately reuse names like "root" across scenarios."""
    cache.DIR_MEMO.clear()
    cache._MEMO_BYTES = 0
    yield
    cache.DIR_MEMO.clear()
    cache._MEMO_BYTES = 0


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE dir_children (oid TEXT PRIMARY KEY, children TEXT)")
    con.execute("CREATE TABLE indirect (oid TEXT PRIMARY KEY, parts TEXT)")
    yield con
    con.close()


def seed(db, tree: dict[str, list[dict]]) -> None:
    for oid, entries in tree.items():
        db.execute("INSERT OR REPLACE INTO dir_children VALUES (?,?)",
                   (oid, cache.encode_json(entries)))
    db.commit()


def d(name: str, obj: str) -> dict:
    return {"name": name, "obj": obj, "type": "d"}


def f(name: str, obj: str, size: int = 1) -> dict:
    return {"name": name, "obj": obj, "type": "f", "size": size}


def collect(repo, db, roots, seen, sep="\\"):
    got: list[str] = []

    def visitor(parent: str, entry: dict) -> None:
        got.append((parent + sep + entry["name"]).lstrip(sep))

    walk.walk_new_visit(repo, db, roots, seen, visitor, sep=sep)
    return got


# --------------------------------------------------------------------------------------------
# The bug itself
# --------------------------------------------------------------------------------------------

def test_a_directory_at_two_paths_emits_files_at_both(db):
    """Root holds A and B, both pointing at the SAME directory object."""
    seed(db, {
        "root": [d("A", "shared"), d("B", "shared")],
        "shared": [f("file.txt", "c1")],
    })
    assert sorted(collect("1", db, ["root"], set())) == ["A\\file.txt", "B\\file.txt"]


def test_a_shared_subtree_emits_its_whole_depth_at_both_paths(db):
    """The second location must descend fully, not just emit its top level - the children of a
    shared directory are themselves shared objects and would hit the same suppression."""
    seed(db, {
        "root": [d("A", "shared"), d("B", "shared")],
        "shared": [d("deep", "inner"), f("top.txt", "c0")],
        "inner": [f("leaf.txt", "c1")],
    })
    assert sorted(collect("1", db, ["root"], set())) == [
        "A\\deep\\leaf.txt", "A\\top.txt", "B\\deep\\leaf.txt", "B\\top.txt",
    ]


def test_a_renamed_directory_keeps_its_earlier_path(db):
    """Newest-first delta walk over two snapshots. The folder was renamed Old -> New; its
    contents are unchanged, so kopia gives both the same object."""
    seed(db, {
        "root_new": [d("New", "folder")],
        "root_old": [d("Old", "folder")],
        "folder": [f("keep.txt", "c1")],
    })
    seen: set = set()
    newest = collect("1", db, ["root_new"], seen)
    older = collect("1", db, ["root_old"], seen)
    assert newest == ["New\\keep.txt"]
    assert older == ["Old\\keep.txt"]


# --------------------------------------------------------------------------------------------
# The optimization the fix must preserve
# --------------------------------------------------------------------------------------------

def test_an_unchanged_directory_is_emitted_once_across_snapshots(db):
    """The delta index depends on this: an untouched directory contributes rows for the newest
    snapshot only. Same object AND same path -> visited once."""
    seed(db, {
        "root_a": [d("docs", "docs_v1")],
        "root_b": [d("docs", "docs_v1")],
        "docs_v1": [f("a.txt", "c1"), f("b.txt", "c2")],
    })
    seen: set = set()
    first = collect("1", db, ["root_a"], seen)
    second = collect("1", db, ["root_b"], seen)
    assert sorted(first) == ["docs\\a.txt", "docs\\b.txt"]
    assert second == []


def test_a_changed_directory_still_emits_again(db):
    seed(db, {
        "root_a": [d("docs", "docs_v2")],
        "root_b": [d("docs", "docs_v1")],
        "docs_v2": [f("a.txt", "c9")],
        "docs_v1": [f("a.txt", "c1")],
    })
    seen: set = set()
    assert collect("1", db, ["root_a"], seen) == ["docs\\a.txt"]
    assert collect("1", db, ["root_b"], seen) == ["docs\\a.txt"]


def test_one_fetch_per_object_even_when_it_sits_at_several_paths(db, monkeypatch):
    """The fan-out must happen after the fetch, not by fetching the same object twice - the
    fetch is the expensive half."""
    seed(db, {
        "root": [d("A", "shared"), d("B", "shared"), d("C", "shared")],
        "shared": [f("file.txt", "c1")],
    })
    calls: list[list[str]] = []
    real = walk.get_children

    def spy(repo, database, oids):
        calls.append(sorted(oids))
        return real(repo, database, oids)

    monkeypatch.setattr(walk, "get_children", spy)
    got = collect("1", db, ["root"], set())
    assert len(got) == 3
    assert calls == [["root"], ["shared"]]


# --------------------------------------------------------------------------------------------
# Content-only walks must keep object-level dedup
# --------------------------------------------------------------------------------------------

def test_walk_new_dedups_on_the_object_alone(db):
    """walk_new feeds content unions (churn, retention simulation). Visiting a shared directory
    twice there would be pure waste - the content set is identical."""
    seed(db, {
        "root": [d("A", "shared"), d("B", "shared")],
        "shared": [f("file.txt", "c1")],
    })
    seen: set = set()
    found = walk.walk_new("1", db, ["root"], seen)
    assert "c1" in found
    assert seen == {"root", "shared"}


def test_walk_without_a_visitor_is_unchanged(db):
    seed(db, {
        "root": [d("A", "shared"), d("B", "shared")],
        "shared": [f("file.txt", "c1")],
    })
    cids, stats = walk.walk("1", ["root"], db)
    assert "c1" in cids
    assert stats["dirs"] == 2          # root + shared, counted once each
    assert stats["files"] == 1         # one file OBJECT


def test_walk_with_a_visitor_reaches_both_locations(db):
    """The same fix applies to walk()'s visitor mode, which backs the reclaimable file list."""
    seed(db, {
        "root": [d("A", "shared"), d("B", "shared")],
        "shared": [f("file.txt", "c1")],
    })
    seen_paths: list[str] = []

    def visitor(parent: str, entry: dict) -> None:
        if entry.get("type") == "f":
            seen_paths.append((parent + "\\" + entry["name"]).lstrip("\\"))

    walk.walk("1", ["root"], db, visitor)
    assert sorted(seen_paths) == ["A\\file.txt", "B\\file.txt"]


# --------------------------------------------------------------------------------------------
# the indirect chunk-list cache
# --------------------------------------------------------------------------------------------

class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_an_unparsable_answer_is_not_cached_as_empty(db, monkeypatch):
    """The indirect rows are immutable, so caching an empty chunk list for a garbled answer
    would undercount that object's physical size permanently."""
    monkeypatch.setattr(walk, "kopia", lambda *a, **k: _Result(stdout="not json at all\n"))
    walk.expand_indirect("1", db, {"Ixaaa"})
    assert db.execute("SELECT count(*) FROM indirect").fetchone()[0] == 0


def test_a_truncated_batch_leaves_the_unanswered_objects_uncached(db, monkeypatch):
    """kopia aborts the whole `content show` batch at the first unreadable content: it prints the
    answers up to that point and exits non-zero. The objects after it got no answer and must not
    be recorded - previously they were silently dropped from the sizing with no trace."""
    answer = json.dumps({"stream": "kopia:indirect", "entries": [{"o": "chunk1"}]})
    monkeypatch.setattr(walk, "kopia",
                        lambda *a, **k: _Result(stdout=answer + "\n", returncode=1,
                                                stderr="error getting content xbbb: content not found"))
    parts = walk.expand_indirect("1", db, {"Ixaaa", "Ixbbb", "Ixccc"})
    cached = {row[0] for row in db.execute("SELECT oid FROM indirect").fetchall()}
    assert len(cached) == 1                     # only the object that actually got an answer
    assert "chunk1" in parts
    assert {"xaaa", "xbbb", "xccc"} <= parts    # the index ids themselves are always counted


def test_a_good_batch_still_caches_every_object(db, monkeypatch):
    answers = "\n".join(
        json.dumps({"stream": "kopia:indirect", "entries": [{"o": f"chunk{i}"}]}) for i in range(3)
    )
    monkeypatch.setattr(walk, "kopia", lambda *a, **k: _Result(stdout=answers + "\n"))
    parts = walk.expand_indirect("1", db, {"Ixaaa", "Ixbbb", "Ixccc"})
    assert db.execute("SELECT count(*) FROM indirect").fetchone()[0] == 3
    assert {"chunk0", "chunk1", "chunk2"} <= parts


def test_a_cached_row_is_read_back_through_the_codec(db, monkeypatch):
    db.execute("INSERT INTO indirect VALUES (?,?)", ("Ixaaa", cache.encode_json(["chunkX"])))
    db.commit()

    def _boom(*a, **k):
        raise AssertionError("a cached object must not be fetched again")

    monkeypatch.setattr(walk, "kopia", _boom)
    assert "chunkX" in walk.expand_indirect("1", db, {"Ixaaa"})
