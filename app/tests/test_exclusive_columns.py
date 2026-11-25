"""paths.exclusive - packed bytes held ONLY by one source (within-source sharing ignored)."""

from __future__ import annotations

import duckdb

from app.index.physical import materialize_exclusive


def _con():
    con = duckdb.connect()
    con.execute("CREATE TABLE paths(repo VARCHAR, source VARCHAR, full_path VARCHAR, "
                "content_id VARCHAR, physical UBIGINT, exclusive UBIGINT)")
    rows = [
        # content A lives in TWO sources -> exclusive 0 for both paths
        ("1", "C:\\a", "C:\\a\\dup.iso", "A", 100, None),
        ("1", "D:\\b", "D:\\b\\dup.iso", "A", 100, None),
        # content B exists only in C:\a -> its path is exclusive
        ("1", "C:\\a", "C:\\a\\mine.mkv", "B", 50, None),
        # content C: two paths WITHIN one source (within-source sharing ignored -> still exclusive)
        ("1", "C:\\a", "C:\\a\\x.bin", "C", 10, None),
        ("1", "C:\\a", "C:\\a\\y.bin", "C", 10, None),
        # another repo's copy of A must not count (repo-scoped)
        ("2", "Z:\\z", "Z:\\z\\dup.iso", "A", 100, None),
        # a NULL-content path is skipped entirely
        ("1", "C:\\a", "C:\\a\\orphan.txt", None, 7, None),
    ]
    con.executemany("INSERT INTO paths VALUES (?, ?, ?, ?, ?, ?)", rows)
    return con


def test_exclusive_flags_single_source_content_and_zeroes_shared():
    con = _con()
    materialize_exclusive(con, "1")
    got = {(s, p): e for s, p, e in
           con.execute("SELECT source, full_path, exclusive FROM paths WHERE repo='1'").fetchall()}
    assert got[("C:\\a", "C:\\a\\dup.iso")] == 0      # content A is in D:\b too
    assert got[("D:\\b", "D:\\b\\dup.iso")] == 0
    assert got[("C:\\a", "C:\\a\\mine.mkv")] == 50    # only source holding content B
    assert got[("C:\\a", "C:\\a\\x.bin")] == 10       # within-source sharing stays exclusive
    assert got[("C:\\a", "C:\\a\\y.bin")] == 10
    assert got[("C:\\a", "C:\\a\\orphan.txt")] is None  # NULL content untouched


def test_exclusive_is_repo_scoped():
    con = _con()
    materialize_exclusive(con, "2")
    e = con.execute("SELECT exclusive FROM paths WHERE repo='2'").fetchone()[0]
    assert e == 100  # repo 1's copy of A does not make repo 2's copy shared
