"""Diagnostics bundle privacy: anonymising hides paths, people and addresses consistently, and
settings never carry secrets."""

from __future__ import annotations

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
    text = diagnostics._settings_text()
    assert "pbkdf2" not in text and "k.pem" not in text
    assert "midnight" in text
