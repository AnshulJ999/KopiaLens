"""Query-layer tests against a synthetic in-memory SQLite (no live bzdata needed).

Covers the query functions that the live harness only smoke-tested: search filters/sort/paging,
overview, group_by_folder, dupes, exclusions impact, and (critically for Phase 3) coverage_children.

Run with pytest or directly (`python app/backblaze/tests/test_queries.py`).
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from app.backblaze import bbdb, queries  # noqa: E402


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


def _db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    bbdb._init_schema(con)
    con.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", FILES)
    bbdb.rebuild_fts(con)
    con.execute("INSERT INTO queue VALUES('E', 'E:\\Photos\\new.jpg', 123)")
    con.commit()
    bbdb._CONN = con  # make queries.* use this connection
    return con


def test_search_substring_and_filters():
    _db()
    # substring (FTS trigram or LIKE fallback) - 'movie' matches both dir1/dir2
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


def test_search_sort_and_paging():
    _db()
    res = queries.search(sort="size", direction="desc", limit=2, offset=0)
    sizes = [r["size"] for r in res["rows"]]
    assert sizes == [5000, 5000]               # two biggest first
    assert res["total"] == 8                    # total ignores limit
    res2 = queries.search(sort="size", direction="asc", limit=1)
    assert res2["rows"][0]["size"] == 50        # smallest first


def test_search_short_query_like_fallback():
    _db()
    # 2-char query falls back to LIKE scan (below trigram's 3-char minimum).
    # "sy" only matches C:\Windows\sys.dll (unambiguous - not inside any other path).
    res = queries.search(query="sy")
    assert res["total"] == 1 and res["rows"][0]["name"] == "sys.dll"


def test_overview_counts():
    _db()
    rows = {r["drive"]: r for r in queries.overview()["rows"]}
    assert rows["C"]["total"] == 4
    assert rows["C"]["backed_files"] == 2
    assert rows["C"]["excluded_files"] == 2
    assert rows["E"]["backed_files"] == 4
    assert rows["E"]["backed_bytes"] == 100 + 1000 + 5000 + 5000


def test_group_by_folder():
    _db()
    rows = queries.group_by_folder(drive="E", depth=1, status="backed")
    folders = {r["folder"]: r for r in rows}
    assert folders[r"E:\Photos"]["files"] == 2
    assert folders[r"E:\Photos"]["bytes"] == 1100
    assert folders[r"E:\dir1"]["files"] == 1


def test_dupes_heuristic():
    _db()
    d = queries.dupes(min_size=1000)
    hit = [x for x in d if x["name"] == "movie.mkv"]
    assert hit and hit[0]["copies"] == 2 and hit[0]["wasted"] == 10000


def test_exclusions_impact():
    _db()
    imp = {i["rule"]: i for i in queries.exclusions()["impact"]}
    assert imp["mandatory:\\windows\\"]["bytes"] == 200
    assert imp["folder:\\games\\"]["files"] == 1


def test_coverage_children():
    _db()
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


def test_queue():
    _db()
    q = queries.queue()
    assert q["total_files"] == 1 and q["total_bytes"] == 123
    assert q["by_drive"][0]["drive"] == "E"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} query tests passed.")


if __name__ == "__main__":
    _run_all()
