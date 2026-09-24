"""Setup routes + the API contract they hold to (/ backend).

Two invariants live here beyond the routes themselves: no route or request model may default
`repo` to a personal id ("1"), and no route may spawn kopia unless the user asked for it.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest
from fastapi import HTTPException

from app.api import dashboard as dashboard_api
from app.api import setup
from app.core import kopiabin, repoconnect
from app.core import repos as repo_store
from config import REPOS

_APP = pathlib.Path(repo_store.__file__).resolve().parents[1]


def test_no_route_or_model_defaults_repo_to_a_personal_id():
    """The API must not assume a repo called "1" exists: an install without it answered every
    request that omitted `repo` with "unknown repo: 1"."""
    offenders = []
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP).as_posix()
        if rel.startswith("tests/"):
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if 'repo: str = "1"' in line:
                offenders.append(f"{rel}:{num}")
    assert not offenders, f"these still default repo to '1': {offenders}"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Record store + settings file + kopia config dir on scratch, REPOS restored afterwards."""
    from app import settings as settings_mod

    monkeypatch.setattr("config.REPOS_FILE", str(tmp_path / "repos.json"))
    monkeypatch.setattr("config.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("config.CACHEDIR", str(tmp_path / "cache"))
    monkeypatch.setattr("config.RECORDS", {"version": 1, "cache_dir": None, "repos": []})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr("app.core.audit._path", lambda: str(tmp_path / "audit.jsonl"))
    cfgdir = tmp_path / "kopia"
    cfgdir.mkdir()
    monkeypatch.setattr(repoconnect, "kopia_config_dir", lambda: str(cfgdir))
    monkeypatch.setattr(repo_store, "capture_status", lambda repo, status=None: {})
    monkeypatch.delenv(kopiabin.ENV_OVERRIDE, raising=False)
    snapshot = dict(REPOS)
    kopiabin.invalidate()
    try:
        yield cfgdir
    finally:
        REPOS.clear()
        REPOS.update(snapshot)
        kopiabin.invalidate()


def test_kopia_block_reports_the_missing_binary(sandbox, monkeypatch):
    monkeypatch.setattr(kopiabin, "_usual_locations", lambda: [])
    monkeypatch.setattr(kopiabin, "_kopiaui_bundles", lambda: [])
    monkeypatch.setattr(kopiabin, "_bundled_copies", lambda: [])
    monkeypatch.setattr(kopiabin.shutil, "which", lambda _n: None)
    kopiabin.invalidate()
    out = setup.get_kopia()
    assert out["available"] is False and out["detail"]
    assert out["tested"] == kopiabin.TESTED_VERSION


def test_kopia_route_lists_every_copy_found(sandbox, monkeypatch):
    found = [{"path": "/bundled/kopia", "source": "bundled", "version": "0.23.1",
              "in_use": True, "newest": False},
             {"path": "/usr/bin/kopia", "source": "PATH", "version": "0.24.1",
              "in_use": False, "newest": True}]
    monkeypatch.setattr(kopiabin, "inventory", lambda: found)
    out = setup.get_kopia()
    assert out["found"] == found
    assert out["searched"]


def test_data_status_route_reports_versions_and_index_states(sandbox, monkeypatch):
    from app.core import dataupgrade
    from app.index import store as index_store
    monkeypatch.setattr(dataupgrade, "VERSION_FILE", str(sandbox.parent / "data_version.json"))
    # store binds DATA_DIR at import, so the index paths are redirected directly
    monkeypatch.setattr(index_store, "manifest_path",
                        lambda repo: str(sandbox.parent / f"repo{repo}" / "manifest.json"))
    repo_store.add_record({"name": "One", "config_file": None})
    out = setup.get_data_status()
    assert out["version"] == 1 and out["app_version"] == dataupgrade.DATA_VERSION
    assert out["newer"] is False
    assert out["index"] == [{"repo": "1", "found": None, "expected": 3, "state": "absent"}]


def test_index_rebuild_preview_and_action(sandbox, monkeypatch, tmp_path):
    """The way out of a downgrade: set the newer generation aside (never delete it) and rebuild
    with THIS version, stating the cost first."""
    from app.index import store as index_store
    from app.index import upgrade as index_upgrade

    repo_store.add_record({"name": "One", "config_file": None})
    root = tmp_path / "data" / "repo1"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"generation": 1,
                                                    "schema_version": index_upgrade.MANIFEST_VERSION + 1,
                                                    "artifacts": {}}), encoding="utf-8")
    monkeypatch.setattr(index_store, "repo_dir", lambda repo: str(root))
    monkeypatch.setattr(index_store, "manifest_path", lambda repo: str(root / "manifest.json"))
    monkeypatch.setattr("app.index.publish.repo_dir", lambda repo: str(root))

    preview = setup.index_rebuild_preview("1")
    assert preview["state"] == "newer" and preview["can_rebuild"] is True
    assert "10-40 min" in preview["cost"] and "never deleted" in preview["keeps"]

    with pytest.raises(HTTPException) as exc:
        setup.index_rebuild(setup.IndexRebuildRequest(repo="1"))
    assert exc.value.status_code == 400 and "confirm=true" in exc.value.detail

    calls = []
    from app.core import nightly
    monkeypatch.setattr(nightly, "start_index_pass_async",
                        lambda repo, trigger=None, **kw: calls.append((repo, trigger)) or {"started": True})
    out = setup.index_rebuild(setup.IndexRebuildRequest(repo="1", confirm=True))
    assert out["ok"] is True and calls == [("1", "explicit-rebuild")]
    assert out["archived_to"].startswith(str(root)) and "v4-" in out["archived_to"]
    assert not root.exists() and os.path.isdir(out["archived_to"])       # kept, not deleted
    assert setup.index_rebuild_preview("1")["state"] == "absent"          # fresh rebuild next


