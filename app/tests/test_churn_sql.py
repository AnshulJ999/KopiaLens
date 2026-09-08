"""The churn SQL, against a real (in-memory) DuckDB.

These are the invariants the ranking depends on:
  - only content whose FIRST appearance is inside the window counts
  - a path rewritten repeatedly contributes one row per version (the churn signal)
  - large/split files priced from iphys, small ones from csize
  - the deduped total counts each content once, however many paths hold it
  - an UNTOUCHED file is never reported as new, however recent its only delta row is
  - neither side of a rewrite dates from a delta re-stamp: a content enters on the first
    snapshot after the previous content's last snapshot, however old that is

The last two are the expensive ones. See the block comment above ``_QUALIFY_CTE`` in
churnview.py: `min(start_time)` alone read 5.5x high on real data, and a versions>1 guard
still re-qualified every rewrite older than the window nightly (measured 2026-09-08: repo 1's
24h view held 48,299 file versions of which ~39k were re-stamp artifacts). These tests pin the
correction, because it reasons about how the indexer builds ``filev`` - if that walk ever
changes, this file must fail loudly rather than let the tab go quietly wrong again.

The fixture builds the RAW artifacts (src/snap/pathx/filev/csize/iphys), which is what the
rewritten SQL reads; the helper mirrors the indexer's own pathx maintenance (first_seen =
min over versions, versions = count of DISTINCT snapshots).
"""

from __future__ import annotations

import duckdb
import pytest

from app.index.churnview import _MATERIALIZE_SQL, _base_sql, _deduped_sql, _qualify_params

SEP = chr(92)
CUTOFF = "2026-07-22 00:00:00"
SRC = "Anshul@pc:C:" + SEP

# id allocation + (source, path)/(content) -> id maps, reset per test by the fixture
_st: dict = {}


@pytest.fixture
def con():
    _st.clear()
    _st.update({"snap": 100, "pid": 0, "srcs": {}, "paths": {}, "cids": {}})
    c = duckdb.connect()
    c.execute("CREATE TABLE src(src_id SMALLINT, source VARCHAR)")
    c.execute("CREATE TABLE snap(snap_id INTEGER, snapshot_id VARCHAR, start_time TIMESTAMP, "
              "src_id SMALLINT)")
    c.execute("CREATE TABLE pathx(path_id INTEGER, src_id BIGINT, dir_id INTEGER, name VARCHAR, "
              "ext VARCHAR, size UBIGINT, versions INTEGER, first_seen TIMESTAMP, "
              "last_seen TIMESTAMP, in_latest BOOLEAN, cid_id INTEGER, physical UBIGINT, "
              "exclusive UBIGINT, full_path VARCHAR)")
    c.execute("CREATE TABLE filev(src_id BIGINT, path_id INTEGER, snap_id INTEGER, "
              "cid_id INTEGER, size UBIGINT, mtime TIMESTAMP, ordinal BIGINT)")
    c.execute("CREATE TABLE csize(cid_id INTEGER, packed UBIGINT)")
    c.execute("CREATE TABLE iphys(cid_id INTEGER, packed UBIGINT)")
    return c


def add_file(con, path: str, cid: str, ts: str, source: str = SRC) -> None:
    """Add a file version, and maintain its pathx row the way the indexer would.

    The snapshot id is the timestamp: each version comes from a different snapshot, and
    `pathx.versions` counts DISTINCT snapshot ids, so a shared constant here would make every
    path look like it had exactly one version and quietly disarm the test."""
    src = _st["srcs"].get(source)
    if src is None:
        src = _st["srcs"][source] = len(_st["srcs"]) + 1
        con.execute("INSERT INTO src VALUES(?, ?)", [src, source])
    c = _st["cids"].get(cid)
    if c is None:
        c = _st["cids"][cid] = len(_st["cids"]) + 1
    snap = _st["snap"]
    _st["snap"] += 1
    con.execute("INSERT INTO snap VALUES(?, ?, ?, ?)", [snap, ts, ts, src])
    key = (source, path)
    pid = _st["paths"].get(key)
    if pid is None:
        _st["pid"] += 1
        pid = _st["paths"][key] = _st["pid"]
        con.execute(
            "INSERT INTO pathx VALUES(?, ?, NULL, ?, NULL, 0, 0, ?, ?, true, ?, 0, NULL, ?)",
            [pid, src, path.rsplit(SEP, 1)[-1], ts, ts, c, path])
    con.execute("INSERT INTO filev VALUES(?, ?, ?, ?, 0, NULL, 0)", [src, pid, snap, c])
    con.execute(
        """
        UPDATE pathx SET
          first_seen = (SELECT min(sn.start_time) FROM filev f
                        JOIN snap sn ON sn.snap_id = f.snap_id WHERE f.path_id = pathx.path_id),
          last_seen = (SELECT max(sn.start_time) FROM filev f
                       JOIN snap sn ON sn.snap_id = f.snap_id WHERE f.path_id = pathx.path_id),
          versions = (SELECT count(DISTINCT f.snap_id) FROM filev f
                      WHERE f.path_id = pathx.path_id)
        WHERE path_id = ?
        """, [pid])


