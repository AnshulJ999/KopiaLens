"""Demo copies: names faked everywhere, numbers untouched, the copy opens as a normal index."""

from __future__ import annotations

import json
import os

import duckdb
import pytest

from app.core import demo
from app.core.kopia import kopia as REAL_KOPIA
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
    (7, "budget.secret-pc", 8),   # the computer name as an "extension" (sync-conflict copies)
]


TABLES = ("src", "dir", "anc", "pathx", "tent", "isnaps", "snap", "cid", "filev")


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
                "ext VARCHAR, size UBIGINT, full_path VARCHAR, cid_id INTEGER)")
    for i, (d, name, size) in enumerate(FILES, 1):
        dpath, sep = ids[d][2], (BS if ids[d][1] == 1 else "/")
        con.execute("INSERT INTO pathx VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [i, ids[d][1], d, name, name.rsplit(".", 1)[-1], size, dpath + sep + name,
                     1 if i == 1 else 2])
    con.execute("""CREATE TABLE tent (dir_id INTEGER, pos INTEGER, name VARCHAR, etype TINYINT,
                   size UBIGINT, s_errors VARCHAR, extra VARCHAR)""")
    tree = [(10, n, 0, s) for d, n, s in FILES if d == 3] + [(11, n, 0, s) for d, n, s in FILES if d == 7]
    tree.append((12, "Acme Corp", 1, 0))
    for pos, (d, n, t, s) in enumerate(sorted(tree)):
        con.execute("INSERT INTO tent VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [d, pos, n, t, s, f"C:{BS}Clients{BS}Acme Corp: denied" if t else None,
                     json.dumps({"name": n}) if n == "notes.md" else None])
    con.execute("CREATE TABLE isnaps AS SELECT * FROM (VALUES "
                "('7', 'snapA', ?, TIMESTAMP '2026-01-01 10:00:00', 'kRootA', 3), "
                "('7', 'snapB', ?, TIMESTAMP '2026-01-02 10:00:00', 'kRootB', 3)) "
                "t(repo, snapshot_id, source, start_time, root_obj, file_count)", [WIN, LNX])
    con.execute("CREATE TABLE snap AS SELECT * FROM (VALUES (1, 'snapA', TIMESTAMP '2026-01-01 10:00:00', "
                "1::SMALLINT), (2, 'snapB', TIMESTAMP '2026-01-02 10:00:00', 2::SMALLINT)) "
                "t(snap_id, snapshot_id, start_time, src_id)")
    con.execute("CREATE TABLE cid AS SELECT * FROM (VALUES (1, 'c1'), (2, 'c2')) t(cid_id, content_id)")
    # Every file is in its source's one snapshot; the passport file's content is c1.
    con.execute("CREATE TABLE filev (src_id BIGINT, path_id INTEGER, snap_id INTEGER, cid_id INTEGER, "
                "size UBIGINT)")
    for i, (d, name, size) in enumerate(FILES, 1):
        src = ids[d][1]
        con.execute("INSERT INTO filev VALUES (?, ?, ?, ?, ?)", [src, i, src, 1 if i == 1 else 2, size])
    for table in TABLES:
        con.execute(f"COPY {table} TO '{os.path.join(pool, table)}.parquet' (FORMAT parquet)")
    con.close()
    manifest = {"generation": 3, "repo": "7", "schema_version": MANIFEST_VERSION,
                "encrypted": False, "counters": {}, "stats": {"paths": len(FILES)},
                "sources": {WIN: {"key": WIN, "snapshots": 1}, LNX: {"key": LNX, "snapshots": 1}},
                "tree_incomplete": [LNX], "tree_forced_for": [],
                "artifacts": {t: [f"pool/{t}.parquet"] for t in TABLES}}
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


ACME = f"C:{BS}Clients{BS}Acme Corp"
REPLIES = {  # what kopia says about the synthetic repo; private names in every kind of field
    "snapshot list": [{"id": "snapA", "source": {"host": "secret-pc", "userName": "anshul", "path": ACME},
                       "description": "Payroll for Priya", "tags": {"tag:client": "acme"},
                       "rootEntry": {"name": "Acme Corp", "obj": "kRootA",
                                     "summ": {"errors": [{"path": ACME + BS + "x", "error": "denied"}]}}}],
    "policy export": {f"anshul@secret-pc:{ACME}": {"files": {"ignore": ["/Payroll 2023.xlsx", "*.tmp", "Priya*"]},
                                                  "actions": {"beforeSnapshotRoot": {"script": "C:/Users/anshul/priya.bat"}}},
                      "(global)": {"files": {"ignore": ["*.iso"], "ignoreDotFiles": [".kopiaignore"]}}},
    "policy show": {"files": {"ignore": ["/Acme Corp/"]}},
    "repository status": {"configFile": f"C:{BS}Users{BS}anshul{BS}repository.config",
                          "storage": {"type": "filesystem", "config": {"path": f"E:{BS}Acme Backups"}},
                          "clientOptions": {"hostname": "secret-pc", "username": "anshul",
                                            "description": f"Repository in Filesystem: E:{BS}Acme Backups"},
                          "uniqueIDHex": "ab" * 16, "volume": {"capacity": 100, "available": 40}},
    "maintenance info": {"owner": "anshul@secret-pc",
                         "runs": {"full": [{"success": False, "error": ACME + " is locked"}]}},
    "cache info": f"C:{BS}Users{BS}anshul{BS}AppData{BS}Local{BS}kopia{BS}abc{BS}contents: 3 files",
}


