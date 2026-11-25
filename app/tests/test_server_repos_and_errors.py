"""Repos reached through a kopia server (browse-only), the snapshot folder browser, routes letting
the locked / not-indexed answers through, and removal clearing kopia's saved password."""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi import HTTPException

from app import main as m
from app.api import dashboard, simulate, snapshots
from app.core import auth, nightly, repoconnect, repos
from app.index import entries
from app.index.crypto import StoreLocked
from app.index.store import NoIndex


# ---- catch-all routes pass the special answers through ---------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(auth, "_STARTED", time.time())      # open setup window, no account
    return TestClient(m.app, base_url="http://127.0.0.1")


def _raiser(exc):
    def _f(*_a, **_k):
        raise exc
    return _f


@pytest.mark.parametrize("exc, status", [
    (StoreLocked("the index of repo 1 is locked"), 423),
    (NoIndex("repo '1' has no index (no manifest.json)"), 503),
    (FileNotFoundError("some unrelated file"), 500),
])
def test_catch_all_routes_let_locked_and_not_indexed_through(client, monkeypatch, exc, status):
    monkeypatch.setattr(dashboard, "table_rows", _raiser(exc))
    monkeypatch.setattr(simulate, "validate_retention", _raiser(exc))
    assert client.get("/api/dashboard?repo=1").status_code == status
    assert client.get("/api/validate?repo=1&source=x").status_code == status


# ---- which repos are reached through a server -------------------------------------------------------

def test_via_server_reads_the_connection_config(monkeypatch, tmp_path):
    cfg = tmp_path / "repo7.config"
    monkeypatch.setattr(repos, "exists", lambda r: True)
    monkeypatch.setattr(repos, "config_path_for", lambda r: str(cfg))
    cfg.write_text(json.dumps({"storage": {"type": "filesystem"}}))
    assert repos.via_server("7") is False
    cfg.write_text(json.dumps({"apiServer": {"url": "https://nas:51515"}}))
    later = time.time() + 5
    os.utime(cfg, (later, later))                       # a rewrite is seen despite the cache
    assert repos.via_server("7") is True
    assert repos.via_server("bz") is False              # Backblaze is never a kopia connection


def test_server_repos_are_never_indexed(monkeypatch):
    monkeypatch.setattr(repos, "via_server", lambda r: True)
    monkeypatch.setattr(nightly, "_reserve", _raiser(AssertionError("must not take the slot")))
    out = nightly.start_index_pass_async("1")
    assert out["started"] is False and "kopia server" in out["reason"]


# ---- folder reads -----------------------------------------------------------------------------------

class _Done:
    def __init__(self, stdout, rc=0):
        self.stdout, self.returncode = stdout, rc


def test_server_repos_read_folders_with_kopia_show(monkeypatch):
    calls = []
    listing = json.dumps({"entries": [{"name": "a.txt", "type": "f", "size": 3}]})
    monkeypatch.setattr(repos, "via_server", lambda r: True)
    monkeypatch.setattr(entries, "kopia", lambda repo, args: calls.append(args) or _Done(listing))
    out = entries._from_kopia("1", ["kserver-oid-1"])
    assert calls == [["show", "kserver-oid-1"]]          # `content show` is refused over a server
    assert out["kserver-oid-1"][0]["name"] == "a.txt"


def test_folder_reads_need_no_index(monkeypatch):
    listing = json.dumps({"entries": [{"name": "b.txt", "type": "f", "size": 1}]})
    monkeypatch.setattr(entries, "read_cursor", _raiser(NoIndex("repo '1' has no index")))
    monkeypatch.setattr(repos, "via_server", lambda r: False)
    monkeypatch.setattr(entries, "kopia", lambda repo, args: _Done(listing))
    assert entries.children_of("1", ["kno-index-oid"])["kno-index-oid"][0]["name"] == "b.txt"


# ---- the snapshot folder browser --------------------------------------------------------------------

SOURCE = "tester@kltest:C:\\data"


