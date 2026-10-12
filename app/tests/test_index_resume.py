"""A stopped or killed first-index tree read, resumed, must end with exactly the tree one
uninterrupted read builds.

kopia is faked at the subprocess boundary (test_treesync.FakeKopia); the scratch is a real DuckDB
file, closed and reopened the way a new worker process would."""

from __future__ import annotations

import duckdb
import pytest

from app.index import ids, tree, treesync
from app.tests.test_treesync import FakeKopia, _d, _f

ROOTS = ["r1", "r2", "r3"]


def _repo() -> FakeKopia:
    """Three roots, four levels deep, a shared folder, an empty folder, an indirect folder and
    split files - enough folders that a small CHUNK flushes many times per level."""
    dirs: dict = {}
    parts: dict = {}

    def build(oid: str, depth: int) -> None:
        ents = [_f(f"f{depth}.txt", f"c-{oid}", size=depth + 1),
                _f(f"big{depth}.bin", f"Ip-{oid}", size=1000)]
        parts[f"p-{oid}"] = [f"pa-{oid}", f"pb-{oid}"]
        if depth < 4:
            for i in range(3):
                child = f"{oid}.{i}"
                ents.append(_d(f"d{i}", child))
                build(child, depth + 1)
        dirs[oid] = ents

    for r in ROOTS + ["r_new"]:
        build(r, 1)
    dirs["r1"].append(_d("shared", "kshared"))
    dirs["r2"].append(_d("shared-too", "kshared"))
    dirs["kshared"] = [_f("s.txt", "cs")]
    dirs["r3"].append(_d("empty", "kempty"))
    dirs["kempty"] = []
    dirs["r3"].append(_d("indirect", "Ikind"))
    dirs["Ikind"] = [_f("i.txt", "ci")]
    return FakeKopia(dirs, parts)


def _open(path) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE IF NOT EXISTS cid (cid_id INTEGER, content_id VARCHAR)")
    c.execute("CREATE TABLE IF NOT EXISTS cid_rev (content_id VARCHAR, cid_id INTEGER)")
    tree.create_tables(c)
    return c


@pytest.fixture(autouse=True)
def _small_steps(monkeypatch):
    monkeypatch.setattr(treesync, "BATCH", 3)
    monkeypatch.setattr(treesync, "CHUNK", 7)
    monkeypatch.setattr(treesync, "SAVE_EVERY", 0)          # save at every flush
    monkeypatch.setattr(treesync, "kopia_parallel", lambda: 1)


def _canon(con) -> tuple:
    """The finished tree in content ids (cid numbers differ between runs; the tree must not)."""
    treesync.merge(con)
    dirs = sorted(r[0] for r in con.execute(
        "SELECT c.content_id FROM tdir_final d JOIN cid c USING (cid_id)").fetchall())
    ents = sorted(con.execute("""
        SELECT pc.content_id, e.pos, e.name, e.etype, e.mode, e.size, e.mtime,
               ec.content_id, e.ind, ic.content_id, chc.content_id
        FROM tent_final e
        JOIN tdir_final p ON p.dir_id = e.dir_id JOIN cid pc ON pc.cid_id = p.cid_id
        LEFT JOIN cid ec ON ec.cid_id = e.cid_id
        LEFT JOIN cid ic ON ic.cid_id = e.ind_cid
        LEFT JOIN tdir_final ch ON ch.dir_id = e.child LEFT JOIN cid chc ON chc.cid_id = ch.cid_id
    """).fetchall(), key=repr)
    prts = sorted(con.execute("""
        SELECT a.content_id, b.content_id, t.ord FROM tpart_final t
        JOIN cid a ON a.cid_id = t.iobj_cid JOIN cid b ON b.cid_id = t.part_cid""").fetchall())
    return dirs, ents, prts


def _finish(con, alloc) -> tuple:
    treesync.materialize(con, alloc, base_known="tdir")
    return _canon(con)


def _clean(tmp_path, monkeypatch, roots=ROOTS) -> tuple:
    monkeypatch.setattr(treesync, "kopia", _repo())
    con = _open(tmp_path / "clean.duckdb")
    alloc = ids.Allocator({})
    res = treesync.sync(con, "3", roots, alloc=alloc)
    assert res.complete
    out = _finish(con, alloc)
    con.close()
    return out


