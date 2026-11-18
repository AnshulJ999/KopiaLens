"""Query-layer tests against a synthetic store (no live bzdata needed).

Covers the query functions that the live harness only smoke-tested: search filters/sort/paging,
overview, group_by_folder, dupes, exclusions impact, and (critically for Phase 3) coverage_children.
Every test runs against BOTH `files` stores: the SQLite table and the Parquet generation
store - the small-table (queue etc.) reads always go through the injected SQLite connection.

Run with pytest or directly (`python app/backblaze/tests/test_queries.py`).
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import pytest  # noqa: E402

from app.backblaze import bbdb, pq, queries  # noqa: E402

STORES = ["sqlite", "parquet"]


def _row(drive, full_path, size, excluded, rule=None):
    plc = full_path.lower()
    name = full_path.rsplit("\\", 1)[-1]
    base = name.lower()
    dot = base.rfind(".")
    ext = base[dot + 1:] if dot > 0 else ""
    return (drive, full_path, plc, name, ext, size, "0", 1 if excluded else 0, rule)


FILES = [
    _row("C", r"C:\Users\me\photo.jpg", 100, False),
    _row("C", r"C:\Users\me\doc.txt", 50, False),
    _row("C", r"C:\Windows\sys.dll", 200, True, "mandatory:\\windows\\"),
    _row("C", r"C:\Games\g.exe", 300, True, "folder:\\games\\"),
    _row("E", r"E:\Photos\a.jpg", 100, False),
    _row("E", r"E:\Photos\big.mp4", 1000, False),
    _row("E", r"E:\dir1\movie.mkv", 5000, False),
    _row("E", r"E:\dir2\movie.mkv", 5000, False),   # same name+size as dir1 -> dup candidate
]


def _pq_publish_initial(tmp_path):
    """Build generation 1 of the Parquet store from FILES (grouped per drive)."""
    by_drive: dict = {}
    for row in FILES:
        by_drive.setdefault(row[0], []).append(row)
    m = pq.read_manifest()
    gen = int((m or {}).get("generation") or 0) + 1
    rels, moves = {}, []
    for drive, rows in by_drive.items():
        staged = pq.staged_path(drive, gen)
        writer = pq.open_writer(staged)
        try:
            writer.write_table(pq.table_from_rows(rows))
        finally:
            writer.close()
        rel = pq.pool_rel(drive, gen)
        moves.append((staged, rel))
        rels[drive] = rel
    with pq.WriterLock():
        pq.publish(gen, rels, moves)


@pytest.fixture(autouse=True)
def _reset_store_overrides():
    yield
    queries._STORE_OVERRIDE = None
    pq.ROOT = None


def _db(store, tmp_path=None):
    """In-memory SQLite for the small tables (always the bbdb side) + the `files` rows in the
    requested store."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    bbdb._init_schema(con)
    if store == "sqlite":
        con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", FILES)
        bbdb.rebuild_fts(con)
    else:
        pq.ROOT = str(tmp_path / "bz")
        _pq_publish_initial(tmp_path)
    con.execute("INSERT INTO queue VALUES('E', 'E:\\Photos\\new.jpg', 123)")
    con.commit()
    bbdb._local.con = con  # queries.* small-table reads (and the sqlite files store) use this
    queries._STORE_OVERRIDE = store
    return con


@pytest.mark.parametrize("store", STORES)
def test_search_substring_and_filters(store, tmp_path):
    _db(store, tmp_path)
    # substring (FTS trigram, or the LIKE scan on parquet) - 'movie' matches both dir1/dir2
    assert queries.search(query="movie")["total"] == 2
    # drive filter
    assert queries.search(drive="C")["total"] == 4
    # status filter
    assert queries.search(status="excluded")["total"] == 2
    assert queries.search(status="backed")["total"] == 6
    # ext filter
    assert queries.search(ext="jpg")["total"] == 2
    # min_size
    assert queries.search(min_size=1000)["total"] == 3  # big.mp4 + 2x movie.mkv


@pytest.mark.parametrize("store", STORES)
def test_search_sort_and_paging(store, tmp_path):
    _db(store, tmp_path)
    res = queries.search(sort="size", direction="desc", limit=2, offset=0)
    sizes = [r["size"] for r in res["rows"]]
    assert sizes == [5000, 5000]               # two biggest first
    assert res["total"] == 8                    # total ignores limit
    res2 = queries.search(sort="size", direction="asc", limit=1)
    assert res2["rows"][0]["size"] == 50        # smallest first


@pytest.mark.parametrize("store", STORES)
def test_search_short_query_like_fallback(store, tmp_path):
    _db(store, tmp_path)
    # 2-char query falls back to a LIKE scan (below trigram's 3-char minimum).
    # "sy" only matches C:\Windows\sys.dll (unambiguous - not inside any other path).
    res = queries.search(query="sy")
    assert res["total"] == 1 and res["rows"][0]["name"] == "sys.dll"


