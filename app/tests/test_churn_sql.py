"""The churn SQL, against a real (in-memory) DuckDB.

These are the invariants the ranking depends on:
  - only content whose FIRST appearance is inside the window counts
  - a path rewritten repeatedly contributes one row per version (the churn signal)
  - large/split files priced from indirect_phys, small ones from content_sizes
  - the deduped total counts each content once, however many paths hold it
  - an UNTOUCHED file is never reported as new, however recent its only delta row is

That last one is the expensive one. See the block comment above ``_QUALIFY_CTE`` in churnview.py:
`min(files.start_time)` alone read 5.5x high on real data because the delta index re-stamps
untouched files with the newest snapshot's timestamp. These tests pin the fix, because the
correction reasons about how the indexer builds ``files`` - if that walk ever changes, this file
must fail loudly rather than let the tab go quietly wrong again.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index.churnview import _MATERIALIZE_SQL, _base_sql, _deduped_sql, _qualify_params

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
    # Only the columns the churn query joins on. `first_seen` here is the STICKY, backfilled value,
    # which is exactly why it can disagree with min(files.start_time); `versions` mirrors the
    # indexer's own count(DISTINCT snapshot_id) from builder._PATHS_SQL.
    c.execute("CREATE TABLE paths(repo VARCHAR, source VARCHAR, full_path VARCHAR, "
              "first_seen TIMESTAMP, versions BIGINT)")
    return c


def add_file(con, path: str, cid: str, ts: str, source: str = SRC) -> None:
    """Add a file version, and rebuild its `paths` row the way the indexer would.

    The snapshot id is the timestamp: each version comes from a different snapshot, and
    `paths.versions` counts DISTINCT snapshot ids, so a shared constant here would make every path
    look like it had exactly one version and quietly disarm the test."""
    con.execute("INSERT INTO files VALUES('1',?,?,?,0,?,?,?,0,NULL)",
                [source, path, path.rsplit(SEP, 1)[-1], cid, ts, ts])
    con.execute("DELETE FROM paths WHERE repo='1' AND source=? AND full_path=?", [source, path])
    con.execute(
        "INSERT INTO paths SELECT '1', ?, ?, min(start_time), count(DISTINCT snapshot_id) "
        "FROM files WHERE repo='1' AND source=? AND full_path=?", [source, path, source, path])


def set_first_seen(con, path: str, first_seen: str, source: str = SRC) -> None:
    """Override the sticky first-seen date. This is the whole bug in one helper: an untouched file
    has a `files` row stamped today and a `first_seen` from years ago."""
    con.execute("UPDATE paths SET first_seen=? WHERE repo='1' AND source=? AND full_path=?",
                [first_seen, source, path])


TMP = "_t_newc"


def _materialize(con) -> str:
    """The qualifying set is now built ONCE per call and both outputs derive from it.
    Every assertion below is unchanged, so they also prove the refactor returns identical rows."""
    con.execute(_MATERIALIZE_SQL.format(tmp=TMP), _qualify_params("1", CUTOFF))
    return TMP


def base_rows(con, depth: int = 4) -> list[tuple]:
    return con.execute(
        _base_sql(_materialize(con)), [SEP, depth, SEP, SEP, SEP, SEP]).fetchall()


def deduped(con) -> int:
    return int(con.execute(_deduped_sql(_materialize(con))).fetchone()[0])


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


# ---- the delta-index re-stamp: untouched files must never read as new ---------------------------
def test_an_untouched_file_is_not_new_data(con):
    """THE bug. A file last modified years ago has exactly one row in the delta index, stamped with
    the NEWEST snapshot's time, because the walk skips its unchanged directory in every older
    snapshot. Read naively that says "first appeared today" - every day, forever. On repo 1 this
    inflated the 30-day file count from 245,775 to 1,349,520."""
    p = "C:" + SEP + "Python" + SEP + "site-packages" + SEP + "stable.py"
    add_file(con, p, "unchanged", "2026-08-20")      # only row: today's full walk
    set_first_seen(con, p, "2024-03-01")             # ...but the path is two years old
    con.execute("INSERT INTO content_sizes VALUES('1','unchanged',5000)")
    assert base_rows(con) == []
    assert deduped(con) == 0


def test_a_file_rewritten_in_the_newest_snapshot_still_counts(con):
    """The case that makes the discriminator per-PATH rather than per-(path, content). A file
    rewritten in the newest snapshot has only ONE row for its new content - same shape as an
    untouched file - but its PATH also carries the previous content's row. Counting per content
    group would throw this away, and it is real churn."""
    p = "C:" + SEP + "Chrome" + SEP + "profile.dat"
    add_file(con, p, "before", "2026-05-01")         # previous content, outside the window
    add_file(con, p, "after", "2026-08-20")          # rewritten in the newest snapshot
    set_first_seen(con, p, "2025-01-01")             # the path itself is old
    con.execute("INSERT INTO content_sizes VALUES('1','before',10),('1','after',700)")
    rows = base_rows(con)
    assert len(rows) == 1
    assert rows[0][3] == 700     # only the new content, not the old one
    assert rows[0][4] == 1


def test_a_genuinely_new_file_counts_even_with_one_row(con):
    """Same single-row shape as the untouched file; `paths.first_seen` is the only thing that tells
    them apart, which is why the fix leans on it rather than on row shape alone."""
    p = "C:" + SEP + "Downloads" + SEP + "installer.exe"
    add_file(con, p, "fresh", "2026-08-20")
    set_first_seen(con, p, "2026-08-20")
    con.execute("INSERT INTO content_sizes VALUES('1','fresh',900)")
    assert base_rows(con)[0][3] == 900


def test_a_rewrite_older_than_the_window_is_excluded(con):
    """An old path rewritten before the window has >1 row, so it passes the path test - the content
    group's own min(start_time) is what correctly excludes it. Both halves have to hold."""
    p = "C:" + SEP + "docs" + SEP + "notes.md"
    add_file(con, p, "v1", "2025-01-01")
    add_file(con, p, "v2", "2026-05-01")             # rewritten, but before the cutoff
    add_file(con, p, "v2", "2026-08-20")             # today's full walk re-stamps the same content
    set_first_seen(con, p, "2025-01-01")
    con.execute("INSERT INTO content_sizes VALUES('1','v1',10),('1','v2',20)")
    assert base_rows(con) == []