def set_first_seen(con, path: str, first_seen: str, source: str = SRC) -> None:
    """Override the sticky first-seen date. This is the whole bug in one helper: an untouched
    file has a filev row stamped today and a `first_seen` from years ago."""
    con.execute("UPDATE pathx SET first_seen=? WHERE path_id=?",
                [first_seen, _st["paths"][(source, path)]])


def set_packed(con, cid: str, packed: int) -> None:
    con.execute("INSERT INTO csize VALUES(?, ?)", [_st["cids"][cid], packed])


def set_ipacked(con, oid: str, packed: int) -> None:
    con.execute("INSERT INTO iphys VALUES(?, ?)", [_st["cids"][oid], packed])


TMP = "_t_newc"


def _materialize(con) -> str:
    """The qualifying set is built ONCE per call and both outputs derive from it.
    Every assertion below is unchanged, so they also prove the rewrite returns identical rows."""
    con.execute(_MATERIALIZE_SQL.format(tmp=TMP), _qualify_params(CUTOFF))
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
    set_packed(con, "old", 999)
    set_packed(con, "new", 10)
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
    set_packed(con, "c1", 10)
    set_packed(con, "c2", 20)
    set_packed(con, "c3", 30)
    row = base_rows(con)[0]
    assert row[3] == 60
    assert row[4] == 3   # three versions
    assert row[5] == 1   # of ONE file


def test_indirect_objects_are_priced(con):
    """Large/split files carry an I-prefixed object id that lives in iphys, not csize. Missing
    this would silently value every big file at zero."""
    add_file(con, "C:" + SEP + "big" + SEP + "v.iso", "Iabc", "2026-08-01")
    set_ipacked(con, "Iabc", 5000)
    assert base_rows(con)[0][3] == 5000


def test_unknown_content_is_zero_not_null(con):
    """Content missing from both maps must not turn the whole sum into NULL."""
    add_file(con, "C:" + SEP + "x" + SEP + "y.dat", "ghost", "2026-08-01")
    assert base_rows(con)[0][3] == 0


def test_extension_is_lowercased_and_blank_when_absent(con):
    add_file(con, "C:" + SEP + "a" + SEP + "LOUD.TXT", "c1", "2026-08-01")
    add_file(con, "C:" + SEP + "a" + SEP + "noext", "c2", "2026-08-01")
    set_packed(con, "c1", 1)
    set_packed(con, "c2", 2)
    exts = {r[2] for r in base_rows(con)}
    assert exts == {"txt", ""}


def test_folder_depth_controls_the_rollup(con):
    p = "C:" + SEP + "a" + SEP + "b" + SEP + "c" + SEP + "f.dat"
    add_file(con, p, "c1", "2026-08-01")
    set_packed(con, "c1", 10)
    assert base_rows(con, depth=2)[0][1] == "C:" + SEP + "a"
    assert base_rows(con, depth=3)[0][1] == "C:" + SEP + "a" + SEP + "b"
    # depth beyond the path never swallows the filename itself
    assert base_rows(con, depth=9)[0][1] == "C:" + SEP + "a" + SEP + "b" + SEP + "c"


def test_deduped_total_counts_shared_content_once(con):
    """Two paths holding the same content are charged twice in the ranking (that is the point -
    it says who is responsible) but only once in the deduped repo-growth figure."""
    add_file(con, "C:" + SEP + "a" + SEP + "one.dat", "shared", "2026-08-01")
    add_file(con, "C:" + SEP + "b" + SEP + "two.dat", "shared", "2026-08-01")
    set_packed(con, "shared", 100)
    assert sum(r[3] for r in base_rows(con)) == 200
    assert deduped(con) == 100


