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
    from app.core.kopia import _repo_args
    own = _repo_args("1")[-1] if _repo_args("1") else ""
    assert all(own in h["fix_command"] and "maintenance" in h["fix_command"]
               for h in health1), health1


def test_index_health_reports_the_published_generation(monkeypatch):
    """Artifacts are immutable, so the useful facts are which generation is live and how big it
    is - not bloat against a compaction baseline, which no longer exists."""
    import app.index.store as store

    monkeypatch.setattr(store, "read_manifest", lambda repo: {
        "generation": 7, "stats": {"paths": 5_333_248, "file_versions": 18_972_487}})
    monkeypatch.setattr(store, "repo_dir", lambda repo: "nowhere")
    monkeypatch.setattr(stats_api.os, "walk", lambda p: [("nowhere", [], ["a.parquet"])])
    monkeypatch.setattr(stats_api.os.path, "getsize", lambda p: 378 * 10**6)
    entries = stats_api._index_health("1")
    assert entries[0]["status"] == "ok"
    assert "generation 7" in entries[0]["title"]
    assert "5,333,248 paths" in entries[0]["detail"]


def test_index_health_warns_when_a_repo_has_never_been_indexed(monkeypatch):
    import app.index.store as store

    monkeypatch.setattr(store, "read_manifest", lambda repo: None)
    entries = stats_api._index_health("1")
    assert entries[0]["status"] == "warn"
    assert "not been indexed" in entries[0]["detail"]


def test_the_settings_payload_never_carries_the_password_hash(monkeypatch):
    """The session cookie is SIGNED with the stored hash, so the hash must never reach the
    browser - anyone holding it could mint valid sessions. The UI only needs yes/no."""
    from app.api import dashboard as dash

    monkeypatch.setattr(dash, "load_settings",
                        lambda: {"kopialens": {"auth_password_hash": "secret", "auth_enabled": True},
                                 "backblaze": {}})
    monkeypatch.setattr(dash, "REPOS", {"1": {"name": "Main", "kind": "kopia"}})
    monkeypatch.setattr(auth, "_kl", lambda: {"auth_enabled": True})
    from app.core import users
    users.create("admin", auth.hash_password("correct horse"))
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


def _fresh_stats_state(monkeypatch):
    monkeypatch.setattr(stats_api, "_stats_cache", {})
    monkeypatch.setattr(stats_api, "_stats_ts", {})
    monkeypatch.setattr(stats_api, "refresh_stats", lambda repo: None)


def test_saved_stats_are_served_after_a_restart_without_asking_kopia(monkeypatch):
    _fresh_stats_state(monkeypatch)
    saved = {"stats_repo1": {"at": 1000.0, "data": {"repo": "1", "health": []}}}
    monkeypatch.setattr(stats_api.sidecache, "read", lambda name: saved.get(name))

    def no_kopia(repo):
        raise AssertionError("must not compute while saved stats exist")

    monkeypatch.setattr(stats_api, "_compute_stats", no_kopia)
    out = stats_api.stats("1")
    assert out["repo"] == "1" and out["computed_at"] == 1000.0


def test_refresh_recomputes_even_when_stats_are_cached(monkeypatch):
    _fresh_stats_state(monkeypatch)
    calls = []

    def compute(repo):
        calls.append(repo)
        stats_api._stats_cache[repo] = {"repo": repo, "n": len(calls)}
        stats_api._stats_ts[repo] = 2000.0 + len(calls)
        return stats_api._stats_cache[repo]

    monkeypatch.setattr(stats_api, "_compute_stats", compute)
    monkeypatch.setattr(stats_api.sidecache, "read", lambda name: None)
    assert stats_api.stats("1")["n"] == 1          # nothing saved: computed once
    assert stats_api.stats("1")["n"] == 1          # cached
    assert stats_api.stats("1", refresh=True)["n"] == 2
