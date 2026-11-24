"""Step 2 of: the tree sync, with kopia faked at the subprocess boundary.

The live proof that the tree equals the old walker runs against a real repository; these pin the
rules that a live run on healthy data would never exercise - a batch that aborts halfway, an
empty folder, an unknown entry type, a split object whose piece list could not be read.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from app.index import ids, tree, treesync


class FakeKopia:
    """Answers ``content show`` / ``show`` from a dict of object -> entry list.

    ``broken`` names objects kopia refuses: the batch prints answers up to that point and exits
    non-zero, exactly as the real CLI does.
    """

    def __init__(self, dirs: dict, parts: dict | None = None, broken: set | None = None):
        self.dirs = dirs
        self.parts = parts or {}
        self.broken = broken or set()
        self.calls = 0

    def __call__(self, repo, args, **kw):
        self.calls += 1
        out, rc, err = [], 0, ""
        if args[:2] == ["content", "show"]:
            for oid in args[2:]:
                if oid.startswith("I"):   # kopia refuses an object id here, as the real CLI does
                    rc, err = 1, "error parsing content ID: invalid content hash"
                    break
                key = oid if oid in self.dirs or oid in self.parts else oid
                if key in self.broken:
                    rc, err = 1, "content not found"
                    break
                if key in self.parts:
                    out.append(json.dumps({"entries": [{"o": p} for p in self.parts[key]]}))
                else:
                    out.append(json.dumps({"entries": self.dirs.get(key, [])}))
        elif args[0] == "show":
            oid = args[1]
            if oid in self.broken:
                rc, err = 1, "not found"
            else:
                out.append(json.dumps({"entries": self.dirs.get(oid, [])}))
        return type("R", (), {"stdout": "\n".join(out), "stderr": err, "returncode": rc})()


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    c.execute("CREATE TABLE cid_rev (content_id VARCHAR, cid_id INTEGER)")
    tree.create_tables(c)
    yield c
    c.close()


def _d(name, obj, **kw):
    return {"name": name, "type": "d", "mode": "0777", "obj": obj, **kw}


def _f(name, obj, size=1, **kw):
    return {"name": name, "type": "f", "mode": "0666", "obj": obj, "size": size, **kw}


def run(con, fake, roots, monkeypatch, known=None):
    monkeypatch.setattr(treesync, "kopia", fake)
    res = treesync.sync(con, "3", roots, known_table=known)
    stats = treesync.materialize(con, ids.Allocator({}))
    merged = treesync.merge(con)
    return res, stats, merged


def test_a_simple_tree_lands_with_every_folder_and_entry(con, monkeypatch):
    fake = FakeKopia({
        "root": [_d("sub", "ksub"), _f("a.txt", "c1", size=10)],
        "ksub": [_f("b.txt", "c2", size=20)],
    })
    res, stats, merged = run(con, fake, ["root"], monkeypatch)
    assert res.complete and res.levels == 2
    assert stats["folders"] == 2 and stats["entries"] == 3
    assert merged["folders"] == 2 and merged["entries"] == 3
    names = [r[0] for r in con.execute("SELECT name FROM tent_final ORDER BY name").fetchall()]
    assert names == ["a.txt", "b.txt", "sub"]


def test_an_empty_folder_is_still_a_folder(con, monkeypatch):
    # The bug this pins: building tdir from the ENTRIES drops every childless directory, which
    # then looks unknown and is re-fetched from kopia on every run.
    fake = FakeKopia({"root": [_d("empty", "kempty")], "kempty": []})
    _res, stats, merged = run(con, fake, ["root"], monkeypatch)
    assert stats["folders"] == 2, "the empty folder was dropped"
    assert merged["folders"] == 2
    lo, hi = con.execute("""SELECT row_lo, row_hi FROM tdir_final d JOIN cid c USING (cid_id)
                            WHERE c.content_id = 'kempty'""").fetchone()
    assert lo == hi                       # a real row, occupying no entry rows


def test_a_shared_folder_object_is_stored_once(con, monkeypatch):
    # Kopia stores identical directories ONCE, so the same object sits at several paths. The
    # TREE is keyed by object; paths are layer 3's problem.
    fake = FakeKopia({
        "root": [_d("x", "kshared"), _d("y", "kshared")],
        "kshared": [_f("f", "c1")],
    })
    _res, stats, _m = run(con, fake, ["root"], monkeypatch)
    assert stats["folders"] == 2          # root + the shared object, not three
    assert con.execute("SELECT count(*) FROM tent_final WHERE child IS NOT NULL").fetchone()[0] == 2


def _stored(con):
    return {r[0] for r in con.execute(
        "SELECT c.content_id FROM tdir_final d JOIN cid c USING (cid_id)").fetchall()}


def test_an_aborted_batch_never_records_the_tail_as_empty(con, monkeypatch):
    # content show aborts at the first unreadable object and exits non-zero. The objects after it
    # got NO answer - recording them as empty folders is how index rows silently go missing.
    fake = FakeKopia({"root": [_d("a", "ka"), _d("b", "kb"), _d("c", "kc")],
                      "ka": [_f("1", "c1")], "kb": [], "kc": [_f("3", "c3")]},
                     broken={"kb"})
    res, stats, _m = run(con, fake, ["root"], monkeypatch)
    assert not res.complete
    for missed in res.unreadable:
        assert missed not in _stored(con), "an unreadable folder was written into the tree"


def test_a_dead_object_does_not_take_the_batch_behind_it_down(con, monkeypatch):
    # The tail got no answer, but it is not unreadable - only the object the batch stopped
    # at is. Writing the tail off is permanent: the ids are fetched in a stable order, so every
    # later run aborts in the same place and those folders are never priced.
    fake = FakeKopia({"root": [_d("a", "ka"), _d("b", "kb"), _d("c", "kc")],
                      "ka": [_f("1", "c1")], "kb": [], "kc": [_f("3", "c3")]},
                     broken={"kb"})
    res, _s, _m = run(con, fake, ["root"], monkeypatch)
    assert res.unreadable == ["kb"]
    assert {"ka", "kc"} <= _stored(con) and "kb" not in _stored(con)


def test_every_dead_object_in_one_batch_is_skipped_in_turn(con, monkeypatch):
    fake = FakeKopia({"root": [_d("a", "ka"), _d("b", "kb"), _d("c", "kc"), _d("d", "kd")],
                      "ka": [_f("1", "c1")], "kb": [], "kc": [], "kd": [_f("4", "c4")]},
                     broken={"kb", "kc"})
    res, _s, _m = run(con, fake, ["root"], monkeypatch)
    assert sorted(res.unreadable) == ["kb", "kc"]
    assert {"ka", "kd"} <= _stored(con)


def test_a_wholesale_kopia_failure_stops_at_the_cap(con, monkeypatch):
    # Nothing answers, which from `answered` alone is indistinguishable from a dead first object.
    # Without the cap that is one kopia call per object in the batch.
    monkeypatch.setattr(treesync, "MAX_SKIPS", 2)
    kids = [_d(f"k{i}", f"k{i}") for i in range(6)]
    fake = FakeKopia({"root": kids, **{f"k{i}": [] for i in range(6)}},
                     broken={f"k{i}" for i in range(6)})
    res, _s, _m = run(con, fake, ["root"], monkeypatch)
    assert len(res.unreadable) == 6          # the 2 it stepped over plus the 4 it gave up on
    assert fake.calls <= 4                   # the root, 2 attempts, one reachability check - not one per object


def test_a_dead_split_object_does_not_drop_the_next_one_s_pieces(con, monkeypatch):
    fake = FakeKopia({"root": [_f("a.bin", "Ixa", size=9), _f("b.bin", "Ixb", size=9)]},
                     parts={"xb": ["p1", "p2"]}, broken={"xa"})
    res, _s, _m = run(con, fake, ["root"], monkeypatch)
    assert res.unreadable == ["Ixa"]
    got = [r[0] for r in con.execute("""SELECT pc.content_id FROM tpart_final p
        JOIN cid pc ON pc.cid_id = p.part_cid ORDER BY p.ord""").fetchall()]
    assert got == ["xb", "p1", "p2"], "the surviving split object lost its piece list"


def test_an_incomplete_sync_is_reported_not_swallowed(con, monkeypatch):
    fake = FakeKopia({"root": [_d("a", "ka")], "ka": []}, broken={"ka"})
    res, _s, _m = run(con, fake, ["root"], monkeypatch)
    assert res.complete is False
    assert res.as_dict()["unreadable"] == 1


def test_indirect_directories_go_through_kopia_show(con, monkeypatch):
    fake = FakeKopia({"root": [_d("big", "Ikbig")], "Ikbig": [_f("f", "c1")]})
    res, stats, _m = run(con, fake, ["root"], monkeypatch)
    assert res.complete and stats["folders"] == 2
    assert con.execute("""SELECT ind FROM tdir_final d JOIN cid c USING (cid_id)
                          WHERE c.content_id = 'Ikbig'""").fetchone()[0] is True


def test_a_split_file_keeps_its_index_object_and_every_piece(con, monkeypatch):
    # Ix... is a split object: strip the leading I, content show the rest for the piece list, and
    # the index object itself counts as a piece (ordinal 0).
    fake = FakeKopia({"root": [_f("big.bin", "Ixidx", size=99)]},
                     parts={"xidx": ["p1", "p2", "p3"]})
    res, stats, _m = run(con, fake, ["root"], monkeypatch)
    assert res.complete and stats["parts"] == 4
    got = [r[0] for r in con.execute("""SELECT pc.content_id FROM tpart_final p
        JOIN cid pc ON pc.cid_id = p.part_cid ORDER BY p.ord""").fetchall()]
    assert got == ["xidx", "p1", "p2", "p3"]


def test_a_file_split_twice_is_read_level_by_level(con, monkeypatch):
    # A piece list big enough to be split again gets a second I. The top content lists the index
    # pieces; `kopia show` with one I fewer lists the data pieces (shapes from kopia 0.23.1).
    fake = FakeKopia({"root": [_f("huge.bin", "IIxtop", size=99)],
                      "Ixtop": [{"o": "p1"}, {"o": "p2"}]},
                     parts={"xtop": ["xi1", "xi2"]})
    res, _s, _m = run(con, fake, ["root"], monkeypatch)
    assert res.complete and res.unreadable == []
    got = [r[0] for r in con.execute("""SELECT pc.content_id FROM tpart_final p
        JOIN cid pc ON pc.cid_id = p.part_cid ORDER BY p.ord""").fetchall()]
    assert got == ["xtop", "xi1", "xi2", "p1", "p2"]


def test_linux_permissions_are_stored_compactly(con, monkeypatch):
    fake = FakeKopia({"root": [_f("a", "c1", mode="0644"), _d("b", "kb", mode="0755")], "kb": []})
    _res, stats, _m = run(con, fake, ["root"], monkeypatch)
    assert stats["unknown_mode"] == 0 and stats["kept_raw"] == 0


def test_an_unreadable_piece_list_is_dropped_not_half_stored(con, monkeypatch):
    # A piece list of only the index object would undercount that file's physical size forever,
    # so a split object we could not resolve must leave NO rows at all.
    fake = FakeKopia({"root": [_f("big.bin", "Ixidx", size=99)]}, parts={}, broken={"xidx"})
    res, _s, _m = run(con, fake, ["root"], monkeypatch)
    assert not res.complete
    assert con.execute("SELECT count(*) FROM tpart_final").fetchone()[0] == 0


def test_an_unknown_entry_type_is_kept_verbatim(con, monkeypatch):
    fake = FakeKopia({"root": [{"name": "weird", "type": "q", "obj": "c1", "wat": 5}]})
    _res, stats, _m = run(con, fake, ["root"], monkeypatch)
    assert stats["unknown_type"] == 1 and stats["kept_raw"] == 1
    raw = con.execute("SELECT extra FROM tent_final WHERE name = 'weird'").fetchone()[0]
    assert json.loads(raw)["wat"] == 5    # nothing lost, even for a shape we do not model


def test_timestamps_round_trip_to_the_exact_string_kopia_wrote(con, monkeypatch):
    stamps = ["2024-02-25T11:56:36.4531752Z", "2026-07-22T02:09:36.722781Z",
              "2026-01-01T00:00:00Z", "2023-03-03T03:03:03.123456789Z"]
    fake = FakeKopia({"root": [_f(f"f{i}", f"c{i}", mtime=s) for i, s in enumerate(stamps)]})
    run(con, fake, ["root"], monkeypatch)
    got = [r[0] for r in con.execute(
        f"SELECT {tree.ns_to_ts('mtime')} FROM tent_final ORDER BY name").fetchall()]
    assert got == stamps


def test_a_known_object_is_never_re_fetched(con, monkeypatch):
    fake = FakeKopia({"root": [_d("sub", "ksub")], "ksub": [_f("b", "c2")]})
    con.execute("CREATE TABLE _known (oid VARCHAR)")
    con.execute("INSERT INTO _known VALUES ('ksub')")
    res, _s, _m = run(con, fake, ["root"], monkeypatch, known="_known")
    assert res.fetched == 1               # root only; the known subtree was not re-read


def test_the_merge_drops_folders_nothing_reaches(con, monkeypatch):
    fake = FakeKopia({"root": [_d("keep", "kkeep")], "kkeep": [], "kgone": []})
    monkeypatch.setattr(treesync, "kopia", fake)
    treesync.sync(con, "3", ["root", "kgone"])
    treesync.materialize(con, ids.Allocator({}))
    con.execute("""CREATE TABLE _reach AS SELECT cid_id FROM cid
                   WHERE content_id IN ('root', 'kkeep')""")
    merged = treesync.merge(con, reachable="_reach")
    assert merged["folders"] == 2 and merged["dropped"] == 1
    left = {r[0] for r in con.execute(
        "SELECT c.content_id FROM tdir_final d JOIN cid c USING (cid_id)").fetchall()}
    assert left == {"root", "kkeep"}


def test_row_ranges_are_contiguous_and_cover_every_entry(con, monkeypatch):
    fake = FakeKopia({"root": [_d("a", "ka"), _d("b", "kb")],
                      "ka": [_f(f"f{i}", f"c{i}") for i in range(5)],
                      "kb": [_f("z", "cz")]})
    _res, _s, merged = run(con, fake, ["root"], monkeypatch)
    rows = con.execute("SELECT dir_id, row_lo, row_hi FROM tdir_final ORDER BY dir_id").fetchall()
    assert rows[0][1] == 0
    for (_i, lo, hi), (_j, nlo, _nhi) in zip(rows, rows[1:]):
        assert lo <= hi and hi == nlo     # contiguous, in dir_id order
    assert rows[-1][2] == merged["entries"]
    for dir_id, lo, hi in rows:
        n = con.execute("SELECT count(*) FROM tent_final WHERE dir_id = ?", [dir_id]).fetchone()[0]
        assert hi - lo == n


def _fingerprint(con):
    """The tree as content, independent of the dir_id numbering a merge happens to assign."""
    ent = con.execute("""SELECT c.content_id, e.pos, e.name, e.etype, e.size,
               oc.content_id, ch.content_id
        FROM tent_final e JOIN tdir_final d ON d.dir_id = e.dir_id
        JOIN cid c ON c.cid_id = d.cid_id
        LEFT JOIN cid oc ON oc.cid_id = e.cid_id
        LEFT JOIN tdir_final cd ON cd.dir_id = e.child
        LEFT JOIN cid ch ON ch.cid_id = cd.cid_id
        ORDER BY 1, 2""").fetchall()
    parts = con.execute("""SELECT ic.content_id, pc.content_id, p.ord FROM tpart_final p
        JOIN cid ic ON ic.cid_id = p.iobj_cid JOIN cid pc ON pc.cid_id = p.part_cid
        ORDER BY 1, 3""").fetchall()
    dirs = sorted(r[0] for r in con.execute(
        "SELECT c.content_id FROM tdir_final d JOIN cid c USING (cid_id)").fetchall())
    return ent, parts, dirs


def test_a_segmented_build_produces_the_same_tree_as_one_pass(con, monkeypatch, tmp_path):
    """Memory safety must not cost correctness.

    A segmented build materializes and clears as it goes, so a folder's children are often not
    known when its own rows are written - which is exactly why `child` is resolved at merge time.
    This pins that the two routes agree.
    """
    import duckdb as _duckdb
    dirs = {"root": [_d(f"d{i}", f"k{i}") for i in range(6)] + [_f("top.txt", "ctop")]}
    for i in range(6):
        dirs[f"k{i}"] = [_f(f"f{i}.txt", f"c{i}", size=i), _d("deep", f"kd{i}")]
        dirs[f"kd{i}"] = [_f("big.bin", "Ixidx")] if i == 0 else []
    fake = FakeKopia(dirs, {"xidx": ["p1", "p2"]})

    monkeypatch.setattr(treesync, "kopia", fake)
    treesync.sync(con, "3", ["root"])
    treesync.materialize(con, ids.Allocator({}))
    treesync.merge(con)
    one_pass = _fingerprint(con)

    seg = _duckdb.connect()
    seg.execute("CREATE TABLE cid (cid_id INTEGER, content_id VARCHAR)")
    seg.execute("CREATE TABLE cid_rev (content_id VARCHAR, cid_id INTEGER)")
    tree.create_tables(seg)
    # alloc= makes sync materialize after EVERY level, so most folders are written before the
    # folders they point at even exist.
    treesync.sync(seg, "3", ["root"], alloc=ids.Allocator({}))
    treesync.merge(seg)
    segmented = _fingerprint(seg)
    seg.close()

    assert segmented[2] == one_pass[2], "different folder sets"
    assert segmented[0] == one_pass[0], "different entries"
    assert segmented[1] == one_pass[1], "different piece lists"


def test_a_segment_flush_clears_its_raw_buffer(con, monkeypatch):
    fake = FakeKopia({"root": [_d("a", "ka")], "ka": [_f("x", "c1")]})
    monkeypatch.setattr(treesync, "kopia", fake)
    treesync.sync(con, "3", ["root"], alloc=ids.Allocator({}))
    assert con.execute("SELECT count(*) FROM _sync_ent").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM _tent_new").fetchone()[0] == 2


def test_object_ids_are_shared_with_the_generation_dictionary(con, monkeypatch):
    fake = FakeKopia({"root": [_f("a", "cshared")], })
    run(con, fake, ["root"], monkeypatch)
    cid_of = dict(con.execute("SELECT content_id, cid_id FROM cid").fetchall())
    ent = con.execute("SELECT cid_id FROM tent_final WHERE name = 'a'").fetchone()[0]
    assert ent == cid_of["cshared"]       # the same id space layer 3 resolves against
