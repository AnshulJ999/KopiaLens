"""Configuration backups, damaged-file recovery and the cross-process settings lock (,).

The failure behind them (2026-09-09): a second process wrote settings.json concurrently, the file
went bad, and the app silently reset to defaults - losing the bind address and table layouts.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap
import time
import zipfile

import pytest

import config
from app import settings as settings_mod
from app.core import configbackup, filelock, users


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Config files in a temp folder."""
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "REPOS_FILE", str(tmp_path / "repos.json"))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(users, "USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setattr(users, "_CACHE", {"mtime": None, "data": None})
    monkeypatch.setattr(configbackup, "_LAST_CHANGE", {"at": 0.0})
    (tmp_path / "settings.json").write_text(json.dumps({"kopialens": {"bind_host": "0.0.0.0"}}))
    (tmp_path / "repos.json").write_text(json.dumps({"version": 1, "repos": [{"id": "1", "name": "Main"}]}))
    return tmp_path


def test_a_damaged_settings_file_comes_back_from_the_newest_backup(cfg):
    assert configbackup.create("manual")
    (cfg / "settings.json").write_text("{torn", encoding="utf-8")

    assert settings_mod.load_settings()["kopialens"]["bind_host"] == "0.0.0.0"
    assert json.loads((cfg / "settings.json").read_text())["kopialens"]["bind_host"] == "0.0.0.0"
    assert list(cfg.glob("settings.json.damaged-*")), "the damaged file is kept, not deleted"
    notice = configbackup.notices()[0]
    assert notice["kind"] == "recovered" and notice["file"] == "settings.json"


def test_without_a_backup_the_damage_is_still_reported(cfg):
    (cfg / "settings.json").write_text("{torn", encoding="utf-8")
    assert settings_mod.load_settings()["kopialens"]["bind_host"] == "127.0.0.1"   # the default
    assert configbackup.notices()[0]["kind"] == "reset"


def test_a_save_never_writes_over_settings_it_could_not_open(cfg):
    """A file briefly held by another program is not damaged; the save fails instead of writing a
    file that holds only the one changed key."""
    (cfg / "settings.json").unlink()
    (cfg / "settings.json").mkdir()          # exists, but open() raises OSError
    with pytest.raises(OSError):
        settings_mod.mutate_settings(lambda s: s.setdefault("kopialens", {}).update(theme="pine"))


def test_a_damaged_accounts_file_keeps_sign_in_on(cfg):
    users._write({"version": 1, "secret": "s", "users": [{"username": "admin", "password_hash": "h"}]})
    assert configbackup.create("manual")
    (cfg / "users.json").write_text("{torn", encoding="utf-8")
    users._CACHE.update(mtime=None, data=None)
    assert [u["username"] for u in users._read()["users"]] == ["admin"]


def test_edits_in_a_burst_share_one_change_backup(cfg):
    for theme in ("pine", "ember", "slate"):
        settings_mod.mutate_settings(lambda s, t=theme: s.setdefault("kopialens", {}).update(theme=t))
    changes = [b for b in configbackup.list_backups() if b["kind"] == "change"]
    assert len(changes) == 1
    saved = json.loads((cfg / "config-backups" / changes[0]["id"] / "settings.json").read_text())
    assert "theme" not in saved["kopialens"], "the copy holds the state from BEFORE the edits"


def test_each_kind_keeps_its_own_count(cfg, monkeypatch):
    monkeypatch.setitem(configbackup.KEEP, "manual", 3)
    for _ in range(5):
        configbackup.create("manual")
    configbackup.create("weekly")
    kinds = [b["kind"] for b in configbackup.list_backups()]
    assert kinds.count("manual") == 3 and kinds.count("weekly") == 1


def test_restore_puts_back_only_the_chosen_files_and_keeps_a_safety_copy(cfg):
    backup = configbackup.create("manual")
    (cfg / "settings.json").write_text(json.dumps({"kopialens": {"bind_host": "10.0.0.1"}}))
    (cfg / "repos.json").write_text(json.dumps({"version": 1, "repos": []}))

    out = configbackup.restore(backup["id"], ["settings"])

    assert out["restored"] == ["settings.json"]
    assert json.loads((cfg / "settings.json").read_text())["kopialens"]["bind_host"] == "0.0.0.0"
    assert json.loads((cfg / "repos.json").read_text())["repos"] == []
    assert any(b["kind"] == "safety" for b in configbackup.list_backups())


def test_restore_refuses_names_that_are_not_backups(cfg):
    with pytest.raises(ValueError):
        configbackup.restore("../settings.json", ["settings"])


def test_export_import_round_trip_previews_before_writing(cfg):
    data = configbackup.export_zip(["settings", "repos"])
    (cfg / "repos.json").write_text(json.dumps({"version": 1, "repos": []}))

    preview = configbackup.import_preview(data)
    repos = next(f for f in preview["files"] if f["file"] == "repos.json")
    assert repos["added"] == ["Main"]
    assert json.loads((cfg / "repos.json").read_text())["repos"] == [], "preview writes nothing"

    configbackup.import_apply(preview["token"], ["repos"])
    assert json.loads((cfg / "repos.json").read_text())["repos"][0]["name"] == "Main"


@pytest.mark.parametrize("name", ["../settings.json", "sub/settings.json", "evil.exe"])
def test_import_refuses_files_it_would_not_write(cfg, name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr(name, "{}")
    with pytest.raises(ValueError):
        configbackup.import_preview(buf.getvalue())


def test_a_second_process_waits_for_the_settings_lock(tmp_path):
    target = tmp_path / "settings.json"
    holder = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(config.HERE)!r})
        from app.core.filelock import locked
        with locked({str(target)!r}):
            print("held", flush=True)
            time.sleep(1.5)
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        started = time.monotonic()
        with filelock.locked(str(target), timeout=10):
            waited = time.monotonic() - started
        assert waited > 0.5
    finally:
        proc.wait(timeout=10)


def test_table_layouts_live_outside_settings_and_move_out_once(cfg, monkeypatch):
    """Column drags rewrite ui_state.json, never settings.json; the upgrade step moves the old
    layouts across without losing one written meanwhile."""
    from app.core import uistate, uistate_upgrade
    monkeypatch.setattr(uistate, "UI_STATE_FILE", str(cfg / "ui_state.json"))
    monkeypatch.setattr(uistate_upgrade, "SETTINGS_FILE", str(cfg / "settings.json"))
    (cfg / "settings.json").write_text(json.dumps({"kopialens": {"tables": {"old": 1, "both": "old"}}}))

    uistate.set_table("both", "new")
    assert uistate.tables() == {"old": 1, "both": "new"}
    assert json.loads((cfg / "settings.json").read_text())["kopialens"]["tables"]["both"] == "old"

    uistate_upgrade.move_tables()
    assert "tables" not in json.loads((cfg / "settings.json").read_text())["kopialens"]
    assert uistate.tables() == {"old": 1, "both": "new"}


def test_recovery_keeps_the_latest_saved_change(cfg):
    """The before-change backup predates a burst of edits; the last good copy does not."""
    settings_mod.mutate_settings(lambda s: s.setdefault("kopialens", {}).update(theme="pine"))
    (cfg / "settings.json").write_text("{torn", encoding="utf-8")
    assert settings_mod.load_settings()["kopialens"]["theme"] == "pine"