@pytest.fixture
def one_snapshot(monkeypatch):
    tree = {
        "root": [{"name": "zeta.txt", "type": "f", "size": 5, "mtime": "2026-09-30T10:00:00Z"},
                 {"name": "docs", "type": "d", "obj": "kdocs", "summ": {"size": 30, "files": 2}}],
        "kdocs": [{"name": "a.txt", "type": "f", "size": 10}, {"name": "b.txt", "type": "f", "size": 20}],
    }
    snap = {"id": "s1", "startTime": "2026-09-30T10:00:00Z", "rootEntry": {"obj": "root"}}
    monkeypatch.setattr(snapshots, "sources_index", lambda repo: {SOURCE: [snap]})
    monkeypatch.setattr(entries, "children_of", lambda repo, oids: {o: tree[o] for o in oids if o in tree})


def test_browse_lists_one_folder_folders_first(one_snapshot):
    top = snapshots.browse("1", SOURCE, "s1")
    assert [e["name"] for e in top["entries"]] == ["docs", "zeta.txt"]
    assert top["entries"][0]["size"] == 30 and top["entries"][0]["files"] == 2
    docs = snapshots.browse("1", SOURCE, "s1", top["entries"][0]["full_path"])
    assert [e["full_path"] for e in docs["entries"]] == ["C:\\data\\docs\\a.txt", "C:\\data\\docs\\b.txt"]
    assert [c["name"] for c in docs["crumbs"]] == ["C:\\data", "docs"]


def test_browse_refuses_a_file_or_a_gone_snapshot(one_snapshot):
    with pytest.raises(HTTPException) as file_err:
        snapshots.browse("1", SOURCE, "s1", "C:\\data\\zeta.txt")
    assert file_err.value.status_code == 404
    with pytest.raises(HTTPException) as gone:
        snapshots.browse("1", SOURCE, "expired")
    assert gone.value.status_code == 404


def test_browse_reports_an_unreadable_folder_on_the_way_down(one_snapshot, monkeypatch):
    monkeypatch.setattr(entries, "children_of", lambda repo, oids: {"root": [
        {"name": "docs", "type": "d", "obj": "kdocs"}]} if oids == ["root"] else {})
    with pytest.raises(HTTPException) as err:
        snapshots.browse("1", SOURCE, "s1", "C:\\data\\docs\\deeper")
    assert err.value.status_code == 502


# ---- removing a connection KopiaLens made clears kopia's saved password ------------------------------

@pytest.fixture
def removable(monkeypatch):
    from app.core import audit
    from app.index import store
    rec = {"id": "7", "name": "Seven", "config_file": "C:\\cfg\\repo7.config", "source": "created"}
    calls = {"disconnect": [], "removed": []}
    monkeypatch.setattr(repos, "all_records", lambda: [rec])
    monkeypatch.setattr(repos, "remove_record", lambda r: None)
    monkeypatch.setattr(repos, "data_paths", lambda r: [])
    monkeypatch.setattr(repos, "forget_state", lambda r: False)
    monkeypatch.setattr(store, "close_repo", lambda r: {})
    monkeypatch.setattr(nightly, "is_running", lambda r: False)
    monkeypatch.setattr(audit, "audit", lambda *a, **k: None)
    monkeypatch.setattr(repoconnect, "is_managed_config", lambda p: True)
    monkeypatch.setattr(repoconnect, "_disconnect", lambda repo, cfg: calls["disconnect"].append(cfg))
    monkeypatch.setattr(repoconnect, "_remove_config_files", lambda cfg: calls["removed"].append(cfg))
    return rec, calls


@pytest.mark.parametrize("source, disconnects", [("created", True), ("connected", True), ("attached", False)])
def test_remove_disconnects_only_connections_kopialens_made(removable, source, disconnects):
    rec, calls = removable
    rec["source"] = source
    repoconnect.remove("7", delete_config=True)
    assert bool(calls["disconnect"]) is disconnects
    assert calls["removed"] == [rec["config_file"]]


def test_keeping_the_config_keeps_the_connection(removable):
    _rec, calls = removable
    repoconnect.remove("7", delete_config=False)
    assert calls == {"disconnect": [], "removed": []}


def test_disconnect_runs_kopias_own_disconnect(monkeypatch, tmp_path):
    cfg = tmp_path / "repo7.config"
    cfg.write_text("{}")
    ran = []
    monkeypatch.setattr(repoconnect, "_run", lambda args, pw, **k: ran.append(args) or {"returncode": 0})
    repoconnect._disconnect("7", str(tmp_path / "missing.config"))
    repoconnect._disconnect("7", str(cfg))
    assert len(ran) == 1               # a missing config has nothing to disconnect
    assert ran[0][1:] == ["--config-file", str(cfg), "repository", "disconnect"]