def test_index_rebuild_refuses_a_current_index(sandbox, monkeypatch, tmp_path):
    from app.index import store as index_store
    from app.index import upgrade as index_upgrade
    repo_store.add_record({"name": "One", "config_file": None})
    root = tmp_path / "data" / "repo1"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"schema_version": index_upgrade.MANIFEST_VERSION}),
                                        encoding="utf-8")
    monkeypatch.setattr(index_store, "repo_dir", lambda repo: str(root))
    monkeypatch.setattr(index_store, "manifest_path", lambda repo: str(root / "manifest.json"))
    with pytest.raises(HTTPException) as exc:
        setup.index_rebuild(setup.IndexRebuildRequest(repo="1", confirm=True))
    assert "no index to rebuild" in exc.value.detail


def test_the_mismatch_payload_says_upgrading_only_when_work_is_under_way(monkeypatch):
    """Reads during an upgrade must show progress, not an error - and a downgrade must be an
    explicit hard stop with a way out."""
    from app.index.upgrade import IndexVersionMismatch
    import app.main as main_mod

    exc = IndexVersionMismatch("1", 2, 3)
    monkeypatch.setattr(main_mod.index_upgrade, "queued_rebuilds", lambda: [])
    monkeypatch.setattr(main_mod.nightly, "is_running", lambda repo: False)
    payload = main_mod._index_mismatch_payload(exc)
    assert payload["mode"] == "rebuild" and payload["upgrading"] is False
    monkeypatch.setattr(main_mod.index_upgrade, "queued_rebuilds", lambda: ["1"])
    payload = main_mod._index_mismatch_payload(exc)
    assert payload["upgrading"] is True and "being upgraded" in payload["detail"]

    newer = IndexVersionMismatch("1", 4, 3)
    payload = main_mod._index_mismatch_payload(newer)
    assert payload["mode"] == "newer" and payload["upgrading"] is False
    assert "newer KopiaLens" in payload["detail"]
    out = dashboard_api.repos()
    assert out["data"]["app_version"] >= 1
    assert out["kopia"]["tested"] == kopiabin.TESTED_VERSION
    assert "repos" in out and "settings" in out