@pytest.mark.parametrize("store", STORES)
def test_overview_counts(store, tmp_path):
    _db(store, tmp_path)
    rows = {r["drive"]: r for r in queries.overview()["rows"]}
    assert rows["C"]["total"] == 4
    assert rows["C"]["backed_files"] == 2
    assert rows["C"]["excluded_files"] == 2
    assert rows["E"]["backed_files"] == 4
    assert rows["E"]["backed_bytes"] == 100 + 1000 + 5000 + 5000


@pytest.mark.parametrize("store", STORES)
def test_group_by_folder(store, tmp_path):
    _db(store, tmp_path)
    rows = queries.group_by_folder(drive="E", depth=1, status="backed")
    folders = {r["folder"]: r for r in rows}
    assert folders[r"E:\Photos"]["files"] == 2
    assert folders[r"E:\Photos"]["bytes"] == 1100
    assert folders[r"E:\dir1"]["files"] == 1


@pytest.mark.parametrize("store", STORES)
def test_dupes_heuristic(store, tmp_path):
    _db(store, tmp_path)
    d = queries.dupes(min_size=1000)
    hit = [x for x in d if x["name"] == "movie.mkv"]
    assert hit and hit[0]["copies"] == 2 and hit[0]["wasted"] == 10000


@pytest.mark.parametrize("store", STORES)
def test_exclusions_impact(store, tmp_path):
    _db(store, tmp_path)
    imp = {i["rule"]: i for i in queries.exclusions()["impact"]}
    assert imp["mandatory:\\windows\\"]["bytes"] == 200
    assert imp["folder:\\games\\"]["files"] == 1


@pytest.mark.parametrize("store", STORES)
def test_coverage_children(store, tmp_path):
    _db(store, tmp_path)
    # under C:\Users\me -> two backed files
    cov = queries.coverage_children(r"C:\Users\me")
    assert set(cov) == {"photo.jpg", "doc.txt"}
    assert cov["photo.jpg"]["has_file"] is True
    assert cov["doc.txt"]["logical"] == 50
    # under C:\ -> only 'users' (windows/games are excluded -> absent from backed coverage)
    top = queries.coverage_children("C:\\")
    assert "users" in top
    assert "windows" not in top and "games" not in top
    assert top["users"]["files"] == 2 and top["users"]["logical"] == 150


@pytest.mark.parametrize("store", STORES)
def test_queue(store, tmp_path):
    _db(store, tmp_path)
    q = queries.queue()
    assert q["total_files"] == 1 and q["total_bytes"] == 123
    assert q["by_drive"][0]["drive"] == "E"


@pytest.mark.parametrize("store", STORES)
def test_folder_children_live_scan(store, tmp_path):
    _db(store, tmp_path)
    # no folder_rollup rows -> the live range-scan path runs (store-dependent)
    children = queries.folder_children(r"C:\Users\me")
    by_name = {c["name"]: c for c in children}
    assert set(by_name) == {"photo.jpg", "doc.txt"}
    assert by_name["photo.jpg"]["backed_bytes"] == 100
    assert by_name["photo.jpg"]["is_dir"] is False


def _run_all():
    import pathlib
    import tempfile
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for store in STORES:
        tmp_path = pathlib.Path(tempfile.mkdtemp(prefix="bz3_q_"))
        for fn in fns:
            fn(store, tmp_path)
            print(f"ok  {fn.__name__} [{store}]")
    print(f"\n{len(fns) * len(STORES)} query tests passed.")


if __name__ == "__main__":
    _run_all()


@pytest.mark.parametrize("store", STORES)
def test_search_is_literal_and_star_is_the_wildcard(store, tmp_path):
    """A typed `_` or `%` is a character; only `*` widens the match."""
    _db(store, tmp_path)
    assert queries.search(query="_")["total"] == 0
    assert queries.search(query="%")["total"] == 0
    assert queries.search(query="*")["total"] == 8
    assert queries.search(query="movie*.mkv")["total"] == 2
    assert queries.search(query="PHOTO.JPG")["total"] == 1


@pytest.mark.parametrize("store", STORES)
def test_overview_marks_the_queue_per_drive(store, tmp_path):
    con = _db(store, tmp_path)
    con.execute("INSERT INTO queue VALUES('C', ?, 10)", [r"C:\Users\me\new.txt"])
    con.commit()
    rows = {r["drive"]: r for r in queries.overview()["rows"]}
    assert rows["E"]["queue_files"] == 1
    assert rows["C"]["queue_files"] == 1
