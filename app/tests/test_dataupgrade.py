"""The data-folder version + upgrade routine ().

The failure this exists for: a format change in a file with no version of its own was silently
ignored or misread (the 2026-09-17 60-second folder panel). The routine also refuses a folder from
a NEWER app, and a failed step must never be skipped - the version advances only after the step
completes.
"""

from __future__ import annotations

import json

import pytest

from app.core import dataupgrade


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """An empty data folder with the version file + settings file redirected."""
    monkeypatch.setattr(dataupgrade, "VERSION_FILE", str(tmp_path / "data_version.json"))
    monkeypatch.setattr(dataupgrade, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(dataupgrade, "_STEP_MODULES", ())   # only the steps a test registers
    yielded = {"steps": dict(dataupgrade._STEPS), "dead": dataupgrade.DEAD_SETTINGS_KEYS}
    dataupgrade._STEPS.clear()
    try:
        yield tmp_path
    finally:
        dataupgrade._STEPS.clear()
        dataupgrade._STEPS.update(yielded["steps"])
        monkeypatch.setattr(dataupgrade, "DEAD_SETTINGS_KEYS", yielded["dead"])


def test_fresh_folder_reports_version_1(folder):
    assert dataupgrade.status()["version"] == 1
    assert dataupgrade.status()["newer"] is False
    assert dataupgrade.status()["pending"] == []


def test_an_unversioned_folder_is_stamped_not_guessed(folder):
    """A folder with no stamp predates versioning and IS version 1 - stamped as such, so only the
    steps from version 1 onward run."""
    assert dataupgrade.status()["unversioned"] is True
    out = dataupgrade.run_pending()
    assert out["version"] == 1 and out["ran"] == []
    assert json.loads((folder / "data_version.json").read_text(encoding="utf-8"))["version"] == 1


def test_a_newer_folder_is_refused_and_never_written(folder):
    (folder / "data_version.json").write_text(json.dumps({"version": 99}), encoding="utf-8")
    status = dataupgrade.status()
    assert status["newer"] is True and "newer KopiaLens" in status["detail"]
    out = dataupgrade.run_pending()
    assert out["newer"] is True and out["ran"] == []
    assert json.loads((folder / "data_version.json").read_text(encoding="utf-8"))["version"] == 99


def test_steps_run_in_order_and_advance_the_version(folder, monkeypatch):
    monkeypatch.setattr(dataupgrade, "DATA_VERSION", 3)
    seen = []

    def step_two():
        seen.append("two")
        return {"changed": 2}

    def step_three():
        seen.append("three")
        return {}

    dataupgrade.register(1, step_two, "one-to-two")
    dataupgrade.register(2, step_three, "two-to-three")
    (folder / "data_version.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert [s["name"] for s in dataupgrade.status()["pending"]] == ["one-to-two", "two-to-three"]
    out = dataupgrade.run_pending()
    assert seen == ["two", "three"] and out["version"] == 3
    assert [r["name"] for r in out["ran"]] == ["one-to-two", "two-to-three"]
    assert out["ran"][0]["report"] == {"changed": 2}


def test_a_failed_step_is_retried_next_start_not_skipped(folder, monkeypatch):
    monkeypatch.setattr(dataupgrade, "DATA_VERSION", 2)

    def boom():
        raise RuntimeError("disk on fire")

    dataupgrade.register(1, boom, "one-to-two")
    (folder / "data_version.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    out = dataupgrade.run_pending()          # never raises
    assert out["version"] == 1 and out["ran"] == []
    assert "disk on fire" in dataupgrade.status()["error"]
    assert json.loads((folder / "data_version.json").read_text(encoding="utf-8"))["version"] == 1


def test_a_missing_step_stops_at_the_last_completed_version(folder, monkeypatch):
    monkeypatch.setattr(dataupgrade, "DATA_VERSION", 3)

    def only_one():
        return {}

    dataupgrade.register(1, only_one, "one-to-two")
    (folder / "data_version.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    out = dataupgrade.run_pending()
    assert out["version"] == 2                    # 2 -> 3 has no step: it waits for one
    assert [r["name"] for r in out["ran"]] == ["one-to-two"]


def test_dead_settings_keys_are_pruned(folder, monkeypatch):
    monkeypatch.setattr(dataupgrade, "DEAD_SETTINGS_KEYS", (("kopialens", "gone"),))
    (folder / "settings.json").write_text(json.dumps(
        {"last_repo": "1", "kopialens": {"theme": "ember", "gone": True}}), encoding="utf-8")
    removed = dataupgrade.prune_dead_settings()
    assert removed == ["kopialens.gone"]
    saved = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
    assert saved == {"last_repo": "1", "kopialens": {"theme": "ember"}}
    assert dataupgrade.prune_dead_settings() == []          # idempotent


def test_prune_is_a_noop_with_a_missing_or_broken_file(folder, monkeypatch):
    monkeypatch.setattr(dataupgrade, "DEAD_SETTINGS_KEYS", (("kopialens", "gone"),))
    assert dataupgrade.prune_dead_settings() == []
    (folder / "settings.json").write_text("{not json", encoding="utf-8")
    assert dataupgrade.prune_dead_settings() == []


def test_status_reports_pending_steps_before_the_startup_run(folder, monkeypatch):
    """The UI can poll this before the background thread has imported the step modules."""
    monkeypatch.setattr(dataupgrade, "DATA_VERSION", 2)
    dataupgrade.register(1, lambda: {}, "one-to-two")
    (folder / "data_version.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    pending = dataupgrade.status()["pending"]
    assert [p["name"] for p in pending] == ["one-to-two"]


def test_a_newer_folder_says_only_the_migration_is_refused(folder):
    """The wording must not over-promise: other parts of the app still write their own files."""
    (folder / "data_version.json").write_text(json.dumps({"version": 99}), encoding="utf-8")
    detail = dataupgrade.status()["detail"]
    assert "Nothing will be migrated" in detail and "version stamp is left untouched" in detail
    assert "Nothing in it will be written" not in detail


def test_the_shipped_steps_are_registered_and_bring_a_folder_to_this_version(
        folder, monkeypatch):
    """The shipped registry: an unversioned folder runs v1 -> v2 (encryption level),
    v2 -> v3 (accounts) and v3 -> v4 (table layouts out of settings), in order."""
    import importlib

    from app.core import encryption_upgrade, filecrypto, users, users_upgrade, uistate_upgrade
    monkeypatch.setattr(filecrypto, "upgrade_step", lambda: {"changed": ["stub"]})
    monkeypatch.setattr(users, "migrate_legacy", lambda: {"moved": False})
    importlib.reload(encryption_upgrade)          # re-register onto the fixture's empty registry
    importlib.reload(users_upgrade)
    importlib.reload(uistate_upgrade)
    assert dataupgrade.DATA_VERSION == 4
    out = dataupgrade.run_pending()
    assert out["version"] == 4
    assert [r["name"] for r in out["ran"]] == ["encryption-level", "accounts", "ui-state"]
    assert json.loads((folder / "data_version.json").read_text(encoding="utf-8"))["version"] == 4


def test_atomic_writers_leave_no_temp_and_replace_in_one_step(folder):
    target = folder / "x.json"
    dataupgrade.atomic_json(str(target), {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert not (folder / "x.json.tmp").exists()
    dataupgrade.atomic_json(str(target), {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    blob = folder / "y.bin"
    dataupgrade.atomic_bytes(str(blob), b"\x00\x01")
    assert blob.read_bytes() == b"\x00\x01"
    assert not (folder / "y.bin.tmp").exists()
