"""The churn SQL, against a real (in-memory) DuckDB.

These are the invariants the ranking depends on:
  - only content whose FIRST appearance is inside the window counts
  - a path rewritten repeatedly contributes one row per version (the churn signal)
  - large/split files priced from indirect_phys, small ones from content_sizes
  - the deduped total counts each content once, however many paths hold it
"""

from __future__ import annotations

import duckdb
import pytest

from app.index.churnview import _BASE_SQL, _DEDUPED_SQL

SEP = chr(92)
CUTOFF = "2026-07-22 00:00:00"
SRC = "Anshul@pc:C:" + SEP


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE files(repo VARCHAR, source VARCHAR, full_path VARCHAR, name VARCHAR, "
              "size UBIGINT, content_id VARCHAR, snapshot_id VARCHAR, start_time TIMESTAMP, "
              "ordinal BIGINT, mtime TIMESTAMP)")
    c.execute("CREATE TABLE content_sizes(repo VARCHAR, cid VARCHAR, packed UBIGINT)")
    c.execute("CREATE TABLE indirect_phys(repo VARCHAR, oid VARCHAR, packed UBIGINT)")
    return c


def add_file(con, path: str, cid: str, ts: str, source: str = SRC) -> None:
    con.execute("INSERT INTO files VALUES('1',?,?,?,0,?,'snap',?,0,NULL)",
                [source, path, path.rsplit(SEP, 1)[-1], cid, ts])


def base_rows(con, depth: int = 4) -> list[tuple]:
    return con.execute(_BASE_SQL, ["1", "1", "1", CUTOFF, SEP, depth, SEP, SEP, SEP, SEP]).fetchall()


def deduped(con) -> int:
    return int(con.execute(_DEDUPED_SQL, ["1", "1", "1", CUTOFF]).fetchone()[0])


def test_only_content_first_seen_in_the_window_counts(con):
    p = "C:" + SEP + "a" + SEP + "f.dat"
    add_file(con, p, "old", "2025-01-01")   # outside the window
    add_file(con, p, "new", "2026-08-01")   # inside
    con.execute("INSERT INTO content_sizes VALUES('1','old',999),('1','new',10)")
    rows = base_rows(con)
    assert len(rows) == 1
    assert rows[0][3] == 10       # bytes: only the in-window content
    assert rows[0][4] == 1        # versions
    assert rows[0][5] == 1        # distinct files


def test_a_rewritten_file_counts_every_new_version(con):
    """The Chrome case: one file, many rewrites. Versions is what exposes it."""
    p = "C:" + SEP + "Chrome" + SEP + "profile.dat"
    for i, cid in enumerate(("c1", "c2", "c3")):
        add_file(con, p, cid, f"2026-08-0{i + 1}")
    con.execute("INSERT INTO content_sizes VALUES('1','c1',10),('1','c2',20),('1','c3',30)")
    row = base_rows(con)[0]
    assert row[3] == 60
    assert row[4] == 3   # three versions
    assert row[5] == 1   # of ONE file


def test_indirect_objects_are_priced(con):
    """Large/split files carry an I-prefixed object id that lives in indirect_phys, not
    content_sizes. Missing this would silently value every big file at zero."""
    add_file(con, "C:" + SEP + "big" + SEP + "v.iso", "Iabc", "2026-08-01")
    con.execute("INSERT INTO indirect_phys VALUES('1','Iabc',5000)")
    assert base_rows(con)[0][3] == 5000


def test_unknown_content_is_zero_not_null(con):
    """Content missing from both maps must not turn the whole sum into NULL."""
    add_file(con, "C:" + SEP + "x" + SEP + "y.dat", "ghost", "2026-08-01")
    assert base_rows(con)[0][3] == 0


def test_extension_is_lowercased_and_blank_when_absent(con):
    add_file(con, "C:" + SEP + "a" + SEP + "LOUD.TXT", "c1", "2026-08-01")
    add_file(con, "C:" + SEP + "a" + SEP + "noext", "c2", "2026-08-01")
    con.execute("INSERT INTO content_sizes VALUES('1','c1',1),('1','c2',2)")
    exts = {r[2] for r in base_rows(con)}
    assert exts == {"txt", ""}


def test_folder_depth_controls_the_rollup(con):
    p = "C:" + SEP + "a" + SEP + "b" + SEP + "c" + SEP + "f.dat"
    add_file(con, p, "c1", "2026-08-01")
    con.execute("INSERT INTO content_sizes VALUES('1','c1',10)")
    assert base_rows(con, depth=2)[0][1] == "C:" + SEP + "a"
    assert base_rows(con, depth=3)[0][1] == "C:" + SEP + "a" + SEP + "b"
    # depth beyond the path never swallows the filename itself
    assert base_rows(con, depth=9)[0][1] == "C:" + SEP + "a" + SEP + "b" + SEP + "c"


def test_deduped_total_counts_shared_content_once(con):
    """Two paths holding the same content are charged twice in the ranking (that is the point -
    it says who is responsible) but only once in the deduped repo-growth figure."""
    add_file(con, "C:" + SEP + "a" + SEP + "one.dat", "shared", "2026-08-01")
    add_file(con, "C:" + SEP + "b" + SEP + "two.dat", "shared", "2026-08-01")
    con.execute("INSERT INTO content_sizes VALUES('1','shared',100)")
    assert sum(r[3] for r in base_rows(con)) == 200
    assert deduped(con) == 100


def test_rows_are_split_per_source(con):
    """A path covered by two sources is two rows - each source is separately responsible."""
    p = "C:" + SEP + "shared" + SEP + "f.dat"
    add_file(con, p, "c1", "2026-08-01", source=SRC)
    add_file(con, p, "c1", "2026-08-01", source="Anshul@pc:C:" + SEP + "shared")
    con.execute("INSERT INTO content_sizes VALUES('1','c1',10)")
    assert len({r[0] for r in base_rows(con)}) == 2
    assert deduped(con) == 10  # ...but it is still one content on disk
