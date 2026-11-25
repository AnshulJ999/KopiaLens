"""Remote access: access modes and the address list, the window-only code, HTTPS, safety headers,
and the licence store key in the keyring."""

from __future__ import annotations

import os
import time

import pytest

from app import main as m
from app.core import auth, netaccess
from app.licensing import licence


@pytest.fixture
def settings(monkeypatch):
    """Explicit kopialens settings, as if read from the file."""
    store: dict = {}
    monkeypatch.setattr(netaccess, "_raw", lambda: store)
    monkeypatch.setattr(netaccess, "_kl", lambda: store)
    for name in ("KOPIALENS_ACCESS", "KOPIALENS_ACCESS_ADDRESSES", "KOPIALENS_HTTPS", "KOPIALENS_TRUSTED_PROXIES",
                 "KOPIALENS_HTTPS_CERT", "KOPIALENS_HTTPS_KEY"):
        monkeypatch.delenv(name, raising=False)
    return store


@pytest.mark.parametrize("profile,mode,bind", [("desktop", "computer", "127.0.0.1"), ("server", "network", "0.0.0.0")])
def test_default_mode_follows_the_profile(settings, monkeypatch, profile, mode, bind):
    monkeypatch.setattr(auth, "profile", lambda: profile)
    assert netaccess.mode() == mode
    assert netaccess.bind_for_mode(netaccess.mode()) == bind


def test_window_mode_is_desktop_only(settings, monkeypatch):
    settings["access"] = "window"
    monkeypatch.setattr(auth, "profile", lambda: "server")
    assert netaccess.mode() == "computer"
    monkeypatch.setattr(auth, "profile", lambda: "desktop")
    assert netaccess.mode() == "window"


def test_address_list_lets_in_this_computer_and_the_listed_networks_only(settings):
    settings["access_addresses"] = ["192.168.1.0/24", "10.0.0.7", "not an address"]
    assert netaccess.client_allowed("192.168.1.44", "addresses")
    assert netaccess.client_allowed("10.0.0.7", "addresses")
    assert netaccess.client_allowed("::ffff:192.168.1.9", "addresses")
    assert netaccess.client_allowed("127.0.0.1", "addresses") and netaccess.client_allowed("::1", "addresses")
    assert not netaccess.client_allowed("10.0.0.8", "addresses")
    assert not netaccess.client_allowed("", "addresses")
    assert netaccess.client_allowed("10.0.0.8", "network")


def test_parse_addresses_splits_good_from_bad():
    assert netaccess.parse_addresses(["192.168.1.5", " 10.0.0.0/8 ", "", "nope"]) == (
        ["192.168.1.5/32", "10.0.0.0/8"], ["nope"])


def test_a_window_code_works_once():
    code = netaccess.issue_code()
    token = netaccess.redeem(code)
    assert token and netaccess.window_ok(token)
    assert netaccess.redeem(code) is None
    assert not netaccess.window_ok("forged")


def test_an_expired_window_code_is_refused(monkeypatch):
    code = netaccess.issue_code()
    real = time.time
    monkeypatch.setattr(netaccess.time, "time", lambda: real() + netaccess._CODE_TTL + 1)
    assert netaccess.redeem(code) is None


def test_desktop_never_serves_https(settings, monkeypatch):
    settings["https"] = "auto"
    monkeypatch.setattr(auth, "profile", lambda: "desktop")
    assert netaccess.https_mode() == "off" and netaccess.tls_files() is None
    monkeypatch.setattr(auth, "profile", lambda: "server")
    assert netaccess.https_mode() == "auto"


