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


def test_the_week_view_only_takes_snapshots_from_inside_that_week(monkeypatch):
    """The selection used to be "latest snapshot at or before the week's end" - unbounded below, so
    a source last backed up in July answered a query about a week in August. Both ends now bound it,
    and a source with no snapshot that week contributes nothing."""
    from app.api import tree as tree_api

    snaps = {
        "nightly": [{"id": "n1", "startTime": "2026-08-25T02:00:00+05:30"},
                    {"id": "n2", "startTime": "2026-08-28T02:00:00+05:30"},
                    {"id": "n3", "startTime": "2026-09-03T02:00:00+05:30"}],   # a later week
        "stale": [{"id": "s1", "startTime": "2026-07-04T02:00:00+05:30"}],     # months earlier
    }
    monkeypatch.setattr(tree_api, "sources_index", lambda repo: snaps)
    ids = tree_api._snapshot_ids_in_week("1", "2026-08-30")  # week of Aug 24 - Aug 30
    assert sorted(ids) == ["n1", "n2"], ids
    assert "s1" not in ids, "a source that did not back up that week must contribute nothing"
    assert "n3" not in ids, "a later week's snapshot must not leak in"


def test_week_bounds_span_the_whole_seven_days():
    from app.api.tree import _week_bounds

    start, end = _week_bounds("2026-08-30")
    assert start.date().isoformat() == "2026-08-24"
    assert end.date().isoformat() == "2026-08-30"
    assert (end - start).days == 6


def test_automatic_compaction_is_audited_as_system_not_as_the_user():
    """One column with one value carries no information. Auto-compaction is the only audited action
    that is not a click, so it is the one that must not say "admin"."""
    from app.core import audit, auth

    assert audit.SYSTEM == "system"
    assert auth.current_actor() != audit.SYSTEM
