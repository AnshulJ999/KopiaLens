"""What the machine running KopiaLens can do: folder picker choice, open-folder, mount folders,
the drive list, the folder browser, and restart without a tray."""

from __future__ import annotations

import os
import sys

import pytest

from app.core import disklookup, host, mounts

POSIX = pytest.mark.skipif(os.name == "nt", reason="Linux/macOS behaviour")


def _settings(monkeypatch, **kl):
    monkeypatch.setattr(host, "_kl", lambda: kl)


@pytest.mark.parametrize("choice,client,possible,want", [
    ("auto", "127.0.0.1", True, "native"),
    ("auto", "192.168.1.20", True, "browser"),
    ("native", "192.168.1.20", True, "native"),
    ("browser", "127.0.0.1", True, "browser"),
    ("native", "127.0.0.1", False, "browser"),   # Linux/Docker: no Windows dialog to show
])
def test_folder_picker_choice(monkeypatch, choice, client, possible, want):
    _settings(monkeypatch, folder_picker=choice)
    monkeypatch.setattr(host, "native_picker_possible", lambda: possible)
    assert host.folder_picker(client) == want


def test_no_desktop_in_docker_means_no_open_folder(monkeypatch):
    monkeypatch.setattr(host, "in_docker", lambda: True)
    calls = []
    monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert host.has_desktop() is False
    assert host.open_folder("/data") is False
    assert calls == []


@POSIX
def test_shown_path_maps_the_mount_folder_to_the_host(monkeypatch):
    monkeypatch.setenv("KOPIALENS_MOUNT_DIR", "/mounts")
    _settings(monkeypatch, mount_host_path="/mnt/kopialens/")
    assert host.shown_path("/mounts/repo1-abcd1234") == "/mnt/kopialens/repo1-abcd1234"
    assert host.shown_path("/mountsX/other") == "/mountsX/other"
    _settings(monkeypatch)
    assert host.shown_path("/mounts/repo1-abcd1234") == "/mounts/repo1-abcd1234"


@POSIX
def test_auto_mountpoint_is_a_new_folder_under_the_mount_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("KOPIALENS_MOUNT_DIR", str(tmp_path))
    first = mounts.auto_mountpoint("2", "k0123456789abcdef")
    assert first == str(tmp_path / "repo2-k0123456")
    os.makedirs(first)
    (tmp_path / "repo2-k0123456" / "left-over").write_text("x")
    assert mounts.auto_mountpoint("2", "k0123456789abcdef") == first + "-2"


@POSIX
def test_release_folder_touches_only_our_own_folders(monkeypatch, tmp_path):
    root = tmp_path / "mounts"
    root.mkdir()
    monkeypatch.setenv("KOPIALENS_MOUNT_DIR", str(root))
    monkeypatch.setattr(host, "fusermount", lambda: None)
    ours = root / "repo1-aaaa"
    ours.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    mounts.release_folder(str(ours))
    mounts.release_folder(str(other))
    assert not ours.exists()
    assert other.exists()


@POSIX
def test_drive_list_skips_files_system_paths_and_the_data_folder(monkeypatch, tmp_path):
    import config
    data = tmp_path / "kldata"
    data.mkdir()
    user = tmp_path / "photos"
    user.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    assert disklookup._user_folder(str(user))
    assert not disklookup._user_folder(str(data))
    assert not disklookup._user_folder("/etc/hosts")
    assert not disklookup._user_folder("/proc")


def test_folder_browser_lists_folders_only(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "A").mkdir()
    (tmp_path / "file.txt").write_text("x")
    out = host.list_folders(str(tmp_path))
    assert [f["name"] for f in out["folders"]] == ["A", "b"]
    assert out["parent"] == str(tmp_path.parent)
    with pytest.raises(FileNotFoundError):
        host.list_folders(str(tmp_path / "missing"))


def test_restart_without_a_tray_exits_only_in_docker(monkeypatch):
    from app.api import appconfig
    monkeypatch.setattr(appconfig, "_tray", lambda *a, **k: None)
    monkeypatch.setattr(host, "in_docker", lambda: False)
    assert appconfig.restart() == {"ok": False, "reason": "no-tray"}
    started = []
    monkeypatch.setattr(host, "in_docker", lambda: True)
    monkeypatch.setattr(appconfig.threading, "Thread", lambda **k: type("T", (), {"start": lambda s: started.append(k["name"])})())
    assert appconfig.restart() == {"ok": True, "how": "container"}
    assert started == ["restart-request"]


def test_mount_support_names_what_is_missing(monkeypatch):
    if os.name == "nt":
        assert host.mount_support()["ok"] is True
        return
    if sys.platform == "darwin":
        pytest.skip("macFUSE check")
    real = os.path.exists
    monkeypatch.setattr(host.os.path, "exists", lambda p: False if p == "/dev/fuse" else real(p))
    monkeypatch.setattr(host, "in_docker", lambda: True)
    out = host.mount_support()
    assert out["ok"] is False and "FUSE" in out["reason"]