def test_the_self_signed_certificate_names_this_computer_and_is_reused(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(netaccess, "_tls_dir", lambda: str(tmp_path))
    cert, key = netaccess.ensure_self_signed()
    info = netaccess.cert_info(cert)
    assert "localhost" in info["names"] and "127.0.0.1" in info["names"]
    if os.name != "nt":
        assert oct(os.stat(key).st_mode & 0o777) == "0o600"
    mtime = os.path.getmtime(cert)
    assert netaccess.ensure_self_signed() == (cert, key) and os.path.getmtime(cert) == mtime


def test_missing_own_certificate_files_fall_back_to_plain_http(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "profile", lambda: "server")
    settings.update(https="files", https_cert=str(tmp_path / "none.pem"), https_key=str(tmp_path / "none.key"))
    assert netaccess.tls_files() is None


# ---- the middleware ------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(auth, "_STARTED", time.time())
    return TestClient(m.app, base_url="http://127.0.0.1")


def test_safety_headers_and_no_frame_blocking(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert "x-frame-options" not in r.headers


def test_window_mode_turns_browsers_away_until_the_code_is_used(client, monkeypatch):
    monkeypatch.setitem(netaccess.STARTED, "mode", "window")
    assert client.get("/api/index/pause").status_code == 403
    r = client.get("/shell/open", params={"code": netaccess.issue_code(), "next": "/api/index/pause"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/api/index/pause"
    assert client.get("/api/index/pause").status_code == 200        # the window cookie is set now


def test_shell_open_never_redirects_off_site(client):
    r = client.get("/shell/open", params={"code": "x", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_address_mode_refuses_unlisted_clients(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setitem(netaccess.STARTED, "mode", "addresses")
    monkeypatch.setattr(netaccess, "addresses", lambda: netaccess._networks(["192.168.1.0/24"]))
    monkeypatch.setattr(auth, "_STARTED", time.time())
    outside = TestClient(m.app, base_url="http://127.0.0.1", client=("10.1.1.1", 5000))
    inside = TestClient(m.app, base_url="http://127.0.0.1", client=("192.168.1.20", 5000))
    assert outside.get("/api/index/pause").status_code == 403
    assert inside.get("/api/index/pause").status_code == 200


def test_cookies_are_secure_only_over_https():
    token = auth.set_secure(True)
    try:
        assert auth.cookie_kwargs()["secure"] is True
    finally:
        auth._SECURE.reset(token)
    assert auth.cookie_kwargs()["secure"] is False


# ---- the licence store key -----------------------------------------------------------------------

def test_the_store_key_goes_to_the_keyring_when_there_is_one(monkeypatch, tmp_path):
    import json
    import keyring
    vault: dict = {}
    monkeypatch.setattr(licence, "LICENCE_DIR", str(tmp_path))
    monkeypatch.setattr(keyring, "set_password", lambda s, a, v: vault.__setitem__((s, a), v))
    monkeypatch.setattr(keyring, "get_password", lambda s, a: vault.get((s, a)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, a: vault.pop((s, a)))
    licence._set_meta("STOR-1", {"key": "SECRET", "activation_id": "act"})
    on_disk = json.loads((tmp_path / licence.META_FILE).read_text(encoding="utf-8"))
    assert "SECRET" not in json.dumps(on_disk) and on_disk["licences"]["STOR-1"]["key_in"] == "keyring"
    assert licence._meta_for("STOR-1") == {"key": "SECRET", "activation_id": "act"}
    licence._set_meta("STOR-1", None)
    assert not vault


def test_without_a_keyring_the_store_key_stays_in_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(licence, "LICENCE_DIR", str(tmp_path))     # the test keyring backend fails
    licence._set_meta("STOR-2", {"key": "SECRET", "activation_id": "act"})
    assert licence._meta_for("STOR-2") == {"key": "SECRET", "activation_id": "act"}


def test_a_window_cookie_survives_a_server_restart_but_not_an_app_restart(monkeypatch):
    monkeypatch.setenv("KOPIALENS_SHELL_TOKEN", "launch-1")
    token = netaccess.redeem(netaccess.issue_code())
    monkeypatch.setattr(netaccess, "_CODES", {})
    monkeypatch.setattr(netaccess, "_WINDOWS", set())      # a restarted server remembers nothing
    assert netaccess.window_ok(token)
    assert not netaccess.window_ok(token.rpartition(".")[0] + ".forged")
    monkeypatch.setenv("KOPIALENS_SHELL_TOKEN", "launch-2")  # the desktop app itself restarted
    assert not netaccess.window_ok(token)


def test_a_certificate_and_key_that_do_not_load_fall_back_to_plain_http(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "profile", lambda: "server")
    monkeypatch.setattr(netaccess, "_tls_dir", lambda: str(tmp_path))
    cert, key = netaccess.ensure_self_signed()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(netaccess, "_tls_dir", lambda: str(other))
    _cert2, key2 = netaccess.ensure_self_signed()
    settings.update(https="files", https_cert=cert, https_key=key2)
    assert netaccess.tls_files() is None                     # the key belongs to another certificate
    settings["https_key"] = key
    assert netaccess.tls_files() == (cert, key)
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate")
    settings["https_cert"] = str(bad)
    assert netaccess.tls_files() is None