def test_untouched_and_churning_files_together(con):
    """The realistic mix: the ranking must show the churner and drop the noise around it."""
    churner = "C:" + SEP + "app" + SEP + "index.db"
    add_file(con, churner, "d1", "2026-08-18")
    add_file(con, churner, "d2", "2026-08-19")
    add_file(con, churner, "d3", "2026-08-20")
    set_first_seen(con, churner, "2024-01-01")
    for i in range(5):
        stable = "C:" + SEP + "app" + SEP + f"lib{i}.dll"
        add_file(con, stable, f"s{i}", "2026-08-20")
        set_first_seen(con, stable, "2024-01-01")
    con.execute("INSERT INTO content_sizes VALUES('1','d1',100),('1','d2',100),('1','d3',100)")
    con.execute("INSERT INTO content_sizes VALUES('1','s0',9),('1','s1',9),('1','s2',9),"
                "('1','s3',9),('1','s4',9)")
    rows = base_rows(con)
    assert len(rows) == 1
    assert rows[0][3] == 300     # the churner only
    assert rows[0][4] == 3       # three versions
    assert rows[0][5] == 1       # of one file


def test_deduped_total_comes_from_the_same_qualifying_set(con):
    """The attributed and deduped figures used to be independent queries over independently derived
    sets, so a fix to one could silently skip the other. They now share one CTE."""
    p = "C:" + SEP + "stale" + SEP + "big.iso"
    add_file(con, p, "Iold", "2026-08-20")
    set_first_seen(con, p, "2023-01-01")
    con.execute("INSERT INTO indirect_phys VALUES('1','Iold',10_000_000)")
    assert sum(r[3] for r in base_rows(con)) == 0
    assert deduped(con) == 0