def test_rows_are_split_per_source(con):
    """A path covered by two sources is two rows - each source is separately responsible."""
    p = "C:" + SEP + "shared" + SEP + "f.dat"
    add_file(con, p, "c1", "2026-08-01", source=SRC)
    add_file(con, p, "c1", "2026-08-01", source="Anshul@pc:C:" + SEP + "shared")
    set_packed(con, "c1", 10)
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
    set_packed(con, "unchanged", 5000)
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
    set_packed(con, "before", 10)
    set_packed(con, "after", 700)
    rows = base_rows(con)
    assert len(rows) == 1
    assert rows[0][3] == 700     # only the new content, not the old one
    assert rows[0][4] == 1


def test_a_genuinely_new_file_counts_even_with_one_row(con):
    """Same single-row shape as the untouched file; `pathx.first_seen` is the only thing that
    tells them apart, which is why the fix leans on it rather than on row shape alone."""
    p = "C:" + SEP + "Downloads" + SEP + "installer.exe"
    add_file(con, p, "fresh", "2026-08-20")
    set_first_seen(con, p, "2026-08-20")
    set_packed(con, "fresh", 900)
    assert base_rows(con)[0][3] == 900


def test_a_rewrite_older_than_the_window_is_excluded(con):
    """An old path rewritten before the window: the new content's re-stamp is inside the window,
    but its true entry - the first snapshot after the previous content's last - is not, and no
    snapshot sits in between that could move it forward."""
    p = "C:" + SEP + "docs" + SEP + "notes.md"
    add_file(con, p, "v1", "2025-01-01")
    add_file(con, p, "v2", "2026-05-01")             # rewritten, but before the cutoff
    add_file(con, p, "v2", "2026-08-20")             # today's full walk re-stamps the same content
    set_first_seen(con, p, "2025-01-01")
    set_packed(con, "v1", 10)
    set_packed(con, "v2", 20)
    assert base_rows(con) == []


def test_the_old_side_of_a_rewrite_is_not_new_data(con):
    """The double-count. The previous content's only row is stamped with its LAST snapshot, so a
    rewrite landed inside the window qualified BOTH contents under the old rule - the replacement
    and the thing it replaced. Only the replacement is new data."""
    p = "C:" + SEP + "Chrome" + SEP + "profile.dat"
    add_file(con, p, "old", "2026-08-19")            # last held the day before the rewrite
    add_file(con, p, "new", "2026-08-20")            # the rewrite, inside the window
    set_first_seen(con, p, "2025-01-01")
    set_packed(con, "old", 400)
    set_packed(con, "new", 100)
    rows = base_rows(con)
    assert len(rows) == 1
    assert rows[0][3] == 100
    assert rows[0][4] == 1


def test_a_rewrite_dates_from_the_first_snapshot_after_a_gap(con):
    """A sparse source: the rewrite only becomes visible at the FIRST snapshot taken after the
    previous content's last one. The window boundary applies to that snapshot, not to when the
    previous content left."""
    p = "C:" + SEP + "monthly" + SEP + "dump.db"
    add_file(con, p, "old", "2026-07-01")            # previous content, before the cutoff
    add_file(con, p, "new", "2026-08-01")            # first snapshot after it - inside the window
    set_first_seen(con, p, "2025-01-01")
    set_packed(con, "old", 10)
    set_packed(con, "new", 200)
    rows = base_rows(con)
    assert len(rows) == 1
    assert rows[0][3] == 200


def test_untouched_and_churning_files_together(con):
    """The realistic mix: the ranking must show the churner and drop the noise around it. The d0
    row is the pre-rewrite content every rewrite leaves behind in the index - without it the
    oldest in-window version would look like retained-history residue, which the rule excludes."""
    churner = "C:" + SEP + "app" + SEP + "index.db"
    add_file(con, churner, "d0", "2026-08-10")
    add_file(con, churner, "d1", "2026-08-18")
    add_file(con, churner, "d2", "2026-08-19")
    add_file(con, churner, "d3", "2026-08-20")
    set_first_seen(con, churner, "2024-01-01")
    for i in range(5):
        stable = "C:" + SEP + "app" + SEP + f"lib{i}.dll"
        add_file(con, stable, f"s{i}", "2026-08-20")
        set_first_seen(con, stable, "2024-01-01")
    set_packed(con, "d0", 50)
    set_packed(con, "d1", 100)
    set_packed(con, "d2", 100)
    set_packed(con, "d3", 100)
    for i in range(5):
        set_packed(con, f"s{i}", 9)
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
    set_ipacked(con, "Iold", 10_000_000)
    assert sum(r[3] for r in base_rows(con)) == 0
    assert deduped(con) == 0
