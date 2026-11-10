"""LIKE wildcard escaping for user-supplied strings, against a real DuckDB.

`_` matches any single character and `%` matches anything, so an unescaped folder name or search
term silently broadens the match. Verified live on repo 1 before the fix: searching for `_` returned
all 5,193,822 paths instead of the ones containing an underscore. The same unescaped pattern fed
`remove_targets`, which enumerates what a chunked DELETE is about to remove - which is why this is
tested rather than left as a cosmetic search quirk.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index.queries import _ESC, like_literal

SEP = chr(92)


@pytest.fixture
def rows():
    c = duckdb.connect()
    c.execute("CREATE TABLE t(p VARCHAR)")
    for p in (
        "C:" + SEP + "Backup_2024" + SEP + "keep.txt",   # the intended folder
        "C:" + SEP + "BackupX2024" + SEP + "other.txt",  # `_` would also match this
        "C:" + SEP + "100% Done" + SEP + "keep.txt",     # the intended folder
        "C:" + SEP + "100 percent Done" + SEP + "x.txt",
        "C:" + SEP + "1000 Done more" + SEP + "y.txt",   # `%` would also match this
    ):
        c.execute("INSERT INTO t VALUES (?)", [p])
    return c


def matched(con, base: str, escaped: bool) -> set:
    if escaped:
        pattern = like_literal(base) + SEP + "%"
        sql = "SELECT p FROM t WHERE p LIKE ?" + _ESC
    else:
        pattern = base + SEP + "%"
        sql = "SELECT p FROM t WHERE p LIKE ?"
    return {r[0] for r in con.execute(sql, [pattern]).fetchall()}


def test_underscore_is_matched_literally(rows):
    """A folder called Backup_2024 must not also select BackupX2024's files for deletion."""
    base = "C:" + SEP + "Backup_2024"
    assert matched(rows, base, escaped=True) == {base + SEP + "keep.txt"}


def test_underscore_over_matches_without_escaping(rows):
    """Pins the bug itself, so the fix cannot be quietly reverted."""
    base = "C:" + SEP + "Backup_2024"
    assert len(matched(rows, base, escaped=False)) == 2


def test_percent_is_matched_literally(rows):
    base = "C:" + SEP + "100% Done"
    assert matched(rows, base, escaped=True) == {base + SEP + "keep.txt"}


def test_percent_over_matches_without_escaping(rows):
    """`C:\\100% Done\\%` unescaped also selects `C:\\100 percent Done\\...`, because the literal
    `%` becomes a wildcard. (Not `1000 Done more`, whose separator falls in the wrong place - the
    point is that it matches MORE than the one folder asked for, not that it matches everything.)"""
    base = "C:" + SEP + "100% Done"
    over = matched(rows, base, escaped=False)
    assert len(over) == 2
    assert any("percent" in p for p in over)


def test_backslash_is_not_the_escape_character(rows):
    """Every Windows path is full of backslashes. If backslash were the escape character each
    separator would have to be doubled, and any miss would silently change the match."""
    assert SEP not in _ESC
    assert like_literal("C:" + SEP + "plain") == "C:" + SEP + "plain"


def test_the_escape_character_escapes_itself(rows):
    """A literal `!` in a filename must not eat the character after it."""
    rows.execute("INSERT INTO t VALUES (?)", ["C:" + SEP + "wow!_dir" + SEP + "f.txt"])
    rows.execute("INSERT INTO t VALUES (?)", ["C:" + SEP + "wow!Xdir" + SEP + "f.txt"])
    base = "C:" + SEP + "wow!_dir"
    assert matched(rows, base, escaped=True) == {base + SEP + "f.txt"}


def test_a_plain_search_term_still_matches_substrings(rows):
    """Escaping must not break ordinary search - the surrounding %...% wildcards are ours, and
    only the user's own text is quoted."""
    pattern = "%" + like_literal("Done") + "%"
    found = rows.execute("SELECT p FROM t WHERE p ILIKE ?" + _ESC, [pattern]).fetchall()
    assert len(found) == 3


def test_star_is_the_only_wildcard_in_a_search_term():
    from app.core.textmatch import search_patterns
    assert search_patterns("a*b") == ["%a%b%"]          # the same ASCII form once
    assert len(search_patterns("café")) == 2             # NFC and NFD both match a Mac name
    assert search_patterns("100%_done*") == ["%100!%!_done%%"]
