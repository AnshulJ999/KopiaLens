"""Release packaging: one version everywhere, every data-upgrade step compiled in, portable data
folder, port parsing, the incomplete-install page, and the KopiaUI label."""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import jinja2
import pytest

import config
from app.core import dataupgrade, kopiabin

ROOT = Path(__file__).resolve().parents[2]


def _load(rel: str):
    spec = importlib.util.spec_from_file_location(Path(rel).stem, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_version_is_the_same_everywhere():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    cargo = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "shell/src-tauri/Cargo.toml").read_text(encoding="utf-8"), re.M)
    tauri = json.loads((ROOT / "shell/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert cargo.group(1) == version
    assert tauri["version"] == version
    assert config.APP_VERSION == version


def test_build_compiles_in_every_upgrade_step():
    build = _load("packaging/build_server.py")
    assert build.upgrade_step_modules() == list(dataupgrade._STEP_MODULES)
    args = build.nuitka_args()
    for mod in dataupgrade._STEP_MODULES:
        assert f"--include-module={mod}" in args
    assert any(a.endswith("=VERSION") and a.startswith("--include-data-files=") for a in args)
    assert "--python-flag=no_docstrings" in args


def test_release_reads_the_pinned_kopia_version():
    release = _load("packaging/release.py")
    assert release.versions()["kopia"] == kopiabin.TESTED_VERSION


def test_release_builds_the_banned_list_into_keys_py():
    """The release rewrites the real keys.py; the result must still be the module the app imports."""
    release = _load("packaging/release.py")
    text = (Path(release.KEYS_FILE)).read_text(encoding="utf-8")
    for ids in ({"BBBB-0000-0002", "AAAA-0000-0001"}, set()):
        scope: dict = {}
        exec(compile(release.with_banned_ids(text, ids), "keys.py", "exec"), scope)
        assert scope["BANNED_IDS"] == frozenset(ids)
        assert release.with_banned_ids(release.with_banned_ids(text, ids), ids) == release.with_banned_ids(text, ids)


@pytest.mark.parametrize("raw,expected", [
    ("", 7123), ("7200", 7200), (" 7200 ", 7200), ("abc", 7123), ("0", 7123), ("70000", 7123),
])
def test_bad_port_values_fall_back_instead_of_crashing(monkeypatch, raw, expected):
    monkeypatch.setenv("KOPIALENS_TEST_PORT", raw)
    assert config._env_port("KOPIALENS_TEST_PORT", 7123) == expected


def test_portable_marker_keeps_data_beside_the_app(monkeypatch, tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    monkeypatch.setattr(config, "HERE", str(server))
    monkeypatch.setattr(config, "COMPILED", True)
    assert config.portable_data_dir() is None
    (tmp_path / config.PORTABLE_MARKER).write_text("x", encoding="utf-8")
    assert config.portable_data_dir() == str(tmp_path / "data")
    monkeypatch.setattr(config, "COMPILED", False)
    assert config.portable_data_dir() is None, "running from source never switches to portable"


def test_every_page_file_is_present():
    import app.main as m
    assert m.missing_partials() == []


def test_missing_page_file_shows_a_readable_page(monkeypatch):
    from starlette.testclient import TestClient
    import app.main as m

    def broken(*_args):
        raise jinja2.TemplateNotFound("partials/nav.html")
    monkeypatch.setattr(m, "_compose_html", broken)
    monkeypatch.setattr(m, "missing_partials", lambda: ["partials/nav.html"])
    monkeypatch.setattr(m.auth, "enabled", lambda: False)
    res = TestClient(m.app, base_url="http://127.0.0.1").get("/")
    assert res.status_code == 500
    assert res.headers["content-type"].startswith("text/html")
    assert "Reinstall KopiaLens" in res.text and "partials/nav.html" in res.text


def _kopiaui(tmp_path: Path, portable: bool) -> str:
    install = tmp_path / "KopiaUI"
    bundled = install / "resources" / "server" / kopiabin.EXE_NAME
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"")
    if portable:
        (install / "repositories").mkdir()
    return str(bundled)


@pytest.mark.parametrize("installed,portable,expected", [
    (False, False, False),   # no KopiaUI: nothing uses kopia's folder through it
    (True, False, True),
    (True, True, False),     # portable KopiaUI reads its own repositories folder
])
def test_kopiaui_label_only_when_kopiaui_reads_kopias_folder(monkeypatch, tmp_path, installed, portable, expected):
    paths = [_kopiaui(tmp_path, portable)] if installed else [str(tmp_path / "none" / kopiabin.EXE_NAME)]
    monkeypatch.setattr(kopiabin, "_kopiaui_bundles", lambda: paths)
    if os.name != "nt":
        monkeypatch.setattr(kopiabin.sys, "platform", "linux")
    assert kopiabin.kopiaui_uses_kopia_folder() is expected
