"""The code-review fixes, each pinned by the failure it would otherwise reproduce.

These are deliberately negative tests. Every one of them passed before the fix by accident - on a
connection that happened to preserve insertion order, on a memo that could not tell "not looked
up" from "unknown", on a counter whose two halves happened to agree - so a test that only asserts
the happy path proves nothing about any of them.
"""

from __future__ import annotations

import os

import duckdb
import pytest

from app.index import artifacts, crypto, entries, physical, publish, sidecache, treesync
from app.tests.test_incremental import _listing_published, _patch
from app.tests.test_treesync import FakeKopia

SEP = chr(92)


# ------------------------------------------------- positional pairing of kopia's answers

@pytest.mark.parametrize("preserve", [True, False])
def test_line_positions_survive_preserve_insertion_order_off(preserve):
    """`content show` output carries no object id - POSITION is the only mapping back to the
    request, and wrong pairing is silent: entries land under the wrong folders.

    Honest about what this pins. The previous query numbered rows with `row_number() OVER ()`,
    which has no ordering guarantee at all; the build connection now runs with
    `preserve_insertion_order=false`, where that guarantee would matter. It could NOT be made to
    mispair on DuckDB 1.5.5 at 5k or 200k lines, single- or multi-threaded - so this is a
    regression test for a rule, not a reproduction of a defect. The rule is worth holding because
    nothing in it is promised, the cost of ordering explicitly is nil, and the failure it guards
    against produces no error.
    """
    con = duckdb.connect()
    try:
        con.execute("SET preserve_insertion_order=%s" % ("true" if preserve else "false"))
        # Enough lines, and wide enough, that a reordering executor has room to reorder.
        lines = ["{\"n\": %d, \"pad\": \"%s\"}" % (i, "x" * 500) for i in range(5000)]
        parsed = treesync._split_lines("\n".join(lines) + "\n\n")
        treesync._lines_table(con, parsed, "_lines")
        assert len(parsed) == 5000
        got = con.execute(
            "SELECT count(*) FROM _lines WHERE json_extract(line, '$.n')::BIGINT <> pos - 1"
        ).fetchone()[0]
        assert got == 0, f"{got} line(s) paired to the wrong position"
    finally:
        con.close()


def test_blank_lines_are_dropped_before_positions_are_assigned():
    """A blank line must not consume a position, or every object after it pairs one place off."""
    con = duckdb.connect()
    try:
        parsed = treesync._split_lines("\n".join(["{\"n\":0}", "", "{\"n\":1}", "  ", "{\"n\":2}"]))
        treesync._lines_table(con, parsed, "_lines")
        assert len(parsed) == 3
        rows = con.execute("SELECT pos, line FROM _lines ORDER BY pos").fetchall()
        assert [p for p, _ in rows] == [1, 2, 3]
        assert [json_n(line) for _, line in rows] == [0, 1, 2]
    finally:
        con.close()


def json_n(line: str) -> int:
    import json
    return json.loads(line)["n"]


# ------------------------------------------------- unknown chunks are not zero bytes

def test_an_object_the_tree_does_not_hold_reads_as_unknown_not_empty(monkeypatch):
    """`_chunks_for_obj` returning an empty set would price a split file at zero bytes. It must
    return None so callers can tell "no chunks" from "I cannot say"."""
    monkeypatch.setattr(physical, "read_cursor", lambda repo: _EmptyCursor())
    assert physical._chunks_for_obj("1", "Ideadbeef") is None
    assert physical._chunks_for_obj("1", "plain_obj") == {"plain_obj"}
    assert physical._chunks_for_obj("1", "") == set()


def test_an_unknown_chunk_set_is_never_memoized(monkeypatch):
    """The memo is keyed on presence, so caching a None both defeats the memo and hands the
    caller a None where it expects a set. Two lookups must both reach the source."""
    calls = []

    def _fake(repo, obj, *, fetch=False):
        calls.append(obj)
        return None

    monkeypatch.setattr(physical, "_chunks_for_obj", _fake)
    memo: dict = {}
    assert physical._chunks_memo("1", "Ix", memo) is None
    assert physical._chunks_memo("1", "Ix", memo) is None
    assert "Ix" not in memo
    assert calls == ["Ix", "Ix"]


