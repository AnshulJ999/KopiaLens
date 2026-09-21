"""Two M2 review regressions, both invisible to the parity gate.

`shared_min` counted per-source rows instead of distinct locations, and the folder queries
inherited the search interface's comma-splitting of the source parameter. Parity has no
`shared_min` probe and no real source contains a comma, so only these tests pin them.
"""

from __future__ import annotations

import duckdb
import pytest

from app.index import queries
from app.tests._tree_fixture import add_tree_tables

SEP = chr(92)
COMMA_SRC = "u@h:C:" + SEP + "A, docs"


@pytest.fixture()
def cur(monkeypatch):
    """Two sources overlapping on one path, one genuine duplicate pair, one comma-named source."""
    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.execute("INSERT INTO src VALUES (1, 'u@h:C:'), (2, 'u@h:C:" + SEP + "Users'), (3, ?)",
                [COMMA_SRC])
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    con.execute("INSERT INTO cid VALUES (1, 'AAA'), (2, 'BBB'), (3, 'CCC')")
    con.execute("CREATE TABLE pathx(path_id INTEGER, src_id INTEGER, cid_id INTEGER, "
                "full_path VARCHAR, name VARCHAR, ext VARCHAR, size BIGINT, physical BIGINT, "
                "versions INTEGER, first_seen TIMESTAMP, last_seen TIMESTAMP, "
                "in_latest BOOLEAN, exclusive BIGINT)")
    rows = [
        # ONE location, covered by two overlapping sources -> two rows, one full_path.
        (1, 1, 1, "C:" + SEP + "Users" + SEP + "over.txt", "over.txt", "txt", 10, 5, 1, None, None, True, None),
        (2, 2, 1, "C:" + SEP + "Users" + SEP + "over.txt", "over.txt", "txt", 10, 5, 1, None, None, True, None),
        # TWO locations sharing content -> a genuine duplicate pair.
        (3, 1, 2, "C:" + SEP + "a" + SEP + "dup.txt", "dup.txt", "txt", 20, 7, 1, None, None, True, None),
        (4, 1, 2, "C:" + SEP + "b" + SEP + "dup.txt", "dup.txt", "txt", 20, 7, 1, None, None, True, None),
        # Under the comma-named source.
        # Its own content, so it never counts toward a duplicate group.
        (5, 3, 3, "C:" + SEP + "A, docs" + SEP + "f.txt", "f.txt", "txt", 30, 9, 1, None, None, True, None),
    ]
    con.executemany("INSERT INTO pathx VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # folder scopes resolve through dir/anc, which a generation always carries.
    add_tree_tables(con, [], [(r[1], r[3]) for r in rows])
    # A fresh cursor per call: the query layer closes what read_cursor hands it, so returning the
    # connection itself would leave the second call in a test talking to a closed connection.
    monkeypatch.setattr(queries, "read_cursor", lambda _repo: con.cursor())
    return con


def test_shared_min_counts_locations_not_source_rows(cur):
    """A file covered by two overlapping sources is ONE copy, not two. Counting rows made a
    third of repo1 look duplicated, and this filter feeds the delete plan."""
    paths = {r["full_path"] for r in queries.search_paths("1", shared_min=2)["rows"]}
    assert paths == {"C:" + SEP + "a" + SEP + "dup.txt", "C:" + SEP + "b" + SEP + "dup.txt"}
    assert "C:" + SEP + "Users" + SEP + "over.txt" not in paths


def test_comma_in_a_source_name_still_matches(cur):
    """Folder queries take ONE source; splitting it on commas matched neither half."""
    assert queries.folder_summary("1", "C:" + SEP + "A, docs", source=COMMA_SRC)["files"] == 1
    assert queries.folder_top_children("1", "C:" + SEP + "A, docs", source=COMMA_SRC)[0]["files"] == 1
    assert queries.group_by_folder("1", depth=2, source=COMMA_SRC)["rows"], "no folder rows"


def test_copies_column_counts_locations_not_source_rows(cur):
    """The Copies column promises "this content is at N paths" and pairs with shared_min. A file
    covered by two overlapping sources is at ONE path; how many sources hold it is the Src column."""
    by_path = {r["full_path"]: r for r in queries.search_paths("1")["rows"]}
    over = by_path["C:" + SEP + "Users" + SEP + "over.txt"]
    assert over["copies"] == 1, "one location covered by two sources is not two copies"
    assert over["sources"] == 2, "Src still reports both sources"
    assert by_path["C:" + SEP + "a" + SEP + "dup.txt"]["copies"] == 2, "a real duplicate pair"


def test_search_still_accepts_a_comma_separated_source_list(cur):
    """The multi-source interface keeps its splitting - only the single-source callers changed."""
    got = queries.search_paths("1", source="u@h:C:,u@h:C:" + SEP + "Users")["total"]
    assert got == 4, got
