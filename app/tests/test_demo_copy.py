"""Demo copies: names faked everywhere, numbers untouched, the copy opens as a normal index."""

from __future__ import annotations

import json
import os

import duckdb
import pytest

from app.core import demo
from app.index import store
from app.index.upgrade import MANIFEST_VERSION

BS = chr(92)
WIN = f"anshul@secret-pc:C:{BS}Clients{BS}Acme Corp"
LNX = "anshul@secret-pc:/home/anshul/Priya"
PRIVATE = ("anshul", "secret-pc", "clients", "acme", "priya", "sharma", "passport", "payroll",
           "zebra")

DIRS = [  # dir_id, src_id, dir_path, depth, name, parent_id
    (1, 1, "C:", 1, "C:", None),
    (2, 1, f"C:{BS}Clients", 2, "Clients", 1),
    (3, 1, f"C:{BS}Clients{BS}Acme Corp", 3, "Acme Corp", 2),
    (4, 2, "", 1, "", None),
    (5, 2, "/home", 2, "home", 4),
    (6, 2, "/home/anshul", 3, "anshul", 5),
    (7, 2, "/home/anshul/Priya", 4, "Priya", 6),
]
FILES = [  # dir_id, name, size
    (3, "Priya Sharma Passport.pdf", 1000), (3, "Payroll 2023.xlsx", 2000),
    (3, "zebra_parser.py", 10), (3, "zebra_models.py", 11), (3, "zebra_routes.py", 12),
    (3, "zebra_utils.py", 13), (3, "zebra_views.py", 14), (3, "zebra_forms.py", 15),
    (7, "Zebra.txt", 5), (7, "zebra.txt", 6), (7, "notes.md", 7),
]


def _write(root: str) -> None:
    con = duckdb.connect()
    pool = os.path.join(root, "pool")
    os.makedirs(pool)
    ids = {d[0]: d for d in DIRS}

    def ancestors(d):
        out, cur, depth = [], d, 0
        while cur is not None:
            out.append((d, depth, cur))
            cur, depth = ids[cur][5], depth + 1
        return out

    con.execute("CREATE TABLE src AS SELECT * FROM (VALUES (1::SMALLINT, ?), (2::SMALLINT, ?)) "
                "t(src_id, source)", [WIN, LNX])
    con.execute("CREATE TABLE dir (dir_id INTEGER, src_id INTEGER, dir_path VARCHAR, "
                "depth SMALLINT, name VARCHAR, parent_id INTEGER)")
    con.executemany("INSERT INTO dir VALUES (?, ?, ?, ?, ?, ?)", DIRS)
    con.execute("CREATE TABLE anc (dir_id INTEGER, depth INTEGER, anc_id INTEGER)")
    con.executemany("INSERT INTO anc VALUES (?, ?, ?)", [a for d in ids for a in ancestors(d)])
    con.execute("CREATE TABLE pathx (path_id INTEGER, src_id INTEGER, dir_id INTEGER, name VARCHAR, "
                "ext VARCHAR, size UBIGINT, full_path VARCHAR)")
    for i, (d, name, size) in enumerate(FILES, 1):
        dpath, sep = ids[d][2], (BS if ids[d][1] == 1 else "/")
        con.execute("INSERT INTO pathx VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [i, ids[d][1], d, name, name.rsplit(".", 1)[-1], size, dpath + sep + name])
    con.execute("""CREATE TABLE tent (dir_id INTEGER, pos INTEGER, name VARCHAR, etype TINYINT,
                   size UBIGINT, s_errors VARCHAR, extra VARCHAR)""")
    tree = [(10, n, 0, s) for d, n, s in FILES if d == 3] + [(11, n, 0, s) for d, n, s in FILES if d == 7]
    tree.append((12, "Acme Corp", 1, 0))
    for pos, (d, n, t, s) in enumerate(sorted(tree)):
        con.execute("INSERT INTO tent VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [d, pos, n, t, s, f"C:{BS}Clients{BS}Acme Corp: denied" if t else None,
                     json.dumps({"name": n}) if n == "notes.md" else None])
    con.execute("CREATE TABLE isnaps AS SELECT * FROM (VALUES ('7', 'snapA', ?, 3), "
                "('7', 'snapB', ?, 3)) t(repo, snapshot_id, source, file_count)", [WIN, LNX])
    for table in ("src", "dir", "anc", "pathx", "tent", "isnaps"):
        con.execute(f"COPY {table} TO '{os.path.join(pool, table)}.parquet' (FORMAT parquet)")
    con.close()
    manifest = {"generation": 3, "repo": "7", "schema_version": MANIFEST_VERSION,
                "encrypted": False, "counters": {}, "stats": {"paths": len(FILES)},
                "sources": {WIN: {"key": WIN, "snapshots": 1}, LNX: {"key": LNX, "snapshots": 1}},
                "tree_incomplete": [LNX], "tree_forced_for": [],
                "artifacts": {t: [f"pool/{t}.parquet"] for t in
                              ("src", "dir", "anc", "pathx", "tent", "isnaps")}}
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