def test_a_known_chunk_set_is_memoized(monkeypatch):
    calls = []
    monkeypatch.setattr(physical, "_chunks_for_obj",
                        lambda repo, obj, *, fetch=False: calls.append(obj) or {"c1"})
    memo: dict = {}
    assert physical._chunks_memo("1", "Ix", memo) == {"c1"}
    assert physical._chunks_memo("1", "Ix", memo) == {"c1"}
    assert calls == ["Ix"]


class _EmptyCursor:
    """A generation that holds no tree - what a brand-new snapshot's objects look like."""

    def execute(self, *_a, **_kw):
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self):
        pass


# ------------------------------------------------- an incomplete sync must not be forgotten

def test_an_incomplete_tree_is_recorded_on_the_manifest(tmp_path, monkeypatch):
    """A source built from a tree kopia would not read whole is still marked indexed, so its
    snapshot set matches the listing forever and it is carried by reference every later night.
    The flag is what breaks that loop, and it has to be a PUBLIC manifest key - underscore keys
    are stripped before the manifest is written."""
    root = str(tmp_path / "repo1")
    os.makedirs(root)
    _patch(monkeypatch)
    # One folder object kopia refuses.
    from app.tests.test_incremental import TREE
    monkeypatch.setattr(treesync, "kopia",
                        FakeKopia(dict(TREE), broken={"root_sub"}))
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    body = artifacts.build_repo("1", root, gen=1, job={})
    publish.publish(root, body)
    manifest = _read_manifest(root)
    assert "tree_incomplete" in manifest, "the flag must survive into the written manifest"


def test_a_previously_incomplete_tree_forces_every_source_to_rebuild(tmp_path, monkeypatch):
    """The whole point of the flag: the next build must not carry the short sources forward."""
    root = str(tmp_path / "repo1")
    os.makedirs(root)
    _patch(monkeypatch)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    body = artifacts.build_repo("1", root, gen=1, job={})
    publish.publish(root, body)

    # Same listing: with a clean previous tree every source carries, with the flag none does.
    job: dict = {}
    body2 = artifacts.build_repo("1", root, gen=2, job=job)
    assert job["targets"] == 0, "an unchanged listing should carry every source"

    manifest = _read_manifest(root)
    manifest["tree_incomplete"] = ["obj_a", "obj_b", "obj_c"]
    _write_manifest(root, manifest)
    job2: dict = {}
    body3 = artifacts.build_repo("1", root, gen=3, job=job2)
    assert job2["targets"] == len(_listing_published())

    # ... but only ONCE for that set. A permanently unreadable object must not rebuild every
    # source every night forever, so an unchanged set does not force again.
    publish.publish(root, body3)
    m = _read_manifest(root)
    m["tree_incomplete"] = ["obj_a", "obj_b", "obj_c"]
    m["tree_forced_for"] = ["obj_a", "obj_b", "obj_c"]
    _write_manifest(root, m)
    job3: dict = {}
    artifacts.build_repo("1", root, gen=4, job=job3)
    assert job3["targets"] == 0, "the same unreadable set must not force a second rebuild"


def _read_manifest(root: str) -> dict:
    import json
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _write_manifest(root: str, manifest: dict) -> None:
    import json
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


# ------------------------------------------------- the progress counter's two halves agree

def test_progress_total_and_indexed_carry_the_same_unit(tmp_path, monkeypatch):
    """`index_status` renders these as one pair. `indexed` ticks per rebuilt SOURCE, so a total
    counting SNAPSHOTS made the strip read "3 / 6,629" and never approach the end."""
    root = str(tmp_path / "repo1")
    os.makedirs(root)
    _patch(monkeypatch)
    monkeypatch.setattr(artifacts, "sources_index", lambda repo: _listing_published())
    job: dict = {}
    artifacts.build_repo("1", root, gen=1, job=job)
    assert job["total"] == job["targets"] == len(_listing_published())
    assert job["indexed"] == job["total"], "every target should have been counted once"


def test_pct_is_a_real_fraction_of_the_sources_being_rebuilt():
    from app.index.worker import _pct
    assert _pct({"total": 4, "indexed": 1}) == 25
    assert _pct({"total": 0, "indexed": 0}) is None       # nothing to rebuild -> no denominator
    assert _pct({"sync": {"level": 3, "folders": 12}}) is None   # never a fabricated bar
    assert _pct({"seed": {"done": 25, "total": 100}}) == 25


# ------------------------------------------------- encryption reaches the writers

