"""The file panel prices the same path held by nested sources as ONE file.

A drive source and a folder source on it both back up the same file; removing it from both frees
it, and the second source's row is not an "identical copy at another path". Identical content at
a different path still keeps its bytes, and another machine's same path is a different file."""

from __future__ import annotations

import duckdb
import pytest

from app.index import physical

SEP = chr(92)
DRIVE = "me@pc:H:" + SEP
NESTED = "me@pc:H:" + SEP + "Games"
OTHER_PC = "me@laptop:H:" + SEP
FILE = "H:" + SEP + "Games" + SEP + "big.cso"


def _store(monkeypatch, rows: list[tuple[str, str, str, int]], refs: dict[str, int],
           objrefs: dict[str, int] | None = None, chunks: dict[str, set[str]] | None = None):
    """rows: (source, full_path, content_id, snap_no). Plain objects are their own chunk unless
    ``chunks`` maps them to pieces (split objects); ``objrefs`` is the crefo map."""
    objrefs, chunks = objrefs or {}, chunks or {}
    physical._RECLAIM_CACHE.clear()
    con = duckdb.connect()
    sources = sorted({r[0] for r in rows})
    paths = sorted({(r[0], r[1]) for r in rows})
    cids = sorted({r[2] for r in rows})
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.executemany("INSERT INTO src VALUES (?, ?)", [(i + 1, s) for i, s in enumerate(sources)])
    con.execute("CREATE TABLE pathx(path_id INTEGER, src_id INTEGER, full_path VARCHAR)")
    con.executemany("INSERT INTO pathx VALUES (?, ?, ?)",
                    [(i + 1, sources.index(s) + 1, p) for i, (s, p) in enumerate(paths)])
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    con.executemany("INSERT INTO cid VALUES (?, ?)", [(i + 1, c) for i, c in enumerate(cids)])
    con.execute("CREATE TABLE snap(snap_id INTEGER, snapshot_id VARCHAR, start_time TIMESTAMP)")
    con.execute("CREATE TABLE filev(path_id INTEGER, cid_id INTEGER, snap_id INTEGER, size BIGINT)")
    for n, (s, p, c, snap) in enumerate(rows):
        con.execute("INSERT INTO snap VALUES (?, ?, TIMESTAMP '2026-01-01' + INTERVAL (?) DAY)",
                    [n + 1, f"k{n + 1}", snap])
        con.execute("INSERT INTO filev VALUES (?, ?, ?, 100)",
                    [paths.index((s, p)) + 1, cids.index(c) + 1, n + 1])

    class _Cur:
        def execute(self, sql, params=None):
            return con.execute(sql, params or [])

        def close(self):
            pass

        def __getattr__(self, name):
            return getattr(con, name)

    monkeypatch.setattr(physical, "read_cursor", lambda repo: _Cur())
    monkeypatch.setattr(physical, "_sizes_for", lambda repo, cs: {c: 10 for c in cs})
    monkeypatch.setattr(physical, "_refcount_for", lambda repo, cs: {c: refs.get(c, 0) for c in cs})
    monkeypatch.setattr(physical, "_objrefs_for", lambda repo, cs: {c: objrefs[c] for c in cs if c in objrefs})
    monkeypatch.setattr(physical, "_chunks_for_objs",
                        lambda repo, objs, **kw: {o: set(chunks.get(o, {o})) for o in objs if o})


def test_the_same_path_in_nested_sources_is_one_file(monkeypatch):
    _store(monkeypatch, [(DRIVE, FILE, "A", 1), (NESTED, FILE, "A", 2)], refs={"A": 2})
    d = physical.file_detail("1", DRIVE, FILE)
    assert d["holders"] == [DRIVE, NESTED]
    assert d["freeable"] == 10           # used to read 0: the nested source looked like a copy
    assert d["other_paths"] == []
    assert d["instances"] == 1


def test_versions_held_only_by_the_other_holder_count_too(monkeypatch):
    _store(monkeypatch, [(DRIVE, FILE, "A", 1), (NESTED, FILE, "A", 2), (NESTED, FILE, "B", 3)],
           refs={"A": 2, "B": 1})
    d = physical.file_detail("1", DRIVE, FILE)
    assert d["freeable"] == 20
    assert d["physical_all_versions"] == 20


