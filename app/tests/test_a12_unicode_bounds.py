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