def _fake_kopia(repo, args, ro=True, timeout=None):
    from app.core.kopia import KopiaResult
    words = [a for a in args if not a.startswith("-")]
    key = " ".join(words[:2])
    if key == "policy export":
        with open(args[args.index("--to-file") + 1], "w", encoding="utf-8") as fh:
            json.dump(REPLIES[key], fh)
        out = ""
    elif key in REPLIES:
        out = REPLIES[key] if isinstance(REPLIES[key], str) else json.dumps(REPLIES[key])
    else:
        out = "Count: 3" if key in ("content stats", "blob stats") else ""
    return KopiaResult(args=args, returncode=0, stdout=out, stderr="", elapsed=0.0)


@pytest.fixture
def live(tmp_path, monkeypatch):
    from app.core import kopia as kopia_mod
    data = tmp_path / "data"
    root = data / "repo7"
    root.mkdir(parents=True)
    _write(str(root))
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    monkeypatch.setattr(demo, "DATA_DIR", str(data))
    monkeypatch.setattr(demo, "CACHEDIR", str(data / "cache"))
    monkeypatch.setattr(kopia_mod, "kopia", _fake_kopia)
    return data


def _folder_text(root: str) -> str:
    """Every byte of a copy as text, parquet rows included."""
    blob = ""
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            path = os.path.join(dirpath, f)
            if f.endswith(".parquet"):
                con = duckdb.connect()
                blob += repr(con.execute(f"SELECT * FROM read_parquet('{path}')").fetchall())
                con.close()
            else:
                with open(path, encoding="utf-8") as fh:
                    blob += fh.read()
    return blob.lower()


