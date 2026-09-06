"""Follow-up review fixes: the Stats health commands target the right repo, the settings payload
never leaks the password hash, and the week view tolerates a timestamp without an offset."""

from __future__ import annotations

import datetime as dt

from app.api import stats as stats_api
from app.api.tree import _parse_start
from app.core import auth


def test_maintenance_fix_commands_target_their_own_repo():
    """A bare `kopia maintenance run --full` operates on the DEFAULT repository - copying a fix
    from repo 2 would run it on repo 1. The command must carry the repo's own config args."""
    maint = {"owner": "someone@elsewhere", "full": {"enabled": True},
             "schedule": {"runs": {}}}
    _, health2 = stats_api._maintenance_health("2", maint, {"clientOptions": {"hostname": "x"}})
    assert health2, "an owner mismatch must warn"
    assert all("--config-file" in h["fix_command"] for h in health2), health2
    _, health1 = stats_api._maintenance_health("1", maint, {"clientOptions": {"hostname": "x"}})
    assert all(h["fix_command"].startswith("kopia maintenance") for h in health1), health1


def test_the_duckdb_bloat_fix_names_its_repo(monkeypatch):
    import app.index.schema as schema

    monkeypatch.setattr(schema, "db_path", lambda repo: "nowhere.duckdb")
    monkeypatch.setattr(schema, "compact_state", lambda: {"1": {"size": 10**9}})
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr(stats_api.os.path, "getsize", lambda p: 2 * 10**9)
    entries = stats_api._duckdb_health("1")
    assert entries[0]["status"] == "warn"
    assert entries[0]["fix_command"] == "python compact.py 1"


def test_the_settings_payload_never_carries_the_password_hash(monkeypatch):
    """The session cookie is SIGNED with the stored hash, so the hash must never reach the
    browser - anyone holding it could mint valid sessions. The UI only needs yes/no."""
    from app.api import dashboard as dash

    monkeypatch.setattr(dash, "load_settings",
                        lambda: {"kopialens": {"auth_password_hash": "secret", "auth_enabled": True},
                                 "backblaze": {}})
    monkeypatch.setattr(dash, "REPOS", {"1": {"name": "Main", "kind": "kopia"}})
    monkeypatch.setattr(auth, "_kl", lambda: {"auth_enabled": True, "auth_password_hash": "secret"})
    kl = dash.repos()["settings"]["kopialens"]
    assert "auth_password_hash" not in kl
    assert kl["auth_protected"] is True


def test_a_timestamp_without_an_offset_still_parses():
    got = _parse_start("2026-08-30T14:00:00")
    assert got is not None and got.tzinfo is not None, "naive timestamps must become comparable"


def test_the_default_actor_is_the_single_user():
    assert auth.current_actor() == auth.SUBJECT == "admin"
