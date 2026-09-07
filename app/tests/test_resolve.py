"""The natural-key resolver - id lookups, per-source identity rules, generation-scoped caches.

`src`/`snap` load whole per generation; `pathx`/`dir` match under the source's own case rules
(via core.paths.ident); `cid` resolves batched both ways. The cache proof deletes a dictionary
row behind a live cursor: within the generation the answer survives (cache), on a fresh
generation it re-resolves (scoping) - a cache that crossed a publish would return stale ids.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index import resolve
from app.index.store import Generation, _PinnedCursor

SEP = chr(92)


@pytest.fixture
def cur():
    """Raw dictionary tables - the resolver only runs SELECTs against them, views vs base
    tables is irrelevant, and base tables let the cache test DELETE a row."""
    con = duckdb.connect()
    con.execute(f"CREATE TABLE src AS SELECT * FROM (VALUES "
                f"(1::SMALLINT, 's1:C:{SEP}A'), (3::SMALLINT, 'u@h:/srv')) t(src_id, source)")
    con.execute("CREATE TABLE snap AS SELECT * FROM (VALUES "
                "(100::INTEGER, 'snap1', TIMESTAMP '2025-01-01 00:00:00', 1::SMALLINT)) "
                "t(snap_id, snapshot_id, start_time, src_id)")
    con.execute("CREATE TABLE cid AS SELECT * FROM (VALUES "
                "(10::INTEGER, 'obj_a'), (20, 'obj_b')) t(cid_id, content_id)")
    con.execute(f"CREATE TABLE pathx AS SELECT * FROM (VALUES "
                f"(1::INTEGER, 1::BIGINT, 2::INTEGER, 'a.txt', 'txt', 100::UBIGINT, 2::INTEGER, "
                f"TIMESTAMP '2025-01-01', TIMESTAMP '2025-01-02', true, 10::INTEGER, "
                f"100::UBIGINT, NULL::UBIGINT, 'C:{SEP}A{SEP}sub{SEP}a.txt'), "
                f"(2, 3, 4, 'x.txt', 'txt', 5::UBIGINT, 1::INTEGER, "
                f"TIMESTAMP '2025-01-01', TIMESTAMP '2025-01-01', true, 20::INTEGER, "
                f"5::UBIGINT, NULL::UBIGINT, '/srv/x.txt')) "
                f"t(path_id, src_id, dir_id, name, ext, size, versions, first_seen, last_seen, "
                f"in_latest, cid_id, physical, exclusive, full_path)")
    con.execute(f"CREATE TABLE dir AS SELECT * FROM (VALUES "
                f"(2::INTEGER, 1::SMALLINT, 'C:{SEP}A{SEP}sub', 2::SMALLINT, 'sub', 1::INTEGER), "
                f"(4, 3, '/srv', 1, 'srv', NULL)) "
                f"t(dir_id, src_id, dir_path, depth, name, parent_id)")
    return con


def test_source_and_snapshot_resolve(cur):
    assert resolve.src_id(cur, f"s1:C:{SEP}A") == 1
    assert resolve.snap_id(cur, "snap1") == 100
    assert resolve.src_id(cur, "nope") is None
    assert resolve.snap_id(cur, "nope") is None


def test_path_and_dir_follow_source_case_rules(cur):
    assert resolve.path_id(cur, 1, f"C:{SEP}A{SEP}SUB{SEP}a.txt") == 1
    assert resolve.dir_id(cur, 1, f"c:{SEP}a{SEP}sub") == 2
    assert resolve.path_id(cur, 999, "whatever") is None
    # POSIX source: identity is case-sensitive
    assert resolve.path_id(cur, 3, "/srv/X.txt") is None
    assert resolve.path_id(cur, 3, "/srv/x.txt") == 2
    assert resolve.dir_id(cur, 3, "/srv") == 4


def test_content_ids_batch_and_reverse(cur):
    assert resolve.cid_ids(cur, ["obj_a", "obj_b", "obj_b", "gone", ""]) == \
        {"obj_a": 10, "obj_b": 20}
    assert resolve.cid_id(cur, "gone") is None
    assert resolve.content_ids(cur, [10, 20, 99]) == {10: "obj_a", 20: "obj_b"}


def test_path_ids_batched(cur):
    """One scan per source group; case variants of the same path share one answer; unresolvable
    and invalid pairs are absent, never errors."""
    pairs = [(1, f"C:{SEP}A{SEP}SUB{SEP}a.txt"), (1, f"c:{SEP}a{SEP}sub{SEP}A.TXT"),
             (3, "/srv/x.txt"), (3, "/srv/missing.txt"), (999, "junk"), (1, "")]
    assert resolve.path_ids(cur, pairs) == {
        (1, f"C:{SEP}A{SEP}SUB{SEP}a.txt"): 1,
        (1, f"c:{SEP}a{SEP}sub{SEP}A.TXT"): 1,
        (3, "/srv/x.txt"): 2,
    }


def test_cache_is_scoped_to_the_generation(cur):
    gen1 = Generation("1", 1, {}, cur, 0)
    c1 = _PinnedCursor(gen1, cur.cursor())
    assert resolve.src_id(c1, f"s1:C:{SEP}A") == 1
    cur.execute("DELETE FROM src")                     # a re-resolve would now return None
    assert resolve.src_id(c1, f"s1:C:{SEP}A") == 1     # served from the generation's cache
    gen2 = Generation("1", 2, {}, cur, 0)
    c2 = _PinnedCursor(gen2, cur.cursor())
    assert resolve.src_id(c2, f"s1:C:{SEP}A") is None  # a new generation re-resolves


def test_raw_cursor_without_generation_still_resolves(cur):
    assert resolve.src_id(cur, f"s1:C:{SEP}A") == 1
    assert resolve.path_id(cur.cursor(), 3, "/srv/x.txt") == 2