def _open_copy(path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", path)
    gen = store.open_generation("1")
    return gen, gen.cursor()


@pytest.mark.parametrize("style", ["generic", "codename"])
def test_copy_fakes_every_name_and_keeps_numbers(live, monkeypatch, style):
    result = demo.make_copy("7", style, finish=False)
    assert result["rows"]["pathx"] == len(FILES)
    copy_root = result["path"]
    assert not os.path.exists(os.path.join(copy_root, "name.key"))

    # Index, recorded kopia replies, repo record: no private word anywhere.
    blob = _folder_text(copy_root)
    for word in PRIVATE:
        assert word not in blob, word
    with open(os.path.join(copy_root, "demo_replies_repo1.json"), encoding="utf-8") as fh:
        replies = json.load(fh)
    export = json.loads(replies[json.dumps(["policy", "export"])]["out"])

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
        # A policy names the same source and file the index shows, so rules still match.
        payroll = cur.execute("SELECT name FROM pathx WHERE size = 2000").fetchone()[0]
        win = cur.execute("SELECT source FROM src WHERE src_id = 1").fetchone()[0]
        assert export[win]["files"]["ignore"][:2] == ["/" + payroll, "*.tmp"]
        assert "actions" not in export[win]
    finally:
        gen.close()


def test_leak_check_reports_a_real_name_and_the_copy_still_finishes(live, monkeypatch):
    # Content stats are passed through unfaked (numbers only), so a path there is a real leak.
    monkeypatch.setitem(REPLIES, "content stats", f"Count: 3 in {ACME}")
    r = demo.make_copy("7", "codename", finish=False)
    assert r["leak_warnings"]["file_hits"] >= 1
    assert r["leak_warnings"]["files"] == ["demo_replies_repo1.json"]
    assert os.path.isdir(demo.copy_dir("7", "codename"))


def test_same_key_gives_the_same_copy(live):
    first = demo.make_copy("7", "generic", finish=False)
    con = duckdb.connect()
    a = con.execute(f"SELECT full_path FROM read_parquet('{first['path']}/repo1/pool/pathx.parquet') ORDER BY path_id").fetchall()
    second = demo.make_copy("7", "generic", finish=False)
    b = con.execute(f"SELECT full_path FROM read_parquet('{second['path']}/repo1/pool/pathx.parquet') ORDER BY path_id").fetchall()
    assert a == b


def test_two_repos_make_one_demo_holding_both(live, monkeypatch):
    _write(str(live / "repo8"))
    result = demo.make_copy("7,8", "generic", finish=False)
    copy_root = result["path"]
    assert os.path.basename(copy_root) == "repo7+8-generic"
    assert result["rows"]["8"]["pathx"] == len(FILES)
    with open(os.path.join(copy_root, "repos.json"), encoding="utf-8") as fh:
        records = json.load(fh)["repos"]
    assert [(r["id"], r["name"], r.get("demo"), r["kind"]) for r in records] == [
        ("1", "Demo 1", True, "kopia"), ("2", "Demo 2", True, "kopia"), ("bz", "Backblaze", None, "backblaze")]
    blob = _folder_text(copy_root)
    for word in PRIVATE:
        assert word not in blob, word
    for demo_id in ("1", "2"):
        assert os.path.isfile(os.path.join(copy_root, f"demo_replies_repo{demo_id}.json"))
        monkeypatch.setattr(store, "DATA_DIR", copy_root)
        gen = store.open_generation(demo_id)
        try:
            cur = gen.cursor()
            assert cur.execute("SELECT count(*) FROM pathx").fetchone()[0] == len(FILES)
            assert {r[0] for r in cur.execute("SELECT repo FROM isnaps").fetchall()} == {demo_id}
        finally:
            gen.close()


@pytest.fixture
def demo_repo(live, monkeypatch):
    """A finished copy served as repo "1" (a demo repo), with every process launch failing."""
    import config
    from app.core import demoreplies, demosim
    from app.core import kopia as kopia_mod
    copy_root = demo.make_copy("7", "codename", finish=False)["path"]
    monkeypatch.setitem(config.REPOS, "1", {"name": "Demo", "args": [], "kind": "kopia", "demo": True})
    monkeypatch.setattr(store, "DATA_DIR", copy_root)
    monkeypatch.setattr(demoreplies, "DATA_DIR", copy_root)
    monkeypatch.setattr(demoreplies, "_CACHE", {})
    monkeypatch.setattr(demosim.time, "sleep", lambda _s: None)
    monkeypatch.setattr(kopia_mod, "kopia", REAL_KOPIA)   # the fake was only for making the copy

    def no_spawn(*_a, **_k):
        raise AssertionError("a demo repo started a process")
    monkeypatch.setattr(kopia_mod.subprocess, "run", no_spawn)
    monkeypatch.setattr(kopia_mod.subprocess, "Popen", no_spawn)
    return copy_root


def test_demo_repo_answers_from_recordings_and_never_runs_kopia(demo_repo, tmp_path):
    from app.core import kopia as k
    from app.core.demoreplies import DISPLAY_CONFIG, UNAVAILABLE
    snaps = json.loads(k.kopia("1", ["snapshot", "list", "--json"]).stdout)
    assert snaps and "anshul" not in json.dumps(snaps).lower()
    target = tmp_path / "export.json"
    assert k.kopia("1", ["policy", "export", "--to-file", str(target), "--overwrite"]).returncode == 0
    assert json.loads(target.read_text(encoding="utf-8"))
    missing = k.kopia("1", ["diff", "snapA", "snapB"])
    assert (missing.returncode, missing.stderr) == (1, UNAVAILABLE)
    with pytest.raises(RuntimeError):
        list(k.kopia_lines("1", ["content", "list", "-l"]))
    assert k._repo_args("1") == ["--config-file", DISPLAY_CONFIG]
    with pytest.raises(PermissionError):
        k.snapshot_delete_execute("1", ["snapA"])
    with pytest.raises(PermissionError):
        k.restore_execute("1", "kRootA", str(tmp_path / "out"))
    with pytest.raises(k.MountError):
        k.mount_snapshot("1", "snapA", str(tmp_path / "m"))
    assert k._run_policy_write("1", k.policy_delete_command("1", global_=True), "policy-delete", {})["returncode"] == 0


def test_simulated_runs_follow_the_index(demo_repo):
    from app.core import demosim
    from app.core import kopia as k
    dry = k.snapshot_delete_dry_run("1", ["snapA", "snapB", "unknown"])
    assert (dry["returncode"], dry["snapshots"]) == (0, 2)
    # Content c2 is every file but the passport, across both sources' snapshots.
    removed = k.remove_files_dry_run("1", object_ids=["c2"])
    assert (removed["entries"], removed["snapshots"]) == (len(FILES) - 1, 2)
    result: dict = {}
    lines = [t for _s, t in k.kopia_job_stream("1", ["restore", "kRootA", "D:/out"], result)]
    assert result["returncode"] == 0 and lines[-1] == demosim.DONE_NOTE
    assert "Restored 8 files" in lines[-2]


def test_demo_replies_never_show_this_pcs_folders():
    import config
    from app.core import demoreplies
    body = json.dumps({"dir": os.path.join(config.DATA_DIR, "policy-backups", "repo1"),
                       "home": os.path.join(os.path.expanduser("~"), "notes"),
                       "app": config.HERE.replace(BS, "/"),
                       # an error message carries the path escaped once already
                       "detail": str(FileNotFoundError(2, "missing", os.path.join(config.DATA_DIR, "app.log")))}).encode()
    out = demoreplies.scrub(body).decode().lower()
    assert not any(m in out for m in demoreplies.machine_paths())
    assert json.loads(out)["dir"].startswith(demoreplies._DISPLAY[0][1].lower())
