"""Linux (POSIX) sources beside Windows ones: folder structure comes from the folder table, never
from cutting paths, so a backslash inside a Linux name is just a character.

The fixture mirrors the build: one ``dir`` row per folder prefix of each path, split by the
SOURCE's own separator (the POSIX root and a UNC share's leading levels are empty names), plus the
``anc`` closure. The names are the Linux test bed's awkward ones
.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unicodedata

import duckdb
import pytest

from app.core import paths as pathrules
from app.index import excluded, queries

B = chr(92)
LNX = "tester@linux:/data/src"
WIN = "me@pc:C:" + B
UNC = "me@pc:" + B * 2 + "srv" + B + "share"

FILES = [
    (LNX, "/data/src/back" + B + "slash dir/inner.txt", 10),
    (LNX, "/data/src/docs/weird" + B + "name.txt", 20),
    (LNX, "/data/src/docs/C:" + B + "fake" + B + "path.txt", 30),
    (LNX, "/data/src/docs/dir" + B + "with" + B + "slashes/x.txt", 40),
    (LNX, "/data/src/docs/trail" + B + "/leaf.txt", 50),
    (LNX, "/data/src/case/file.txt", 1),
    (LNX, "/data/src/Case/File.txt", 2),
    (LNX, "/data/src/CASE/FILE.TXT", 4),
    (LNX, "/data/src/docs/" + unicodedata.normalize("NFD", "café") + ".txt", 5),
    (WIN, "C:" + B + "Users" + B + "a.txt", 100),
    (WIN, "C:" + B + "users" + B + "b.txt", 200),
    (UNC, B * 2 + "srv" + B + "share" + B + "v.mkv", 300),
]


@pytest.fixture
def gen(monkeypatch):
    con = duckdb.connect()
    con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
    con.execute("CREATE TABLE dir(dir_id INTEGER, src_id INTEGER, dir_path VARCHAR, depth SMALLINT, "
                "name VARCHAR, parent_id INTEGER)")
    con.execute("CREATE TABLE anc(dir_id INTEGER, depth SMALLINT, anc_id INTEGER)")
    con.execute("CREATE TABLE pathx(path_id INTEGER, src_id INTEGER, dir_id INTEGER, name VARCHAR, "
                "ext VARCHAR, size UBIGINT, versions INTEGER, first_seen TIMESTAMP, "
                "last_seen TIMESTAMP, in_latest BOOLEAN, cid_id INTEGER, physical UBIGINT, "
                "exclusive UBIGINT, full_path VARCHAR)")
    con.execute("CREATE TABLE cid(cid_id INTEGER, content_id VARCHAR)")
    srcs: dict[str, int] = {}
    dirs: dict[tuple[int, str], int] = {}
    for pid, (source, full_path, size) in enumerate(FILES, 1):
        sid = srcs.get(source)
        if sid is None:
            sid = srcs[source] = len(srcs) + 1
            con.execute("INSERT INTO src VALUES (?, ?)", [sid, source])
        sep = pathrules.sep(source)
        parts = full_path.split(sep)
        chain: list[int] = []
        for k in range(1, len(parts)):
            key = (sid, sep.join(parts[:k]))
            if key not in dirs:
                dirs[key] = len(dirs) + 1
                con.execute("INSERT INTO dir VALUES (?, ?, ?, ?, ?, ?)",
                            [dirs[key], sid, key[1], k, parts[k - 1], chain[-1] if chain else None])
                for depth, a in enumerate(chain + [dirs[key]], 1):
                    con.execute("INSERT INTO anc VALUES (?, ?, ?)", [dirs[key], depth, a])
            chain.append(dirs[key])
        con.execute("INSERT INTO pathx VALUES (?, ?, ?, ?, NULL, ?, 1, NULL, NULL, true, NULL, ?, NULL, ?)",
                    [pid, sid, chain[-1], parts[-1], size, size, full_path])
    monkeypatch.setattr(queries, "read_cursor", lambda _repo: con.cursor())
    return con


def _by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in rows}


def test_linux_folders_come_from_the_folder_table_not_from_backslashes(gen):
    top = _by_name(queries.tree_children_paths("t", LNX, "/data/src"))
    assert set(top) == {"back" + B + "slash dir", "docs", "case", "Case", "CASE"}
    assert all(r["type"] == "folder" for r in top.values())
    assert top["back" + B + "slash dir"]["path"] == "/data/src/back" + B + "slash dir"
    docs = _by_name(queries.tree_children_paths("t", LNX, "/data/src/docs"))
    assert docs["weird" + B + "name.txt"]["type"] == "file"
    assert docs["C:" + B + "fake" + B + "path.txt"]["type"] == "file"
    assert docs["dir" + B + "with" + B + "slashes"]["type"] == "folder"
    assert docs["weird" + B + "name.txt"]["path"] == "/data/src/docs/weird" + B + "name.txt"


def test_a_folder_name_ending_in_a_backslash_opens(gen):
    docs = _by_name(queries.tree_children_paths("t", LNX, "/data/src/docs"))
    trail = docs["trail" + B]
    assert trail["type"] == "folder" and trail["path"] == "/data/src/docs/trail" + B
    inside = _by_name(queries.tree_children_paths("t", LNX, trail["path"]))
    assert set(inside) == {"leaf.txt"}


def test_case_twin_folders_stay_apart_in_folder_scopes_and_delete_plans(gen):
    """A delete of Linux ``case/`` must not take ``Case/`` and ``CASE/`` with it (T7)."""
    assert queries.folder_summary("t", "/data/src/case")["files"] == 1
    plan = queries.remove_targets_summary("t", prefix="/data/src/case")
    assert plan["total_file_count"] == 1 and plan["total_logical"] == 1
    # a Windows folder still matches every spelling of its name
    assert queries.folder_summary("t", "c:" + B + "USERS")["files"] == 2


def test_all_sources_tree_has_one_root_per_kind_and_drills_into_each(gen):
    top = _by_name(queries.tree_children_all("t", ""))
    assert set(top) == {"/", "C:", B * 2 + "srv" + B + "share"}
    assert top["/"]["files"] == 9
    assert _by_name(queries.tree_children_all("t", "/"))["data"]["path"] == "/data"
    share = _by_name(queries.tree_children_all("t", top[B * 2 + "srv" + B + "share"]["path"]))
    assert set(share) == {"v.mkv"}


def test_group_by_folder_uses_each_source_s_own_folders(gen):
    rows = {r["folder"]: r for r in queries.group_by_folder("t", depth=4, limit=100)["rows"]}
    assert "/data/src/docs" in rows and "/data/src/back" + B + "slash dir" in rows
    assert "" not in rows


def test_search_finds_a_name_in_either_unicode_form(gen):
    hit = queries.search_paths("t", query=unicodedata.normalize("NFC", "café"))
    assert hit["total"] == 1


def test_excluded_coverage_follows_the_folder_s_own_rules(gen, monkeypatch):
    monkeypatch.setattr(excluded, "coverage_repos", lambda: ["t"])
    monkeypatch.setattr(excluded, "read_cursor", lambda _repo: gen.cursor())
    kids = excluded.kopia_backed_children("/data/src")
    assert {"case", "Case", "CASE", "docs", "back" + B + "slash dir"} <= set(kids)
    assert kids["case"]["files"] == 1
    win = excluded.kopia_backed_children("C:" + B)
    assert win["users"]["files"] == 2          # Windows folds case


# ---- names that are rule or wildcard syntax ------------------------------------------------

AWKWARD = ["[brackets].txt", "  spaces around  ", "back" + B + "slash", "!bang.txt", "#hash",
           "star*.txt", "what?.txt"]


@pytest.mark.parametrize("name", AWKWARD)
def test_exclusion_rules_match_exactly_the_named_file(name):
    from app.core.ignorematch import compile_pattern
    from app.core.policytargets import _pattern_for_shape
    pat = _pattern_for_shape("exact", "docs/" + name, False)
    assert compile_pattern(pat).match("/docs/" + name, is_dir=False), pat
    assert not compile_pattern(pat).match("/docs/" + name + "x", is_dir=False)
    named = _pattern_for_shape("name", "docs/" + name, False)
    assert compile_pattern(named).match("/a/b/" + name, is_dir=False), named


def test_similar_rule_keeps_wildcards_only_where_digits_were():
    from app.core.ignorematch import compile_pattern
    from app.core.policytargets import similar_leaf
    pat = similar_leaf("report[1].txt")
    assert compile_pattern(pat).match("/report[22].txt", is_dir=False)
    assert not compile_pattern(pat).match("/report5.txt", is_dir=False)


def test_filename_values_escape_kopia_wildcards():
    from app.core.kopia import filename_arg
    assert filename_arg("a[1]*?.txt") == "a" + B + "[1" + B + "]" + B + "*" + B + "?.txt"
    assert filename_arg("weird" + B + "name") == "weird" + B * 2 + "name"
    assert filename_arg("plain name.txt") == "plain name.txt"


# ---- generated scripts ---------------------------------------------------------------------

SCRIPT_NAMES = ["pipe|name.txt", "q<lt>gt.txt", "caret^name.txt", "amp&semi;.txt",
                'say "hi".txt', "100%.txt", "ends" + B, "paren (1).txt", "plain.txt"]


@pytest.mark.skipif(os.name != "nt", reason="runs the generated .bat through cmd.exe")
def test_bat_passes_every_name_to_the_program_intact(tmp_path):
    from app.core.kopia import _bat_line
    echo = [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))"]
    line = _bat_line(echo + SCRIPT_NAMES)
    bat = tmp_path / "t.bat"
    bat.write_text("@echo off\r\n" + line + "\r\n", encoding="utf-8")
    res = subprocess.run(["cmd", "/c", str(bat)], capture_output=True, text=True, timeout=60)
    import json
    assert json.loads(res.stdout.strip().splitlines()[-1]) == SCRIPT_NAMES, res.stdout + res.stderr


def test_bat_leaves_out_names_with_a_line_break_and_says_so():
    from app.core.kopia import remove_files_bat_text
    text = remove_files_bat_text([["kopia", "--filename", "a\nb", "--filename", "ok.txt"]], "r.log")
    assert "ok.txt" in text and "a\nb" not in text
    assert "1 name contains a line break" in text


def test_sh_script_quotes_every_name():
    import shlex
    from app.core.kopia import _remove_sh_lines
    cmd = ["kopia", "snapshot", "fix", "remove-files"] + [x for n in SCRIPT_NAMES + ["a\nb"]
                                                          for x in ("--filename", n)]
    lines = "\n".join(_remove_sh_lines([cmd], "r.log"))
    run = next(seg for seg in lines.split("\n" + "RC=$?")[0].split("\n") if seg.startswith("kopia"))
    full = lines[lines.index(run):lines.index('"$SLICE" 2>&1')].rstrip(" >")
    assert shlex.split(full) == cmd


@pytest.mark.skipif(os.name == "nt", reason="runs the generated .sh through /bin/sh")
def test_sh_script_passes_every_name_to_the_program_intact(tmp_path):
    import json
    from app.core.kopia import _remove_sh_lines
    names = SCRIPT_NAMES + ["a\nb", "$HOME `x` 'q'.txt"]
    out = tmp_path / "argv.json"
    record = [sys.executable, "-c", f"import json, sys; open({str(out)!r}, 'w').write(json.dumps(sys.argv[1:]))"]
    script = tmp_path / "r.sh"
    script.write_text("\n".join(_remove_sh_lines([record + names], "r.log")) + "\n", encoding="utf-8")
    res = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == names


# ---- restore onto this computer --------------------------------------------------------------

def test_restore_flags_names_this_computer_cannot_create():
    from app.api.restore import _bad_here
    if os.name == "nt":
        for bad in ("a:b.txt", "q<lt>gt.txt", "what?.txt", "CON.txt", "trail ", "x" + B + "y"):
            assert _bad_here(bad), bad
    assert not _bad_here("normal file.txt")


# ---- guard: no separator cutting comes back -------------------------------------------------

_BANNED = ("chr(92)", '.split("' + B * 2 + '")', '.rsplit("' + B * 2 + '"', 'rstrip("' + B * 2 + '/")',
           'strip("' + B * 2 + '/")', "_SEP =")
_ALLOWED = {os.path.join("api", "jobs.py")}     # a restore target on THIS computer


def test_index_and_api_never_cut_paths_on_a_fixed_separator():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    hits = []
    for sub in ("index", "api"):
        for name in os.listdir(os.path.join(root, sub)):
            rel = os.path.join(sub, name)
            if not name.endswith(".py") or rel in _ALLOWED:
                continue
            text = open(os.path.join(root, rel), encoding="utf-8").read()
            hits += [f"{rel}: {b}" for b in _BANNED if b in text]
    assert not hits, "separator and case rules come from core.paths per source: " + "; ".join(hits)
