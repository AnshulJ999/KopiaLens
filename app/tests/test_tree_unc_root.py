"""UNC sources must group under named \\host\share roots in the All-Sources tree, not
collapse into one blank-named node (split_part returns '' before a leading separator)."""

from __future__ import annotations

import duckdb

from app.index import queries

SEP = chr(92)


def _tree_children_all(repo: str, rows: list[tuple], prefix: str = "") -> list[dict]:
    con = duckdb.connect()
    con.execute("CREATE TABLE paths(repo VARCHAR, source VARCHAR, full_path VARCHAR, size BIGINT, "
                "physical BIGINT, last_seen TIMESTAMP, in_latest BOOLEAN)")
    con.executemany("INSERT INTO paths VALUES (?,?,?,?,?,?,?)", rows)
    con.execute("CREATE TABLE content_refs(repo VARCHAR, cid VARCHAR, refs BIGINT)")  # _search_where refs queries
    orig = queries.read_cursor
    monkey_cursor = con
    queries.read_cursor = lambda _repo: monkey_cursor
    try:
        return queries.tree_children_all(repo, prefix)
    finally:
        queries.read_cursor = orig


def _rows():
    return [
        ("1", "C:/x", "C:" + SEP + "Users" + SEP + "a.txt", 10, 5, None, True),
        ("1", "U:/h", SEP * 2 + "srv1" + SEP + "media" + SEP + "v1.mkv", 30, 9, None, True),
        ("1", "U:/h", SEP * 2 + "srv1" + SEP + "media" + SEP + "sub" + SEP + "v2.mkv", 40, 9, None, True),
        ("1", "U:/h2", SEP * 2 + "other" + SEP + "stuff" + SEP + "c.doc", 50, 9, None, True),
    ]


def test_unc_roots_are_named_and_grouped_per_share():
    out = _tree_children_all("1", _rows())
    names = {r["name"]: r for r in out}
    assert r"\\srv1\media" in names, names
    assert r"\\other\stuff" in names
    assert "" not in names, "the blank UNC node must be gone"
    assert names[r"\\srv1\media"]["files"] == 2
    assert names["C:"]["files"] == 1


def test_drilling_into_a_unc_root_keeps_working():
    out = _tree_children_all("1", _rows(), prefix=SEP * 2 + "srv1" + SEP + "media")
    names = {r["name"] for r in out}
    assert names == {"v1.mkv", "sub"}, names
