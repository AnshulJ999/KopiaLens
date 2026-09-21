""" Prefix range bounds must cover names that start above the BMP (emoji, rare glyphs)."""

from __future__ import annotations

SEP = chr(92)
LO = f"c:{SEP}x{SEP}"


def test_prefix_bounds_include_astral_names():
    from app.core.paths import prefix_bounds

    lo, hi = prefix_bounds(LO)
    assert (lo, hi) == (LO, f"c:{SEP}x]")
    assert lo <= LO + "a.txt" < hi
    assert lo <= LO + chr(0x1F600) + "f.txt" < hi     # the old `lo + U+FFFF` bound excluded this
    assert lo <= LO + chr(0x10FFFF) + "z" < hi
    assert not (lo <= f"c:{SEP}y{SEP}" < hi)          # a sibling folder stays outside


def test_the_excluded_browser_uses_the_correct_bound():
    from app.index import excluded

    lo, hi, plen = excluded._prefix_bounds(f"C:{SEP}X")
    assert (lo, hi) == (LO, f"c:{SEP}x]") and plen == len(LO)
    assert LO + chr(0x1F600) + "f.txt" < hi


def test_backblaze_rollup_bounds_match_the_helper():
    from app.backblaze import queries as bzq
    from app.core.paths import prefix_bounds

    lo = f"c:{SEP}x{SEP}"
    assert bzq.prefix_bounds(lo) == prefix_bounds(lo)


def test_backblaze_rollup_clears_astral_named_children():
    """ ``_write_rollup`` rewrites a drive's rows with the same bound the folder
    views use. The old ``lo + U+FFFF`` left every row whose parent name starts above the BMP
    behind - and the helper was not even imported in this module (8 red BZ tests)."""
    import sqlite3

    from app.backblaze import bbdb, indexer

    con = sqlite3.connect(":memory:")
    bbdb._init_schema(con)
    root_lc = f"c:{SEP}"
    emoji = chr(0x1F600)
    con.execute("INSERT INTO folder_rollup VALUES (?,?,?,?,?,?,?,?,?)",
                [root_lc, root_lc + "data", "data", 1, 1, 1000, 0, 0, None])
    con.execute("INSERT INTO folder_rollup VALUES (?,?,?,?,?,?,?,?,?)",
                [root_lc + emoji + SEP, root_lc + emoji + SEP + "data", "data", 1, 1, 1000, 0, 0, None])
    con.execute("INSERT INTO folder_rollup VALUES (?,?,?,?,?,?,?,?,?)",
                [f"d:{SEP}" + emoji + SEP, f"d:{SEP}" + emoji + SEP + "data", "data", 1, 1, 1000, 0, 0, None])

    indexer._write_rollup(con, root_lc, {}, {})

    kept = [r[0] for r in con.execute("SELECT parent_lc FROM folder_rollup").fetchall()]
    assert kept == [f"d:{SEP}" + emoji + SEP]   # this drive was rewritten; the other drive was not