def test_an_identical_copy_at_another_path_keeps_its_bytes(monkeypatch):
    other = "H:" + SEP + "Backup" + SEP + "big.cso"
    _store(monkeypatch, [(DRIVE, FILE, "A", 1), (DRIVE, other, "A", 1)], refs={"A": 1})
    d = physical.file_detail("1", DRIVE, FILE)
    assert d["freeable"] == 0            # the copy at the other path still holds it
    assert d["other_paths"] == [{"source": DRIVE, "full_path": other}]
    assert d["reclaim_everywhere"] == 10


def test_another_machines_same_path_is_a_different_file(monkeypatch):
    _store(monkeypatch, [(DRIVE, FILE, "A", 1), (OTHER_PC, FILE, "A", 2)], refs={"A": 2})
    d = physical.file_detail("1", DRIVE, FILE)
    assert d["holders"] == [DRIVE]
    assert d["freeable"] == 0
    assert d["other_paths"] == [{"source": OTHER_PC, "full_path": FILE}]


def test_many_versions_in_one_source_do_not_outvote_a_second_source(monkeypatch):
    # Chunk A sits in three versions here and in an unrelated source: three references from one
    # source are still one source, so it is not freed.
    _store(monkeypatch, [(DRIVE, FILE, "A", 1), (DRIVE, FILE, "B", 2), (DRIVE, FILE, "A", 3)],
           refs={"A": 2, "B": 1})
    d = physical.file_detail("1", DRIVE, FILE)
    assert d["freeable"] == 10
    assert [c["exclusive"] for c in sorted(d["chunks"], key=lambda r: r["cid"])] == [False]


@pytest.mark.parametrize("missing", [("nobody@pc:C:" + SEP, FILE), (DRIVE, "H:" + SEP + "nope")])
def test_unknown_source_or_path_is_not_found(monkeypatch, missing):
    _store(monkeypatch, [(DRIVE, FILE, "A", 1)], refs={"A": 1})
    assert physical.file_detail("1", *missing) == {"found": False}


def test_a_piece_another_files_version_still_uses_is_not_freed(monkeypatch):
    # [C1] Two different big files (split objects) share piece p in ONE source: crefs says one
    # source, but crefo says two objects, so deleting this file leaves p stored.
    other = "H:" + SEP + "Games" + SEP + "copy-then-edited.cso"
    _store(monkeypatch, [(DRIVE, FILE, "Ia", 1), (DRIVE, other, "Ib", 1)],
           refs={"p": 1, "a": 1, "b": 1}, objrefs={"p": 2, "a": 1, "b": 1},
           chunks={"Ia": {"p", "a"}, "Ib": {"p", "b"}})
    d = physical.file_detail("1", DRIVE, FILE)
    assert d["freeable"] == 10           # only piece a; p stays with the other file
    assert d["physical_all_versions"] == 20


def test_versions_sharing_pieces_free_them_together(monkeypatch):
    # Two versions of one big file share piece p: removing the file (every version) frees it.
    _store(monkeypatch, [(DRIVE, FILE, "Iv1", 1), (DRIVE, FILE, "Iv2", 2)],
           refs={"p": 1, "a": 1, "b": 1}, objrefs={"p": 2, "a": 1, "b": 1},
           chunks={"Iv1": {"p", "a"}, "Iv2": {"p", "b"}})
    assert physical.file_detail("1", DRIVE, FILE)["freeable"] == 30


def test_object_refcount_counts_source_object_pairs_per_piece():
    from app.index import layer2
    con = duckdb.connect()
    con.execute("CREATE TABLE filev(src_id INTEGER, cid_id INTEGER)")
    # source 1 holds split objects 10 and 11 (twice: two versions of a path), source 2 holds 10
    # and a whole file 5 whose bytes equal piece 5
    con.execute("INSERT INTO filev VALUES (1, 10), (1, 10), (1, 11), (2, 10), (2, 5)")
    con.execute("CREATE TABLE tpart(iobj_cid INTEGER, part_cid INTEGER)")
    con.execute("INSERT INTO tpart VALUES (10, 5), (10, 6), (11, 5), (11, 7)")
    layer2.object_refcount(con, "filev", out="m")
    assert dict(con.execute("SELECT cid_id, refs FROM m").fetchall()) == {5: 4, 6: 2, 7: 1}
