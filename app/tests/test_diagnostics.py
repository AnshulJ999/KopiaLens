"""Diagnostics bundle privacy: anonymising hides paths, people and addresses consistently, and
settings never carry secrets."""

from __future__ import annotations

import json

from app.core import diagnostics

SEP = chr(92)


def test_anonymising_hides_private_strings_and_keeps_app_routes():
    anon = diagnostics._Anon("salt")
    path = "H:" + SEP + "PSP Emulator" + SEP + "Games"
    text = (f"remove {path} for alice@desktop:{path} mail bob@example.com from 192.168.1.20 "
            "GET /api/search/detail -> 200")
    out = anon(text)
    for private in ("PSP Emulator", "alice", "desktop", "bob@example.com", "192.168.1.20"):
        assert private not in out
    assert "/api/search/detail" in out
    assert anon(path) == anon(path)                       # same folder, same code across bundles
    assert diagnostics._Anon("other")(path) != anon(path)  # keyed to this install


def test_settings_lose_their_secrets(monkeypatch):
    from app import settings
    monkeypatch.setattr(settings, "load_settings", lambda: {"kopialens": {
        "auth_password_hash": "pbkdf2$abc", "https_key": "C:/k.pem", "theme": "midnight", "update_check": True}})
    text = json.dumps(diagnostics._settings_data())
    assert "pbkdf2" not in text and "k.pem" not in text
    assert "midnight" in text


def test_json_paths_with_spaces_and_typed_text_are_hidden_whole():
    """Escaped JSON text splits a Windows path at its spaces, so JSON is anonymised as data."""
    anon = diagnostics._Anon("salt")
    path = "D:" + SEP + "Personal Stuff" + SEP + "Borderlands 3 Mods"
    entry = {"actor": "carol", "summary": {"path": path, "query": "tax return 2025", "sources": [path]}}
    out = json.dumps(anon.walk(entry))
    for private in ("Stuff", "Mods", "tax return", "carol"):
        assert private not in out
    repr_text = "moved " + repr({"p": path})                 # doubled separators, as in a log line
    assert "Stuff" not in anon(repr_text)



def test_recent_problems_group_repeats_and_respect_the_period(monkeypatch, tmp_path):
    import time as _t
    log = tmp_path / "kopialens.log"
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    old = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - 10 * 86400))
    log.write_text("\n".join([
        f"{now},1 ERROR kopialens.app | repo 1 failed after 12 s",
        f"{now},2 ERROR kopialens.app | repo 1 failed after 30 s",
        f"{now},3 INFO  kopialens.app | fine",
        f"{old},4 WARNING kopialens.app | too old",
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(diagnostics, "LOG_FILE", str(log))
    out = diagnostics._problems(7)
    assert "2x ERROR" in out and "fine" not in out and "too old" not in out


def test_ipv6_addresses_are_hidden_and_clock_times_are_not():
    anon = diagnostics._Anon("salt")
    out = anon("allow fd00::/8 and 2001:db8:0:1:2:3:4:5 at 04:27:03,123 via tauri::Builder on ::1")
    for private in ("fd00::", "2001:db8"):
        assert private not in out
    assert "04:27:03,123" in out and "tauri::Builder" in out and "::1" in out
    walked = anon.walk({"access_addresses": ["fd00::/8", "192.168.1.0/24"]})
    assert "fd00" not in json.dumps(walked) and "192.168" not in json.dumps(walked)
