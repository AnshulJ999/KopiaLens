"""Accounts and sign-in (,): sessions, the on/off switch, the legacy move, the
first-run window, and the shared sign-in/encryption password."""

from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException

from app.api import account
from app.core import auth, users
from app.index import crypto


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Settings in memory, a server-profile install, and a real keystore in tmp."""
    state = {"kopialens": {}}
    envvars: dict[str, str] = {}

    def _mutate(fn):
        fn(state)
        return state

    monkeypatch.setattr(auth, "_kl", lambda: state["kopialens"])
    monkeypatch.setattr(auth, "mutate_settings", _mutate)
    monkeypatch.setattr(auth, "raw_kopialens_settings", lambda: state["kopialens"])
    monkeypatch.setattr(auth, "_env", lambda name: envvars.get(name, ""))
    monkeypatch.setattr(auth, "_STARTED", time.time())
    monkeypatch.setattr(auth, "_SETUP_CODE", "")
    monkeypatch.setattr(crypto, "keystore_dir", lambda: str(tmp_path / "keystore"))
    monkeypatch.setattr(crypto, "remember_on_this_machine", lambda on: True)
    crypto.lock()
    yield {"settings": state, "env": envvars}
    crypto.lock()


def _account(name="admin", password="correct horse", **kw):
    return users.create(name, auth.hash_password(password), **kw)


def _body(resp) -> dict:
    return json.loads(resp.body)


# ---- sessions --------------------------------------------------------------------------------

def test_a_session_signs_in_its_account_until_the_password_changes(env):
    user = _account()
    token = auth.issue_session(user, persist=True)
    assert auth.session_user(token)["username"] == "admin"
    users.set_password_hash("admin", auth.hash_password("another password"))
    assert auth.session_user(token) is None


def test_sign_out_all_devices_ends_every_session(env):
    user = _account()
    token = auth.issue_session(user, persist=False)
    users.bump_epoch("admin")
    assert auth.session_user(token) is None
    assert auth.session_user(auth.issue_session(users.find("admin"), persist=False)) is not None


def test_the_server_enforces_the_session_length(env):
    user = _account()
    t0 = 1_800_000_000
    token = auth.issue_session(user, persist=True, now=t0)
    assert auth.session_user(token, now=t0 + 29 * 86400) is not None
    assert auth.session_user(token, now=t0 + 31 * 86400) is None      # default 30 days
    env["settings"]["kopialens"]["session_days"] = 90
    assert auth.session_user(token, now=t0 + 31 * 86400) is not None


def test_a_tampered_or_unknown_session_is_rejected(env):
    _account()
    _account("other", "other password")
    token = auth.issue_session(users.find("admin"), persist=True)
    payload, mac = token.rsplit(".", 1)
    forged = auth._b64(auth._unb64(payload).replace(b"admin", b"other")) + "." + mac
    assert auth.session_user(forged) is None
    assert auth.session_user("garbage") is None and auth.session_user("") is None


def test_keep_me_signed_in_controls_the_cookie_lifetime_only(env):
    assert "max_age" in auth.cookie_kwargs(persist=True)
    assert "max_age" not in auth.cookie_kwargs(persist=False)


# ---- on/off -----------------------------------------------------------------------------------

def test_sign_in_needs_an_account_and_switching_off_keeps_it(env):
    assert auth.enabled() is False                       # fresh install: never locked out
    _account()
    assert auth.enabled() is True
    auth.set_enabled(False)
    assert auth.enabled() is False and users.has_accounts()
    auth.set_enabled(True)
    assert auth.enabled() is True


def test_the_env_switch_beats_the_setting(env):
    _account()
    env["env"]["KOPIALENS_AUTH_ENABLED"] = "false"
    assert auth.enabled() is False


def test_an_unknown_username_and_a_wrong_password_both_fail(env):
    _account()
    assert auth.authenticate("admin", "correct horse")["username"] == "admin"
    assert auth.authenticate("ADMIN", "correct horse") is not None      # case-insensitive name
    assert auth.authenticate("admin", "wrong") is None
    assert auth.authenticate("nobody", "correct horse") is None


# ---- legacy move ------------------------------------------------------------------------------

def test_the_old_shared_password_becomes_the_admin_account(monkeypatch):
    legacy = auth.hash_password("old shared password")
    file_state = {"kopialens": {"auth_password_hash": legacy, "auth_enabled": True}}
    monkeypatch.setattr(users, "raw_kopialens_settings", lambda: file_state["kopialens"])
    monkeypatch.setattr(users, "mutate_settings", lambda fn: fn(file_state))
    monkeypatch.setattr(users, "_MIGRATED", False)
    assert users.migrate_legacy() == {"moved": True}
    admin = users.find("admin")
    assert admin["password_hash"] == legacy and admin["role"] == users.ROLE_ADMIN
    assert "auth_password_hash" not in file_state["kopialens"]
    assert users.migrate_legacy() == {"moved": False}                  # idempotent


# ---- first run ----------------------------------------------------------------------------------

def test_first_run_is_open_for_the_window_then_needs_the_code(env, monkeypatch):
    assert auth.first_run_state()["required"] and auth.first_run_allowed("")
    monkeypatch.setattr(auth, "_STARTED", time.time() - auth.SETUP_WINDOW - 1)
    assert auth.first_run_state()["needs_code"]
    assert not auth.first_run_allowed("") and not auth.first_run_allowed("AAAA-AAAA")
    assert auth.first_run_allowed(auth.setup_code().lower())


def test_desktop_installs_and_a_made_choice_skip_the_window(env, monkeypatch):
    monkeypatch.setattr(auth, "_STARTED", time.time() - auth.SETUP_WINDOW - 1)
    env["env"]["KOPIALENS_PROFILE"] = "desktop"
    assert not auth.first_run_state()["required"]
    env["env"]["KOPIALENS_PROFILE"] = ""
    env["settings"]["kopialens"]["auth_enabled"] = False                 # "skip protection"
    assert not auth.first_run_state()["required"]


def test_creating_the_first_account_after_the_window_needs_the_code(env, monkeypatch):
    monkeypatch.setattr(auth, "_STARTED", time.time() - auth.SETUP_WINDOW - 1)
    with pytest.raises(HTTPException) as exc:
        account.create(account.CreateRequest(username="admin", password="correct horse"))
    assert exc.value.status_code == 403
    resp = account.create(account.CreateRequest(username="admin", password="correct horse",
                                                code=auth.setup_code()))
    assert _body(resp)["state"]["protected"] is True
    assert "kopialens_session=" in resp.headers["set-cookie"]


# ---- shared password --------------------------------------------------------------------------

def _shared(env):
    crypto.setup("correct horse", with_recovery=False)
    account.create(account.CreateRequest(username="admin", password="correct horse"))
    assert users.find("admin")["unlocks_encryption"] is True


def test_changing_a_shared_password_changes_both(env):
    _shared(env)
    body = _body(account.change_password(account.PasswordRequest(current="correct horse",
                                                                 new="battery staple")))
    assert body["both"] is True
    assert auth.authenticate("admin", "battery staple") is not None
    crypto.lock()
    assert crypto.unlock("battery staple")


def test_a_wrong_current_password_changes_nothing(env):
    _shared(env)
    with pytest.raises(HTTPException) as exc:
        account.change_password(account.PasswordRequest(current="wrong one", new="battery staple"))
    assert exc.value.status_code == 401
    crypto.lock()
    assert crypto.unlock("correct horse")


def test_split_then_join_the_passwords(env):
    _shared(env)
    account.split(account.SplitRequest(current="correct horse", encryption_password="separate one"))
    assert users.find("admin")["unlocks_encryption"] is False
    crypto.lock()
    assert crypto.unlock("separate one")
    account.join(account.JoinRequest(password="correct horse", encryption_password="separate one"))
    assert users.find("admin")["unlocks_encryption"] is True
    crypto.lock()
    assert crypto.unlock("correct horse")


def test_signing_in_with_the_shared_password_unlocks_the_index(env):
    _shared(env)
    crypto.lock()
    account.unlock_if_linked(users.find("admin"), "correct horse")
    assert crypto.unlocked()


def test_renaming_keeps_the_browser_signed_in(env):
    _account()
    token = auth.issue_session(users.find("admin"), persist=False)

    class _Req:
        cookies = {auth.COOKIE_NAME: token}
    resp = account.change_username(account.UsernameRequest(username="anshul"), _Req())
    assert users.find("anshul") and users.find("admin") is None
    new_token = resp.headers["set-cookie"].split("kopialens_session=")[1].split(";")[0]
    assert auth.session_user(new_token)["username"] == "anshul"
    assert "max-age" not in resp.headers["set-cookie"].lower()          # still a session cookie


# ---- the real routes ------------------------------------------------------------------------------

def test_signing_in_through_the_route_passes_the_gate(env):
    from starlette.testclient import TestClient

    from app import main
    _account()
    client = TestClient(main.app, base_url="http://127.0.0.1")
    try:
        assert client.get("/api/account").status_code == 401
        bad = client.post("/api/auth/login", data={"username": "admin", "password": "nope"},
                          follow_redirects=False)
        assert bad.status_code == 401 and "Wrong username or password." in bad.text
        ok = client.post("/api/auth/login", follow_redirects=False,
                         data={"username": "admin", "password": "correct horse", "remember": "1"})
        assert ok.status_code == 303 and "max-age" in ok.headers["set-cookie"].lower()
        assert client.get("/api/account").json()["username"] == "admin"
    finally:
        with auth._THROTTLE_LOCK:
            auth._THROTTLE.clear()
