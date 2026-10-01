"""Onboarding and repositories: when onboarding shows, the closed-setup gate, the "off" and
"added after this slot" scheduling rules, the first-index queue, repo display order, the summary."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from app import main as m
from app.api import dashboard, onboarding, setup
from app.core import auth, reindexq, repoconnect, repos


@pytest.fixture
def records(monkeypatch):
    """A settable record list and kopialens settings block."""
    state = {"repos": [], "kl": {}}
    monkeypatch.setattr(repos, "all_records", lambda: state["repos"])
    monkeypatch.setattr(onboarding, "raw_kopialens_settings", lambda: state["kl"])

    def _mutate(fn):
        s = {"kopialens": state["kl"]}
        fn(s)
        state["kl"] = s["kopialens"]
        return s

    monkeypatch.setattr(onboarding, "mutate_settings", _mutate)
    return state


# ---- when onboarding shows ------------------------------------------------------------------------

def test_onboarding_shows_until_a_kopia_repo_exists(records):
    assert onboarding.needed() is True
    records["repos"] = [{"id": "bz", "kind": "backblaze"}]
    assert onboarding.needed() is True          # Backblaze alone is not a kopia repository
    records["repos"].append({"id": "1", "kind": "kopia"})
    assert onboarding.needed() is False         # an existing install never sees it


def test_a_started_onboarding_resumes_until_finished(records):
    onboarding.begin()
    records["repos"] = [{"id": "1", "kind": "kopia"}]
    assert onboarding.needed() is True          # first repo added, then the page reloads
    onboarding.finish()
    assert onboarding.needed() is False and records["kl"]["onboarded"] is True


def test_finishing_needs_a_repository(records):
    with pytest.raises(Exception):
        onboarding.finish()


# ---- setup closed: the API needs the setup code ---------------------------------------------------

@pytest.fixture
def closed_setup(monkeypatch, tmp_path):
    from app.core import users
    kl: dict = {}
    monkeypatch.setattr(auth, "_kl", lambda: kl)
    monkeypatch.setattr(auth, "raw_kopialens_settings", lambda: kl)
    monkeypatch.setattr(auth, "_env", lambda name: "")
    monkeypatch.setattr(auth, "_STARTED", time.time() - auth.SETUP_WINDOW - 1)
    monkeypatch.setattr(auth, "_SETUP_CODE", "")
    assert not users.has_accounts()
    return kl


def test_a_closed_setup_window_blocks_the_api_until_the_code_is_entered(closed_setup):
    from starlette.testclient import TestClient
    client = TestClient(m.app, base_url="http://127.0.0.1")
    blocked = client.get("/api/repos")
    assert blocked.status_code == 403 and blocked.json()["setup_code"] is True
    state = client.get("/api/onboarding/state").json()
    assert state["setup_unlocked"] is False and "repos" not in state
    assert client.post("/api/onboarding/setup-code", json={"code": "WRONG-CODE"}).status_code == 403
    ok = client.post("/api/onboarding/setup-code", json={"code": auth.setup_code()})
    assert ok.status_code == 200 and auth.SETUP_COOKIE in ok.headers["set-cookie"]
    assert client.get("/api/repos").status_code == 200
    assert client.get("/api/onboarding/state").json()["setup_unlocked"] is True


def test_the_setup_cookie_also_opens_the_account_step(closed_setup):
    assert auth.first_run_allowed("", auth.setup_cookie_value()) is True
    assert auth.first_run_allowed("", "forged") is False


def test_an_open_setup_window_needs_no_code(closed_setup, monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(auth, "_STARTED", time.time())
    client = TestClient(m.app, base_url="http://127.0.0.1")
    assert client.get("/api/repos").status_code == 200


# ---- scheduling -----------------------------------------------------------------------------------

@pytest.fixture
def daily(monkeypatch):
    monkeypatch.setattr(m, "_index_time", lambda: (23, 0))
    state = {"mode": "daily", "last": 0.0, "created": 0.0}
    monkeypatch.setattr(m, "_index_mode", lambda: state["mode"])
    monkeypatch.setattr(m, "nightly_ts", lambda repo: state["last"])
    monkeypatch.setattr(m, "_repo_created", lambda repo: state["created"])
    return state


def _due(now: dt.datetime) -> bool:
    return m._index_due("7", now, now.timestamp())


def test_a_repo_added_after_the_slot_waits_for_the_next_one(daily):
    daily["created"] = dt.datetime(2026, 9, 30, 23, 20).timestamp()
    assert _due(dt.datetime(2026, 9, 30, 23, 25)) is False    # added after tonight's 23:00
    assert _due(dt.datetime(2026, 10, 1, 23, 1)) is True      # tomorrow's slot


def test_a_repo_added_during_the_day_runs_tonight(daily):
    daily["created"] = dt.datetime(2026, 9, 30, 15, 0).timestamp()
    assert _due(dt.datetime(2026, 9, 30, 16, 0)) is False
    assert _due(dt.datetime(2026, 9, 30, 23, 1)) is True


def test_off_never_schedules(daily):
    daily["mode"] = "off"
    assert _due(dt.datetime(2026, 9, 30, 23, 1)) is False


def test_off_is_a_valid_mode(monkeypatch):
    monkeypatch.setattr(m, "_kl", lambda: {"index_mode": "off"})
    assert m._index_mode() == "off"
    monkeypatch.setattr(m, "_kl", lambda: {"index_mode": "bogus"})
    assert m._index_mode() == "daily"


# ---- first-index queue ----------------------------------------------------------------------------

@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setattr(reindexq, "_PENDING", {})
    monkeypatch.setattr(reindexq, "_path", lambda: str(tmp_path / "reindex_pending.json"))
    monkeypatch.setattr(reindexq, "_ensure_thread", lambda: None)
    monkeypatch.setattr(reindexq, "delay", lambda: 300)


def test_a_first_index_is_queued_with_no_wait_and_labelled(queue):
    reindexq.request("4", now=1000, wait=0, reason="first")
    reindexq.request("5", now=1000)
    rows = {r["repo"]: r for r in reindexq.pending()}
    assert rows["4"]["due"] == 1000 and rows["4"]["reason"] == "first"
    assert rows["5"]["due"] == 1300 and rows["5"]["reason"] == "delete"


def test_start_indexing_queues_every_new_repo_and_starts_the_first(queue, monkeypatch):
    monkeypatch.setattr(repos, "exists", lambda r: r in ("4", "5"))
    started = []
    monkeypatch.setattr(reindexq, "run_now", lambda r: started.append(r) or {"started": True})
    out = onboarding.start_indexing(onboarding.StartRequest(repos=["4", "5", "gone"]))
    assert out["queued"] == ["4", "5"] and started == ["4"]
    assert {r["repo"] for r in reindexq.pending()} == {"4", "5"}


# ---- display order and the Connected summary ------------------------------------------------------

def test_repos_follow_the_chosen_order_and_unnamed_ones_keep_their_place():
    rows = [{"id": "1"}, {"id": "2"}, {"id": "3"}, {"id": "bz"}]
    assert [r["id"] for r in dashboard._ordered(rows, ["3", "1"])] == ["3", "1", "2", "bz"]
    assert [r["id"] for r in dashboard._ordered(rows, [])] == ["1", "2", "3", "bz"]


def test_summary_counts_sources_computers_and_snapshots(monkeypatch):
    from app.core import sources
    snaps = [
        {"source": {"host": "pc", "userName": "a", "path": "C:\\"}, "endTime": "2026-09-01T00:00:00Z"},
        {"source": {"host": "pc", "userName": "a", "path": "C:\\"}, "endTime": "2026-09-30T00:00:00Z"},
        {"source": {"host": "nas", "userName": "b", "path": "/data"}, "endTime": "2026-09-15T00:00:00Z"},
    ]
    monkeypatch.setattr(repos, "exists", lambda r: True)
    monkeypatch.setattr(sources, "all_snapshots", lambda r: snaps)
    out = repoconnect.summary("4")
    est = out.pop("estimate")
    assert out == {"repo": "4", "snapshots": 3, "sources": 2, "hosts": 2, "newest": "2026-09-30T00:00:00Z"}
    assert est["seconds"][0] <= est["seconds"][1] and est["bytes"][0] <= est["bytes"][1]


def test_backblaze_takes_a_name_and_short_name(records, monkeypatch):
    records["repos"] = [{"id": "bz", "kind": "backblaze", "name": "Backblaze Personal Backup"}]
    monkeypatch.setattr(repos.config, "save_records", lambda *a, **kw: None)
    monkeypatch.setattr(repos.config, "reload_repos", lambda: None)
    monkeypatch.setattr(repos, "detail", lambda rec: dict(rec))
    out = setup.edit("bz", setup.RepoEditRequest(short_name="BZ", name="Backblaze"))
    assert out["repo"]["short_name"] == "BZ" and out["repo"]["name"] == "Backblaze"