def test_set_kopia_rejects_junk_and_accepts_a_runnable_program(sandbox, tmp_path, monkeypatch):
    junk = tmp_path / "not-kopia.exe"
    junk.write_text("x", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        setup.set_kopia(setup.KopiaPathRequest(path=str(junk)))
    assert exc.value.status_code == 400

    # a file that runs but does not report a kopia version is refused too
    with pytest.raises(HTTPException) as exc:
        setup.set_kopia(setup.KopiaPathRequest(path=sys.executable))
    assert exc.value.status_code == 400 and "kopia version" in exc.value.detail

    # a real kopia binary passes the check and becomes the resolved program
    monkeypatch.setattr(kopiabin, "check",
                        lambda path: {"path": path, "ok": True, "version": "0.23.1", "detail": ""})
    monkeypatch.setattr(kopiabin, "version", lambda force=False: "0.23.1")
    out = setup.set_kopia(setup.KopiaPathRequest(path=sys.executable))
    assert out["available"] is True
    assert out["path"] == os.path.abspath(sys.executable)
    assert out["source"] == "setting"
    assert out["version"] == "0.23.1"
    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings["kopialens"]["kopia_path"] == os.path.abspath(sys.executable)


def test_set_kopia_with_an_empty_path_returns_to_automatic(sandbox):
    out = setup.set_kopia(setup.KopiaPathRequest(path=""))
    assert "searched" in out and out["source"] != "setting"


def test_providers_and_detect_configs_routes(sandbox):
    providers = setup.get_providers()["providers"]
    assert {p["key"] for p in providers} >= {"filesystem", "s3", "rclone", "server", "token"}
    (sandbox / "repository.config").write_text(json.dumps({"storage": {"type": "filesystem"}}),
                                               encoding="utf-8")
    out = setup.detect_configs()
    assert out["dir"] == str(sandbox)
    assert [c["filename"] for c in out["configs"]] == ["repository.config"]
    assert out["configs"][0]["in_store"] is False


def test_route_error_mapping_is_a_400(sandbox, monkeypatch):
    monkeypatch.setattr(repoconnect, "probe", lambda config_file=None, repo=None: (_ for _ in ()).throw(
        ValueError("config file not found: x")))
    with pytest.raises(HTTPException) as exc:
        setup.probe(setup.ProbeRequest(config_file="x"))
    assert exc.value.status_code == 400 and "config file not found" in exc.value.detail

    monkeypatch.setattr(repoconnect, "connect", lambda *a, **k: (_ for _ in ()).throw(
        ValueError("No such bucket")))
    with pytest.raises(HTTPException) as exc:
        setup.connect(setup.ConnectRequest(provider="b2", fields={}))
    assert exc.value.status_code == 400

    monkeypatch.setattr(repoconnect, "attach", lambda *a, **k: (_ for _ in ()).throw(
        ValueError("already attached as repo 1")))
    with pytest.raises(HTTPException) as exc:
        setup.attach(setup.AttachRequest(config_file="x"))
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        setup.remove("1")
    assert exc.value.status_code == 400 and "confirm=true" in exc.value.detail


def test_connect_attach_edit_remove_round_trip(sandbox, monkeypatch):
    """One pass over the whole lifecycle with kopia at the boundary faked."""
    cfgdir = sandbox
    external = cfgdir / "repository-9.config"
    external.write_text(json.dumps({"storage": {"type": "s3", "config": {"bucket": "b"}}}),
                        encoding="utf-8")

    monkeypatch.setattr(repoconnect, "_run", lambda args, password, timeout=600.0: {
        "returncode": 0, "stdout": "Connected to repository.", "stderr": "", "command": args})
    rec = setup.connect(setup.ConnectRequest(provider="filesystem", fields={"path": "/repo"},
                                            password="pw", name="Connected", short_name="C"))["repo"]
    assert rec["id"] == "1" and rec["source"] == "connected"

    att = setup.attach(setup.AttachRequest(config_file=str(external), name="Attached"))["repo"]
    assert att["id"] == "2" and att["source"] == "attached"
    assert att["storage"]["kind"] == "cloud"

    edited = setup.edit("2", setup.RepoEditRequest(short_name="AT", counts_as_backup=False))["repo"]
    assert edited["short_name"] == "AT" and edited["counts_as_backup"] is False
    assert REPOS["2"]["counts_as_backup"] is False

    assert setup.remove_preview("2")["paths"] == []
    out = setup.remove("2", confirm=True)
    assert out["ok"] and [r["id"] for r in repo_store.all_records()] == ["1"]

    with pytest.raises(HTTPException):
        setup.edit("2", setup.RepoEditRequest(name="gone"))