def test_the_build_scratch_round_trips_encrypted_parquet(keystore, tmp_path):
    """`copy_options` NAMES the key; only the connection registers it. A scratch built by hand had
    neither, so an encrypted build would have written plaintext or failed at the COPY."""
    crypto.setup("pw", with_recovery=False)
    out = str(tmp_path / "enc.parquet").replace(chr(92), "/")
    con = crypto.open_scratch(str(tmp_path / "b.duckdb"), str(tmp_path / "tmp"), "256MB", 2)
    try:
        con.execute("CREATE TABLE t AS SELECT 42 AS answer")
        con.execute(f"COPY t TO '{out}' ({crypto.copy_options()})")
        got = con.execute(
            "SELECT answer FROM read_parquet(%s)" % crypto.parquet_read(out)).fetchone()[0]
        assert got == 42
    finally:
        con.close()
    plain = duckdb.connect()
    try:
        with pytest.raises(duckdb.Error):
            plain.execute(f"SELECT * FROM read_parquet('{out}')").fetchall()
    finally:
        plain.close()


def test_the_build_scratch_is_a_plain_database_when_encryption_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "enabled", lambda: False)
    con = crypto.open_scratch(str(tmp_path / "b.duckdb"), str(tmp_path / "tmp"), "256MB", 2)
    try:
        con.execute("CREATE TABLE t AS SELECT 1 AS a")
        assert con.execute("SELECT a FROM t").fetchone()[0] == 1
    finally:
        con.close()


def test_a_side_cache_round_trips_while_encryption_is_on(keystore, tmp_path, monkeypatch):
    """Without the registered key every write returned False and every read None - the caches
    stopped working entirely the moment encryption was switched on."""
    crypto.setup("pw", with_recovery=False)
    base = str(tmp_path / "cache")
    assert sidecache.write("probe", {"a": 1}, base=base) is True
    assert sidecache.read("probe", base=base) == {"a": 1}
    plain = duckdb.connect()
    try:
        p = sidecache.path("probe", base).replace(chr(92), "/")
        with pytest.raises(duckdb.Error):
            plain.execute(f"SELECT * FROM read_parquet('{p}')").fetchall()
    finally:
        plain.close()


def test_a_side_cache_is_never_written_in_the_clear_while_locked(keystore, tmp_path):
    crypto.setup("pw", with_recovery=False)
    crypto.lock()
    base = str(tmp_path / "cache")
    assert sidecache.write("probe", {"a": 1}, base=base) is False
    assert sidecache.read("probe", base=base) is None


# ------------------------------------------------- the folder LRU accounts by stored size

def test_the_entry_lru_evicts_by_size_without_remeasuring(monkeypatch):
    """The size is stored beside the entries. Recomputing it on every eviction walked 50k dicts
    inside the global lock - the same mistake the byte-budget cache this replaces had to unlearn."""
    monkeypatch.setattr(entries, "LRU_MAX_BYTES", 4000)
    entries._LRU.clear()
    entries._LRU_BYTES.clear()
    measured = []
    real = entries._bytes_of
    monkeypatch.setattr(entries, "_bytes_of", lambda e: measured.append(1) or real(e))
    for i in range(40):
        entries._lru_put("1", f"oid{i}", [{"name": f"f{i}", "obj": "o", "size": i}])
    # One measurement per PUT and none per eviction.
    assert len(measured) == 40
    assert entries._LRU_BYTES["1"] <= entries.LRU_MAX_BYTES
    assert entries._LRU["1"], "the cache should still hold the most recent entries"
    hit = entries._lru_get("1", ["oid39"])
    assert hit["oid39"] == [{"name": "f39", "obj": "o", "size": 39}]


@pytest.fixture
def keystore(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "keystore_dir", lambda: str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "enabled", lambda: crypto.configured())
    monkeypatch.setattr(crypto, "remember_on_this_machine", lambda on: True)
    crypto.lock()
    yield tmp_path
    crypto.lock()


def test_parse_chunks_cover_every_line_once_in_order():
    """Chunked parsing pairs answers to objects by (start + offset); a gap, overlap or wrong start
    puts entries under the wrong folder silently."""
    lines = ["x" * n for n in (5, 40, 3, 3, 3, 100, 1, 60, 2)]
    chunks = list(treesync._chunks(lines, 50))
    assert [ln for _, c in chunks for ln in c] == lines
    assert [s for s, _ in chunks] == [sum(len(c) for _, c in chunks[:k]) for k in range(len(chunks))]
    assert all(sum(map(len, c)) <= 50 or len(c) == 1 for _, c in chunks)
    assert list(treesync._chunks([], 50)) == []
