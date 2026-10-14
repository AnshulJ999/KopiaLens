"""The desktop shell's feed and its per-launch secret: it opens only the tray's paths, only from
this computer, and only with the exact secret the shell handed the server."""

from __future__ import annotations

import time

import pytest

import app.main as m
from app.api import shell
from app.core import auth

TOKEN = "s3cret-token"


@pytest.fixture
def signed_in_only(monkeypatch):
    """Sign-in on, nobody signed in, and the shell secret in the environment."""
    from starlette.testclient import TestClient
    monkeypatch.setenv("KOPIALENS_SHELL_TOKEN", TOKEN)
    monkeypatch.setattr(auth, "_STARTED", time.time())
    monkeypatch.setattr(auth, "enabled", lambda: True)
    monkeypatch.setattr(auth, "session_user", lambda cookie: None)

    def make(host="127.0.0.1"):
        return TestClient(m.app, base_url="http://127.0.0.1", client=(host, 50000))
    return make


@pytest.mark.parametrize("path,header,host,ok", [
    ("/api/shell/state", TOKEN, "127.0.0.1", True),
    ("/api/index/pause", TOKEN, "::1", True),
    ("/api/shell/state", "wrong", "127.0.0.1", False),
    ("/api/shell/state", None, "127.0.0.1", False),
    ("/api/shell/state", TOKEN, "192.168.1.20", False),   # another machine
    ("/api/settings", TOKEN, "127.0.0.1", False),          # not a tray path
    ("/api/shell/state", TOKEN, "testclient", False),      # not an address at all
])
def test_the_secret_opens_only_tray_paths_from_this_computer(monkeypatch, path, header, host, ok):
    monkeypatch.setenv("KOPIALENS_SHELL_TOKEN", TOKEN)
    assert auth.shell_request_ok(path, header, host) is ok


def test_no_secret_configured_means_nothing_is_opened(monkeypatch):
    monkeypatch.delenv("KOPIALENS_SHELL_TOKEN", raising=False)
    assert auth.shell_request_ok("/api/shell/state", "", "127.0.0.1") is False
    assert auth.shell_request_ok("/api/shell/state", "anything", "127.0.0.1") is False


def test_signed_out_requests_need_the_secret(signed_in_only):
    client = signed_in_only()
    assert client.get("/api/shell/state").status_code == 401
    assert client.get("/api/shell/state", headers={auth.SHELL_HEADER: "nope"}).status_code == 401
    resp = client.get("/api/shell/state", headers={auth.SHELL_HEADER: TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] and body["prefs"]["tray_click"] in ("window", "menu")
    assert isinstance(body["repos"], list) and "paused" in body["pause"]


def test_the_secret_does_not_open_other_routes(signed_in_only):
    client = signed_in_only()
    assert client.get("/api/settings", headers={auth.SHELL_HEADER: TOKEN}).status_code == 401


def test_the_secret_is_refused_from_another_machine(signed_in_only):
    client = signed_in_only("192.168.1.20")
    assert client.get("/api/shell/state", headers={auth.SHELL_HEADER: TOKEN}).status_code == 401


def test_prefs_accept_only_known_values(monkeypatch):
    from fastapi import HTTPException
    saved = {"kopialens": {}}
    monkeypatch.setattr(shell, "mutate_settings", lambda fn: fn(saved) or saved)
    monkeypatch.setattr(shell, "load_settings", lambda: saved)
    assert shell.set_prefs({"notifications": "warnings"})["notifications"] == "warnings"
    for bad in ({"notifications": "loud"}, {"bind_host": "0.0.0.0"}):
        with pytest.raises(HTTPException):
            shell.set_prefs(bad)
    assert "bind_host" not in saved["kopialens"]


def test_unknown_stored_values_read_as_defaults():
    prefs = shell._prefs({"tray_click": "sideways", "notifications": None, "control_api": False})
    assert prefs == {"tray_click": "window", "tray_double_click": "window", "notifications": "warnings",
                     "tray_icon": "k3g", "quit_indexing": "finish", "control_api": False}
