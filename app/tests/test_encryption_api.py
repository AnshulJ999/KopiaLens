"""The encryption API surface (): routes called directly, fakes at the edges.

Pins the contract's screens build against - the status shape, the distinct unlock
reasons, the refusal to set up twice, the remember/lock rules, and the switch route (intent
flipped, one job started, nothing rewritten inline). Route-level tests are the only place some
of this is caught: a unit test can pass the wrong reason code straight through.

The settings file is never written: ``mutate_settings`` is faked with a recorder.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import encryption as api
from app.index import crypto, keyswitch


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "keystore_dir", lambda: str(tmp_path))
    monkeypatch.setattr(crypto, "remember_on_this_machine", lambda on: True)
    monkeypatch.setattr(crypto, "is_remembered", lambda: False)
    monkeypatch.setattr("app.settings.load_settings", lambda: {"kopialens": {"encryption": True}})
    monkeypatch.setattr("app.settings.raw_kopialens_settings",
                        lambda: {"encryption_level": "index"})
    monkeypatch.setattr(keyswitch, "repo_states", lambda: [
        {"repo": "1", "generation": 4, "encrypted": False, "pending": True},
    ])
    monkeypatch.setattr(keyswitch, "bz_state", lambda: {
        "repo": "bz", "generation": None, "encrypted": False, "pending": False,
        "exists": False})
    monkeypatch.setattr("app.core.filecrypto.pending_files", lambda: [])
    saved = {"level": None, "writes": 0}

    def fake_mutate(mutator):
        settings = {"kopialens": {}}
        mutator(settings)
        saved["level"] = settings["kopialens"].get("encryption_level")
        saved["writes"] += 1
        return settings

    monkeypatch.setattr(api, "mutate_settings", fake_mutate)
    jobs = {"started": [], "kinds": []}

    def fake_start(kind, repo, params, *args, **kwargs):
        jobs["started"].append({"kind": kind, "repo": repo, "params": params})
        return {"job_id": "j1", "kind": kind, "repo": repo}

    monkeypatch.setattr(api.jobrunner, "start_job", fake_start)
    monkeypatch.setattr(api.jobrunner, "job_status", lambda job_id, after=0: {"job_id": job_id})
    crypto.lock()
    yield {"saved": saved, "jobs": jobs}
    crypto.lock()


def test_status_reports_the_pending_repos_and_the_level(wired):
    body = api.status()
    assert body["configured"] is False and body["unlocked"] is False
    assert body["pending"] == ["1"] and body["repos"][0]["generation"] == 4
    assert body["level"] == "index" and body["files_pending"] == 0
    assert body["backblaze"]["exists"] is False


def test_setup_creates_the_keystore_and_returns_a_recovery_key(wired):
    body = api.setup(api.SetupRequest(password="correct horse", recovery=True))
    assert body["ok"] is True and body["recovery_key"]
    assert body["status"]["configured"] is True and body["status"]["unlocked"] is True
    assert body["status"]["has_recovery"] is True
    assert wired["saved"]["level"] == "everything"     # the ticked onboarding box means Everything


def test_setup_refuses_a_second_keystore(wired):
    api.setup(api.SetupRequest(password="correct horse"))
    with pytest.raises(HTTPException) as exc:
        api.setup(api.SetupRequest(password="another one"))
    assert exc.value.status_code == 409


def test_setup_refuses_a_short_password(wired):
    with pytest.raises(HTTPException) as exc:
        api.setup(api.SetupRequest(password="short"))
    assert exc.value.status_code == 400


def test_a_wrong_password_and_a_damaged_keystore_are_different_answers(wired, tmp_path):
    api.setup(api.SetupRequest(password="correct horse", recovery=False))
    api.lock()
    assert api.status()["unlocked"] is False

    with pytest.raises(HTTPException) as exc:
        api.unlock(api.UnlockRequest(password="wrong horse"))
    assert exc.value.status_code == 401                 # wrong password

    wrap = tmp_path / "keystore-password.kdb"
    wrap.write_bytes(b"this is not a keystore anymore")
    with pytest.raises(HTTPException) as exc:
        api.unlock(api.UnlockRequest(password="correct horse"))
    assert exc.value.status_code == 500                 # damaged file, not a wrong password


def test_unlock_and_lock_round_trip(wired):
    api.setup(api.SetupRequest(password="correct horse", recovery=False))
    api.lock()
    assert api.unlock(api.UnlockRequest(password="correct horse"))["status"]["unlocked"] is True
    assert api.lock()["status"]["unlocked"] is False


def test_change_password_needs_the_old_one(wired):
    api.setup(api.SetupRequest(password="correct horse", recovery=False))
    with pytest.raises(HTTPException) as exc:
        api.change_password(api.ChangePasswordRequest(old="guess", new="another good one"))
    assert exc.value.status_code == 401
    body = api.change_password(api.ChangePasswordRequest(old="correct horse",
                                                         new="another good one"))
    assert body["ok"] is True
    api.lock()
    assert api.unlock(api.UnlockRequest(password="another good one"))["ok"] is True
    with pytest.raises(HTTPException):
        api.unlock(api.UnlockRequest(password="correct horse"))


def test_the_recovery_key_resets_a_forgotten_password(wired):
    body = api.setup(api.SetupRequest(password="correct horse", recovery=True))
    recovery = body["recovery_key"]
    api.lock()

    assert api.unlock_recovery(api.RecoveryRequest(recovery_key=recovery))["ok"] is True
    api.change_password(api.ChangePasswordRequest(old="correct horse", new="a fresh new one"))

    # Forgot it entirely: the recovery key still opens the store, and sets a new password.
    api.reset_password(api.ResetPasswordRequest(recovery_key=recovery, new="third time lucky"))
    api.lock()
    assert api.unlock(api.UnlockRequest(password="third time lucky"))["ok"] is True
    assert api.unlock_recovery(api.RecoveryRequest(recovery_key=recovery))["ok"] is True


def test_a_mistyped_recovery_key_is_refused(wired):
    api.setup(api.SetupRequest(password="correct horse", recovery=True))
    api.lock()
    with pytest.raises(HTTPException) as exc:
        api.unlock_recovery(api.RecoveryRequest(recovery_key="not-even-base64 !!"))
    assert exc.value.status_code == 401


def test_issuing_a_new_recovery_key_needs_the_password(wired):
    api.setup(api.SetupRequest(password="correct horse", recovery=False))
    with pytest.raises(HTTPException):
        api.new_recovery_key(api.UnlockRequest(password="wrong"))
    body = api.new_recovery_key(api.UnlockRequest(password="correct horse"))
    assert body["recovery_key"] and body["status"]["has_recovery"] is True


def test_remember_refuses_while_locked(wired):
    api.setup(api.SetupRequest(password="correct horse", recovery=False))
    api.lock()
    with pytest.raises(HTTPException) as exc:
        api.remember(api.RememberRequest(on=True))
    assert exc.value.status_code == 409


def test_the_level_route_flips_the_intent_and_starts_one_job(wired):
    api.setup(api.SetupRequest(password="correct horse", recovery=False))
    body = api.set_level(api.LevelRequest(level="index"))
    assert wired["saved"]["level"] == "index"
    assert wired["jobs"]["started"] == [
        {"kind": "encryption_level", "repo": "all", "params": {"level": "index"}}]
    assert body["started"] == "j1"


def test_the_level_route_refuses_a_bad_level_or_a_locked_install(wired):
    api.setup(api.SetupRequest(password="correct horse", recovery=False))
    with pytest.raises(HTTPException) as exc:
        api.set_level(api.LevelRequest(level="sometimes"))
    assert exc.value.status_code == 400
    api.lock()
    with pytest.raises(HTTPException) as exc:
        api.set_level(api.LevelRequest(level="everything"))
    assert exc.value.status_code == 409
    assert wired["jobs"]["started"] == []
    assert wired["saved"]["writes"] == 1             # setup's own write; neither refusal wrote


def test_the_level_route_refuses_to_encrypt_without_a_keystore(wired, monkeypatch):
    monkeypatch.setattr(crypto, "unlocked", lambda: True)    # past the unlock check, keystore absent
    with pytest.raises(HTTPException) as exc:
        api.set_level(api.LevelRequest(level="everything"))
    assert exc.value.status_code == 409
    assert "Set up encryption" in exc.value.detail