def _resume_run(path, saved: dict, roots=ROOTS) -> tuple:
    """What a new worker does: reopen the scratch, restore the counters, continue."""
    con = _open(path)
    if not saved:
        alloc = ids.Allocator({})
        treesync.sync(con, "3", roots, alloc=alloc)
    else:
        alloc = ids.Allocator(saved["counters"])
        alloc.at_least("cid_id", con.execute("SELECT coalesce(max(cid_id), 0) FROM cid").fetchone()[0])
        treesync.sync(con, "3", roots, alloc=alloc, resume=saved["state"])
    out = _finish(con, alloc)
    con.close()
    return out


def _saver(con, alloc, saved: dict):
    def save(state):
        con.execute("CHECKPOINT")
        saved.update(state=dict(state), counters=alloc.counters())
    return save


@pytest.mark.parametrize("stop_at", [1, 2, 5, 9, 14, 22, 31, 40, 55])
def test_a_stopped_read_resumes_to_the_same_tree(tmp_path, monkeypatch, stop_at):
    clean = _clean(tmp_path, monkeypatch)
    monkeypatch.setattr(treesync, "kopia", _repo())
    path = tmp_path / "stopped.duckdb"
    con = _open(path)
    alloc = ids.Allocator({})
    saved: dict = {}
    asked = {"n": 0}

    def should_stop() -> bool:
        asked["n"] += 1
        return asked["n"] >= stop_at
    try:
        treesync.sync(con, "3", ROOTS, alloc=alloc, save=_saver(con, alloc, saved),
                      should_stop=should_stop)
        stopped = False
    except treesync.StopRequested:
        stopped = True
    con.close()
    if not stopped:
        pytest.skip("the read finished before this stop point")
    assert _resume_run(path, saved) == clean


class _Killed(BaseException):
    """The process dying: no handler in the code under test runs."""


@pytest.mark.parametrize("die_at", [2, 4, 7, 11, 16, 23, 30, 38])
def test_a_killed_read_resumes_to_the_same_tree(tmp_path, monkeypatch, die_at):
    clean = _clean(tmp_path, monkeypatch)
    fake = _repo()
    calls = {"n": 0}

    def dying(repo, args, **kw):
        calls["n"] += 1
        if calls["n"] == die_at:
            raise _Killed()
        return fake(repo, args, **kw)
    monkeypatch.setattr(treesync, "kopia", dying)
    path = tmp_path / "killed.duckdb"
    con = _open(path)
    alloc = ids.Allocator({})
    saved: dict = {}
    with pytest.raises(_Killed):
        treesync.sync(con, "3", ROOTS, alloc=alloc, save=_saver(con, alloc, saved))
    con.close()
    monkeypatch.setattr(treesync, "kopia", fake)
    assert _resume_run(path, saved) == clean


def test_a_resumed_read_picks_up_a_snapshot_taken_since(tmp_path, monkeypatch):
    clean = _clean(tmp_path, monkeypatch, roots=ROOTS + ["r_new"])
    monkeypatch.setattr(treesync, "kopia", _repo())
    path = tmp_path / "grown.duckdb"
    con = _open(path)
    alloc = ids.Allocator({})
    saved: dict = {}
    asked = {"n": 0}

    def should_stop() -> bool:
        asked["n"] += 1
        return asked["n"] >= 6
    with pytest.raises(treesync.StopRequested):
        treesync.sync(con, "3", ROOTS, alloc=alloc, save=_saver(con, alloc, saved),
                      should_stop=should_stop)
    con.close()
    assert _resume_run(path, saved, roots=ROOTS + ["r_new"]) == clean


def test_an_unreachable_repository_is_waited_out_not_written_off(tmp_path, monkeypatch):
    clean = _clean(tmp_path, monkeypatch)
    fake = _repo()
    state = {"away": 4}           # the next few calls find the repository gone

    def flaky(repo, args, **kw):
        if state["away"] > 0:
            state["away"] -= 1
            return type("R", (), {"stdout": "", "stderr": "dial tcp: i/o timeout", "returncode": 1})()
        return fake(repo, args, **kw)
    monkeypatch.setattr(treesync, "kopia", flaky)
    monkeypatch.setattr(treesync, "_WAIT_START", 0)
    con = _open(tmp_path / "flaky.duckdb")
    alloc = ids.Allocator({})
    res = treesync.sync(con, "3", ROOTS, alloc=alloc, should_stop=lambda: False)
    assert res.complete, f"objects were written off while the repository was away: {res.unreadable}"
    assert _finish(con, alloc) == clean