@pytest.fixture
def live(tmp_path, monkeypatch):
    data = tmp_path / "data"
    root = data / "repo7"
    root.mkdir(parents=True)
    _write(str(root))
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    monkeypatch.setattr(demo, "DATA_DIR", str(data))
    return data


def _open_copy(path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", path)
    gen = store.open_generation("1")
    return gen, gen.cursor()


@pytest.mark.parametrize("style", ["generic", "codename"])
def test_copy_fakes_every_name_and_keeps_numbers(live, monkeypatch, style):
    result = demo.make_copy("7", style)
    assert result["rows"]["pathx"] == len(FILES)
    copy_root = result["path"]
    assert not os.path.exists(os.path.join(copy_root, "repo1", "pool", "..", "..", "name.key"))

    blob = ""
    for f in os.listdir(os.path.join(copy_root, "repo1", "pool")):
        con = duckdb.connect()
        rows = con.execute(f"SELECT * FROM read_parquet('{os.path.join(copy_root, 'repo1', 'pool', f)}')").fetchall()
        blob += repr(rows)
        con.close()
    with open(os.path.join(copy_root, "repo1", "manifest.json"), encoding="utf-8") as fh:
        blob += fh.read()
    for word in PRIVATE:
        assert word not in blob.lower(), word

    gen, cur = _open_copy(copy_root, monkeypatch)
    try:
        assert cur.execute("SELECT sum(size) FROM pathx").fetchone()[0] == sum(s for _, _, s in FILES)
        # Paths are rebuilt from the faked folder path plus the faked file name.
        for full, dpath, name, src in cur.execute(
                "SELECT p.full_path, d.dir_path, p.name, p.src_id FROM pathx p JOIN dir d USING (dir_id)").fetchall():
            sep = BS if src == 1 else "/"
            assert full == dpath + sep + name
        assert cur.execute("SELECT dir_path FROM dir WHERE dir_id = 1").fetchone()[0] == "C:"
        # Siblings stay unique, case twins included, and the Tree agrees with Search.
        assert cur.execute("SELECT count(*) FROM (SELECT dir_id FROM pathx GROUP BY dir_id, "
                           "lower(name) HAVING count(*) > 1)").fetchone()[0] == 0
        assert set(r[0] for r in cur.execute("SELECT name FROM tent WHERE etype = 0").fetchall()) == \
               set(r[0] for r in cur.execute("SELECT name FROM pathx").fetchall())
        tree = cur.execute("SELECT dir_id, pos, name, s_errors, extra FROM tent "
                           "ORDER BY dir_id, pos").fetchall()
        assert all(r[3] is None and r[4] is None for r in tree)
        for d in {r[0] for r in tree}:
            names = [r[2] for r in tree if r[0] == d]
            assert names == sorted(names)
        assert {r[0] for r in cur.execute("SELECT repo FROM isnaps").fetchall()} == {"1"}
    finally:
        gen.close()


def test_same_key_gives_the_same_copy(live):
    first = demo.make_copy("7", "generic")
    con = duckdb.connect()
    a = con.execute(f"SELECT full_path FROM read_parquet('{first['path']}/repo1/pool/pathx.parquet') ORDER BY path_id").fetchall()
    second = demo.make_copy("7", "generic")
    b = con.execute(f"SELECT full_path FROM read_parquet('{second['path']}/repo1/pool/pathx.parquet') ORDER BY path_id").fetchall()
    assert a == b
