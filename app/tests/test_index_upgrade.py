"""In-place index migration (, index level).

A schema-version change used to mean an automatic rebuild: 10-40 minutes per repo, and the sticky
first-seen dates (which come from snapshots that have since expired) were lost. The registry lets a
format change be migrated in place instead; a rebuild is now only the fallback, and it says why.
A generation from a NEWER app is refused outright.
"""

from __future__ import annotations

import json

import pytest

from app.index import artifacts, store, upgrade

CURRENT = upgrade.MANIFEST_VERSION


@pytest.fixture
def clean_registry():
    with upgrade._LOCK:
        saved = dict(upgrade._MIGRATIONS)
        upgrade._MIGRATIONS.clear()
    try:
        yield
    finally:
        with upgrade._LOCK:
            upgrade._MIGRATIONS.clear()
            upgrade._MIGRATIONS.update(saved)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """A repo root with just a manifest (enough for version checks - no artifacts needed)."""
    root = tmp_path / "repo1"
    root.mkdir()
    monkeypatch.setattr(store, "repo_dir", lambda repo: str(root))
    monkeypatch.setattr(store, "manifest_path", lambda repo: str(root / "manifest.json"))
    return root


def _manifest(version: int, **extra) -> dict:
    return {"generation": 5, "repo": "1", "schema_version": version, "artifacts": {},
            "counters": {}, "sources": {}, "stats": {}, **extra}


def _write(root, manifest) -> None:
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_no_registered_step_means_no_migration(clean_registry, repo_root):
    manifest = _manifest(CURRENT - 1)
    assert upgrade.migrate(str(repo_root), manifest) is None


def test_a_registered_step_migrates_in_place(clean_registry, repo_root, monkeypatch):
    calls = []

    def step(root, manifest):
        calls.append(root)
        return {**manifest, "schema_version": CURRENT, "migrated_by": "test"}

    upgrade.register(CURRENT - 1, step, "bump-fields")
    out = upgrade.migrate(str(repo_root), _manifest(CURRENT - 1))
    assert out["schema_version"] == CURRENT and out["migrated_by"] == "test"
    assert calls == [str(repo_root)]


def test_a_step_that_declares_a_rebuild_is_not_run(clean_registry, repo_root):
    def step(root, manifest):       # pragma: no cover - must not be called
        raise AssertionError("a needs_rebuild step must never run in the migrate path")

    upgrade.register(CURRENT - 1, step, "needs-kopia", needs_rebuild=True)
    assert upgrade.migrate(str(repo_root), _manifest(CURRENT - 1)) is None


def test_a_broken_step_falls_back_to_a_rebuild(clean_registry, repo_root):
    def step(root, manifest):
        raise RuntimeError("boom")

    upgrade.register(CURRENT - 1, step, "broken")
    assert upgrade.migrate(str(repo_root), _manifest(CURRENT - 1)) is None


def test_a_step_that_returns_the_wrong_version_is_rejected(clean_registry, repo_root):
    upgrade.register(CURRENT - 1, lambda root, manifest: {"schema_version": CURRENT - 1}, "lazy")
    assert upgrade.migrate(str(repo_root), _manifest(CURRENT - 1)) is None


@pytest.mark.parametrize("version,state", [
    (CURRENT, "current"), (CURRENT - 1, "rebuild"), (CURRENT + 1, "newer"),
])
def test_index_status_states(clean_registry, repo_root, version, state):
    _write(repo_root, _manifest(version))
    assert upgrade.index_status("1")["state"] == state


def test_index_status_reports_a_migratable_generation(clean_registry, repo_root):
    upgrade.register(CURRENT - 1, lambda root, manifest: {**manifest, "schema_version": CURRENT},
                     "m")
    _write(repo_root, _manifest(CURRENT - 1))
    assert upgrade.index_status("1")["state"] == "migratable"


def test_index_status_is_absent_without_a_manifest(clean_registry, repo_root):
    assert upgrade.index_status("1")["state"] == "absent"


def test_migrate_all_repos_writes_the_migrated_manifest(clean_registry, repo_root, monkeypatch):
    upgrade.register(CURRENT - 1, lambda root, manifest: {**manifest, "schema_version": CURRENT},
                     "m")
    _write(repo_root, _manifest(CURRENT - 1))
    monkeypatch.setattr(upgrade, "index_status", lambda repo: {"repo": repo, "found": CURRENT - 1,
                                                               "expected": CURRENT,
                                                               "state": "migratable"})
    report = upgrade.migrate_all_repos(["1"])
    assert report["migrated"] == ["1"]
    assert json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))["schema_version"] == CURRENT


def test_migrate_all_repos_refuses_a_newer_generation(clean_registry, repo_root):
    _write(repo_root, _manifest(CURRENT + 1))
    report = upgrade.migrate_all_repos(["1"])
    assert report["refused_newer"] == ["1"] and report["migrated"] == []
    assert json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))["schema_version"] == CURRENT + 1


def test_store_refuses_to_read_a_generation_from_another_version(clean_registry, repo_root):
    _write(repo_root, _manifest(CURRENT - 1))
    with pytest.raises(upgrade.IndexVersionMismatch, match="An index pass rebuilds it") as older:
        store.open_generation("1")
    assert older.value.mode == "rebuild" and older.value.found == CURRENT - 1
    _write(repo_root, _manifest(CURRENT + 1))
    with pytest.raises(upgrade.IndexVersionMismatch, match="newer KopiaLens") as newer:
        store.open_generation("1")
    assert newer.value.mode == "newer"


