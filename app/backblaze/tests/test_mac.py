"""Backblaze data from a Mac, data from another computer, and Backblaze's version.

The Mac rule lines are copied word for word from Backblaze's own bzexcluderules_mandatory.xml
(Windows installs ship the Mac rules too); file list names and mounts follow Backblaze's Mac layout.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import pytest  # noqa: E402

from app.backblaze import bbdb, indexer, parsers, paths, pq, queries  # noqa: E402

PAD = ("# " + "x" * 200 + "\n") * 8   # over the stub threshold
MAC_RULES = """<bzexclusions>
<excludefname_rule plat="mac" osVers="*"  ruleIsOptional="f" skipFirstCharThenStartsWith="private/var/tmp/" contains_1="*" contains_2="*" doesNotContain="*" endsWith="*" hasFileExtension="*" />
<excludefname_rule plat="mac" osVers="*"  ruleIsOptional="f" skipFirstCharThenStartsWith=".bzvol/" contains_1="*" contains_2="*" doesNotContain="*" endsWith="*" hasFileExtension="*" />
<excludefname_rule plat="mac" osVers="*"  ruleIsOptional="f" skipFirstCharThenStartsWith=".spotlight-v100/" contains_1="*" contains_2="*" doesNotContain="*" endsWith="*" hasFileExtension="*" />
<excludefname_rule plat="mac" osVers="*"  ruleIsOptional="t" skipFirstCharThenStartsWith="users/" contains_1="/library/" contains_2="/saved application state/" doesNotContain="*" endsWith="*" hasFileExtension="*" />
<excludefname_rule plat="mac" osVers="10.5"  ruleIsOptional="f" skipFirstCharThenStartsWith="users/" contains_1="*" contains_2="*" doesNotContain="*" endsWith="*" hasFileExtension="*" />
<excludefname_rule plat="win" osVers="*"  ruleIsOptional="f" skipFirstCharThenStartsWith="*" contains_1="report" contains_2="*" doesNotContain="*" endsWith="*" hasFileExtension="*" />
</bzexclusions>
"""


def _hex(s: str) -> str:
    return s.encode("utf-8").hex()


def _write(root: str, rel: str, text: str) -> None:
    fp = os.path.join(root, rel)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(text)


def _filelist(lines: list[tuple[int, str]]) -> str:
    return "# GmtMillisThisListWasStarted 1\n" + "".join(
        f"f\t{'0' * 16}\t{size}\t{p}\n" for size, p in lines) + PAD


def _mac_bzdata(root: str, boot: list[tuple[int, str]]) -> None:
    _write(root, "bzfilelists/v0013aaaa_root_filelist.dat", _filelist(boot))
    # an external drive whose list is volume-relative: the mount gets prefixed
    _write(root, "bzfilelists/v0017bbbb_Seagate_filelist.dat", _filelist([
        (300, "/Photos/a.jpg"), (5, "/.bzvol/bzvol.xml"), (7, "/.Spotlight-V100/store.db")]))
    _write(root, "bzvolumes.xml", (
        f'<bzvolumes><bzvolume bzVolumeGuid="v0013aaaa" mountPointPathHex="{_hex("/")}" '
        'numBytesFreeOnVolume="1" numBytesUsedOnVolume="2" numBytesTotalOnVolume="3"/>'
        f'<bzvolume bzVolumeGuid="v0017bbbb" mountPointPathHex="{_hex("/Volumes/Seagate HD")}" '
        'numBytesFreeOnVolume="1" numBytesUsedOnVolume="2" numBytesTotalOnVolume="3"/></bzvolumes>'))
    _write(root, "bzexcluderules_mandatory.xml", MAC_RULES)
    _write(root, "bzinfo.xml", (
        '<bzinfo><myidentity online_hostname="Mac-Studio_2026_01_02" bzlogin="x"/>'
        '<bzdirfilter dir="/Users/me/Downloads/" whichfiles="none" />'
        '<globalexcludes excludefiletypes="vmdk"/></bzinfo>'))
    _write(root, "bzreports/bzstat_totalbackup.xml", (
        '<x totnumfilesforbackup="2" totnumbytesforbackup="1300"/>'
        '<v bzVolumeGuid="v0013aaaa" pervol_sel_for_backup_numfiles="1" pervol_sel_for_backup_numbytes="1000"/>'
        '<v bzVolumeGuid="v0017bbbb" pervol_sel_for_backup_numfiles="1" pervol_sel_for_backup_numbytes="300"/>'))


BOOT = [(1000, "/Users/me/Documents/report.pdf"),
        (10, "/Users/me/Library/Saved Application State/x.plist"),
        (5, "/private/var/tmp/t.tmp"),
        (9, "/.Spotlight-V100/store.db"),
        (50, "/Users/me/Downloads/big.iso"),
        (70, "/Users/me/Movies/clip.vmdk"),
        (3, "/Users/me/Library/Preferences/a.plist")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(pq, "ROOT", str(tmp_path / "bz"))
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    bbdb._init_schema(con)
    bbdb._local.con = con
    queries._STORE_OVERRIDE = "parquet"
    yield con, tmp_path
    queries._STORE_OVERRIDE = None
    bbdb._local.con = None


def _files() -> dict[str, tuple]:
    if not pq.available():
        return {}
    with pq.files_cursor() as cur:
        return {r[0]: (r[1], r[2], r[3]) for r in cur.execute(
            "SELECT full_path, drive, excluded, excl_rule FROM files").fetchall()}


def test_mac_bzdata_indexes_with_backblazes_mac_rules(env, monkeypatch):
    con, tmp = env
    root = str(tmp / "macdata")
    _mac_bzdata(root, BOOT)
    monkeypatch.setattr(paths, "_bzdata_root", root)
    assert paths.platform() == "mac" and paths.sep() == "/"
    vols = paths.volumes()
    assert [(v.key, v.mount, v.label) for v in vols] == [("root", "/", "/"), ("Seagate", "/Volumes/Seagate HD", "/Volumes/Seagate HD")]

    assert indexer.refresh()["ok"]
    f = _files()
    backed = sorted(p for p, (_d, ex, _r) in f.items() if not ex)
    assert backed == ["/Users/me/Documents/report.pdf", "/Users/me/Library/Preferences/a.plist",
                      "/Volumes/Seagate HD/Photos/a.jpg"]
    assert f["/Users/me/Library/Saved Application State/x.plist"][2] == "mandatory:/library/"
    assert f["/private/var/tmp/t.tmp"][1] == 1
    assert f["/Users/me/Downloads/big.iso"][2] == "folder:/users/me/downloads/"
    assert f["/Users/me/Movies/clip.vmdk"][2] == "globalext:vmdk"
    assert f["/Volumes/Seagate HD/.bzvol/bzvol.xml"][1] == 1          # rules see the volume-relative path
    assert f["/Volumes/Seagate HD/Photos/a.jpg"][0] == "Seagate"

    ov = queries._overview_live()
    by_drive = {r["drive"]: r for r in ov["rows"]}
    assert by_drive["root"]["backed_files"] == 2 and by_drive["Seagate"]["diff"] == 0

    root_kids = {c["name"] for c in queries.folder_children("/")}
    assert root_kids == {"Users", "private", ".Spotlight-V100"}       # no external volume inside "/"
    assert {c["name"] for c in queries.folder_children("/Volumes/Seagate HD")} == {"Photos", ".bzvol", ".Spotlight-V100"}
    assert set(queries.coverage_children("/Users/me")) == {"Documents", "Library"}   # exact case, as kopia keys "/"

    # Rebuilding only the startup disk keeps the external drive's own rows.
    _write(root, "bzfilelists/v0013aaaa_root_filelist.dat", _filelist(BOOT + [(4, "/Users/me/new.txt")]))
    assert indexer.refresh()["changed"] == ["root"]
    assert con.execute("SELECT count(*) FROM folder_rollup WHERE parent_lc='/volumes/seagate hd/'").fetchone()[0] == 3
    assert "plat=\"mac\"" in queries.generate_exclusion("editable_contains", "/cache/")["snippet"]


def _win_bzdata(root: str, host: str, folder: str) -> None:
    _write(root, "bzfilelists/v0005_c____filelist.dat", _filelist([(10, f"C:\\{folder}\\a.txt")]))
    _write(root, "bzinfo.xml", f'<bzinfo><myidentity online_hostname="{host}" bzlogin="x"/></bzinfo>')


def test_another_computers_data_sets_the_old_history_aside_and_brings_it_back(env, monkeypatch):
    con, tmp = env
    a, b = str(tmp / "a"), str(tmp / "b")
    _win_bzdata(a, "PC-A", "alpha")
    _win_bzdata(b, "PC-B_2026_03_04", "beta")
    snaps = lambda: {r[0] for r in con.execute("SELECT folder FROM folder_snapshots")}  # noqa: E731

    monkeypatch.setattr(paths, "_bzdata_root", a)
    assert indexer.refresh()["ok"]
    assert snaps() == {"C:\\alpha\\a.txt"} and bbdb.kv_get(con, "bz_computer") == "pc-a"

    monkeypatch.setattr(paths, "_bzdata_root", b)
    assert indexer.detect_changes()["changed"]
    assert indexer.refresh()["ok"]
    assert snaps() == {"C:\\beta\\a.txt"} and bbdb.kv_get(con, "bz_computer") == "pc-b"
    assert set(_files()) == {"C:\\beta\\a.txt"}
    import config
    kept = os.listdir(os.path.join(config.DATA_DIR, "bz-previous"))
    assert len(kept) == 1 and kept[0].startswith("pc-a-")

    monkeypatch.setattr(paths, "_bzdata_root", a)
    assert indexer.refresh()["ok"]
    assert "C:\\alpha\\a.txt" in snaps() and "C:\\beta\\a.txt" not in snaps()


def test_the_worker_reads_the_configured_folder(env, monkeypatch):
    """The refresh worker used to read only the default install folder."""
    from app.backblaze import worker
    from app.index import crypto
    from app.settings import save_settings
    _con, tmp = env
    save_settings({"backblaze": {"bzdata_path": str(tmp / "shared")}})
    seen = []
    monkeypatch.setattr(paths, "_bzdata_root", paths.DEFAULT_BZDATA)
    monkeypatch.setattr(crypto, "read_key_payload", lambda *_a: None)
    monkeypatch.setattr(crypto, "worker_key", lambda *_a: True)
    monkeypatch.setattr(indexer, "refresh", lambda force=False: seen.append(paths.bzdata_root()) or {"ok": True})
    worker.main([])
    assert seen == [str(tmp / "shared")]


def test_installed_and_newest_backblaze_versions(tmp_path):
    logs = tmp_path / "bzfilelist"
    logs.mkdir()
    (logs / "bzfilelist09.log").write_text(
        "2026-11-09 00:51:18 - bzfilelist starting, version=10.0.1.1037 os=win\n", encoding="utf-8")
    cv = tmp_path / "clientversion.xml"
    cv.write_text('<clientversion><update win32_version="10.0.9.1"/>'
                  '<release win32_version="10.0.4.1081" mac_version="10.0.4.1082" linux_version="1.0.0.16"/>'
                  '<autoupdate win32_version="9.2.2.897" mac_version="9.2.2.898"/></clientversion>', encoding="utf-8")
    assert parsers.installed_version([str(logs), str(tmp_path / "missing")]) == "10.0.1.1037"
    assert parsers.newest_version(str(cv), "windows") == "10.0.4.1081"
    assert parsers.newest_version(str(cv), "mac") == "10.0.4.1082"
    assert parsers.version_tuple("10.0.1.1037") < parsers.version_tuple("10.0.4.1081")
    assert json.dumps(parsers.version_tuple("x")) == "[]"


def _empty_bzdata(root: str, host: str) -> None:
    _write(root, "bzinfo.xml", f'<bzinfo><myidentity online_hostname="{host}" bzlogin="x"/></bzinfo>')
    os.makedirs(os.path.join(root, "bzfilelists"), exist_ok=True)


def test_a_folder_with_no_finished_scan_waits_instead_of_showing_the_old_computer(env, monkeypatch):
    """the old computer's files must never sit on screen under the new computer's name."""
    con, tmp = env
    a, b = str(tmp / "a"), str(tmp / "b")
    _win_bzdata(a, "PC-A", "alpha")
    _empty_bzdata(b, "PC-B")
    monkeypatch.setattr(paths, "_bzdata_root", a)
    assert indexer.refresh()["ok"]
    assert r"C:\alpha\a.txt" in set(_files())

    monkeypatch.setattr(paths, "_bzdata_root", b)
    assert indexer.refresh()["ok"]
    assert indexer.freshness()["waiting_for_scan"] is True
    assert queries.status()["built"] is False
    assert queries.status()["computer"] == "PC-B"
    assert queries.overview()["rows"] == []
    assert queries.search(query="")["total"] == 0
    assert not pq.available()
    assert set(_files()) == set()


def test_set_aside_history_belongs_to_the_computer_named_inside_it(env, monkeypatch):
    """PC-A-2's newest copy must not be restored as PC-A's history."""
    con, tmp = env
    a, a2 = str(tmp / "a"), str(tmp / "a2")
    _win_bzdata(a, "PC-A", "alpha")
    _win_bzdata(a2, "PC-A-2", "beta")
    snaps = lambda: {r[0] for r in con.execute("SELECT folder FROM folder_snapshots")}  # noqa: E731

    monkeypatch.setattr(paths, "_bzdata_root", a)
    assert indexer.refresh()["ok"]
    assert snaps() == {r"C:\alpha\a.txt"}

    monkeypatch.setattr(paths, "_bzdata_root", a2)
    assert indexer.refresh()["ok"]
    assert snaps() == {r"C:\beta\a.txt"}

    # The PC-A-2 copy is now the newest file, but it is not PC-A's history.
    monkeypatch.setattr(paths, "_bzdata_root", a)
    assert indexer.refresh()["ok"]
    assert snaps() == {r"C:\alpha\a.txt"}