def test_the_older_format_message_follows_the_registered_step(clean_registry, repo_root):
    """A registgered in-place step must NOT be announced as a rebuild (and the reverse). The
    message is user-visible, so it has to match what will actually happen."""
    upgrade.register(CURRENT - 1, lambda root, manifest: {**manifest, "schema_version": CURRENT},
                     "m")
    _write(repo_root, _manifest(CURRENT - 1))
    with pytest.raises(upgrade.IndexVersionMismatch) as exc:
        store.open_generation("1")
    assert exc.value.mode == "migrate" and "upgraded in place" in str(exc.value)
    assert upgrade.migration_mode(CURRENT - 1) == "migrate"


def test_a_step_that_needs_kopia_reads_as_a_rebuild(clean_registry, repo_root):
    upgrade.register(CURRENT - 1, lambda root, manifest: None, "m", needs_rebuild=True)
    _write(repo_root, _manifest(CURRENT - 1))
    with pytest.raises(upgrade.IndexVersionMismatch) as exc:
        store.open_generation("1")
    assert exc.value.mode == "rebuild" and "rebuilds it" in str(exc.value)


def test_build_refuses_a_newer_generation_instead_of_overwriting_it(clean_registry, repo_root):
    _write(repo_root, _manifest(CURRENT + 1))
    with pytest.raises(upgrade.IndexVersionMismatch):
        artifacts.build_repo("1", str(repo_root))


def test_build_rebuilds_loudly_when_no_migration_was_applied(clean_registry, repo_root, monkeypatch):
    """The leftover case: the app never migrated this generation. The build falls back to a rebuild
    (which is why it must say so - it costs 10-40 min and loses the first-seen dates), rather than
    misreading an older layout."""
    _write(repo_root, _manifest(CURRENT - 1))
    monkeypatch.setattr(artifacts, "_build_incremental",
                        lambda repo, root, prev, gen, job, force: {"prev": prev})
    out = artifacts.build_repo("1", str(repo_root))
    assert out["prev"] is None


def test_migrate_all_repos_skips_a_repo_whose_worker_is_running(clean_registry, repo_root, monkeypatch):
    """The app takes over live workers, so a migration must never run under one: rewriting files
    either fails (Windows) or publishes a manifest built from the pre-migration snapshot (POSIX)."""
    upgrade.register(CURRENT - 1, lambda root, manifest: {**manifest, "schema_version": CURRENT},
                     "m")
    _write(repo_root, _manifest(CURRENT - 1))
    from app.core import nightly
    monkeypatch.setattr(nightly, "is_running", lambda repo: True)
    report = upgrade.migrate_all_repos(["1"])
    assert report["skipped_worker_running"] == ["1"] and report["migrated"] == []
    assert json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))["schema_version"] == CURRENT - 1


def test_an_unmigratable_index_starts_an_index_pass_now(clean_registry, repo_root, monkeypatch):
    """An index the user cannot read must not sit broken until the nightly."""
    _write(repo_root, _manifest(CURRENT - 1))          # no step registered -> rebuild
    calls = []
    from app.core import nightly
    monkeypatch.setattr(nightly, "start_index_pass_async",
                        lambda repo, trigger=None, **kw: calls.append((repo, trigger)) or {"started": True})
    report = upgrade.migrate_all_repos(["1"])
    assert report["needs_rebuild"] == ["1"]
    assert calls == [("1", "upgrade")]


def test_a_failed_migration_also_starts_a_pass(clean_registry, repo_root, monkeypatch):
    """A broken step must not leave the repo unreadable until the nightly either."""
    def boom(root, manifest):
        raise RuntimeError("disk on fire")

    upgrade.register(CURRENT - 1, boom, "broken")
    _write(repo_root, _manifest(CURRENT - 1))
    calls = []
    from app.core import nightly
    monkeypatch.setattr(nightly, "start_index_pass_async",
                        lambda repo, trigger=None, **kw: calls.append((repo, trigger)) or {"started": True})
    report = upgrade.migrate_all_repos(["1"])
    assert report["needs_rebuild"] == ["1"] and calls == [("1", "upgrade")]


def test_a_busy_scheduler_queues_the_rebuild_and_the_tick_drains_it(clean_registry, monkeypatch):
    from app.core import nightly
    state = {"busy": True}
    started = []

    def fake_start(repo, trigger=None, **kw):
        if state["busy"]:
            return {"started": False, "reason": "another pass is running"}
        started.append((repo, trigger))
        return {"started": True}

    monkeypatch.setattr(nightly, "start_index_pass_async", fake_start)
    with upgrade._QUEUE_LOCK:
        upgrade._REBUILD_QUEUE.clear()
    out = upgrade.request_rebuild("1")
    assert out == {"started": False, "queued": True} and upgrade.queued_rebuilds() == ["1"]
    assert upgrade.drain_queue() == []                 # still busy: stays queued
    assert upgrade.queued_rebuilds() == ["1"]
    state["busy"] = False
    assert upgrade.drain_queue() == ["1"] and started == [("1", "upgrade")]
    assert upgrade.queued_rebuilds() == []


def test_build_carries_a_current_manifest_forward(clean_registry, repo_root, monkeypatch):
    _write(repo_root, _manifest(CURRENT))
    monkeypatch.setattr(artifacts, "_manifest_usable", lambda root, manifest: True)
    monkeypatch.setattr(artifacts, "_build_incremental",
                        lambda repo, root, prev, gen, job, force: {"prev": prev})
    out = artifacts.build_repo("1", str(repo_root))
    assert out["prev"] is not None and out["prev"]["schema_version"] == CURRENT
