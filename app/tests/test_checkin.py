"""Daily check-in: the switches decide what is sent, the update answer warns a lifetime licence
before a version it does not cover, and feature names come from what was used."""

from __future__ import annotations

import time

import pytest

from app.core import checkin

_real_stats_ready = checkin.stats_ready


@pytest.fixture
def sent(monkeypatch, tmp_path):
    monkeypatch.setattr(checkin, "STATE_FILE", str(tmp_path / "checkin.json"))
    # Any earlier API call in this process marks features used and the app opened.
    monkeypatch.setattr(checkin, "_used", set())
    monkeypatch.setattr(checkin, "_opened", False)
    monkeypatch.setattr(checkin, "DEMO_INSTANCE", False)
    monkeypatch.setattr(checkin, "_tier", lambda: "free")
    monkeypatch.setattr(checkin, "stats_ready", lambda: True)
    monkeypatch.delenv("KOPIALENS_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("KOPIALENS_NO_USAGE_STATS", raising=False)
    out = []
    monkeypatch.setattr(checkin, "_post", lambda payload: out.append(payload) or {"latest": "9.0.0"})
    return out


def _switches(monkeypatch, update_check, usage_stats):
    monkeypatch.setattr(checkin, "_kl", lambda: {"update_check": update_check, "usage_stats": usage_stats})


def test_with_both_switches_off_nothing_is_sent(monkeypatch, sent):
    _switches(monkeypatch, False, False)
    assert checkin.check_now() is False
    assert sent == []


def test_the_update_check_alone_sends_no_install_id(monkeypatch, sent):
    _switches(monkeypatch, True, False)
    checkin.check_now()
    assert set(sent[0]) == {"schema", "version", "install_type", "os", "channel"}


def test_usage_stats_send_feature_names_once(monkeypatch, sent):
    _switches(monkeypatch, True, True)
    checkin.note_request("/api/search/export")
    checkin.note_request("/api/prune/plan")
    checkin.note_feature("restore")
    checkin.check_now()
    checkin.check_now()
    assert sent[0]["features_used"] == ["csv_export", "prune", "restore"] and sent[0]["opened"] is True
    assert sent[1]["features_used"] == [] and sent[1]["opened"] is False
    assert sent[0]["install_id"] == sent[1]["install_id"]


def test_tray_polling_does_not_count_as_opening_the_app(monkeypatch, sent):
    _switches(monkeypatch, False, True)
    checkin.note_request("/api/shell/state")
    checkin.check_now()
    assert sent[0]["opened"] is False


def test_an_update_the_licence_does_not_cover_names_the_newest_covered_version(monkeypatch, sent):
    _switches(monkeypatch, True, False)
    monkeypatch.setattr(checkin, "APP_VERSION", "2.5.0")
    checkin._write_state(update_info={"latest": "3.0.1", "url": "https://x/3", "checked_at": time.time(),
                                      "latest_by_major": {"2": "2.9.3", "3": "3.0.1"}})
    from app.licensing import licence

    class Lifetime:
        updates_through = 2
    monkeypatch.setattr(licence, "current", lambda: Lifetime())
    st = checkin.status()
    assert st["available"] and st["latest"] == "3.0.1"
    assert st["licence"] == {"covers_through": 2, "latest_covered": "2.9.3"}


def test_usage_stats_wait_for_a_day_old_persistent_install(monkeypatch, sent):
    """A container recreated without a data volume gets a new id each time; it must never count."""
    from app.core import host
    monkeypatch.setattr(checkin, "stats_ready", _real_stats_ready)
    monkeypatch.setattr(host, "data_persistent", lambda: True)
    _switches(monkeypatch, True, True)
    checkin.check_now()
    assert "install_id" not in sent[-1]                       # first day: update check only
    checkin._write_state(install_created_at=time.time() - checkin.STATS_AFTER - 1)
    checkin.check_now()
    assert "install_id" in sent[-1]
    monkeypatch.setattr(host, "data_persistent", lambda: False)
    checkin.check_now()
    assert "install_id" not in sent[-1]


def test_check_now_compares_even_with_automatic_checks_off(monkeypatch, sent):
    _switches(monkeypatch, False, False)
    assert checkin.check_now(manual=True) is True
    assert set(sent[0]) == {"schema", "version", "install_type", "os", "channel"}
    assert checkin.status()["available"] is True
    assert checkin.check_now() is False and len(sent) == 1   # the daily check stays off


@pytest.mark.parametrize("status,tier", [
    ({"state": "none"}, "free"),
    ({"state": "expired", "plan": "Pro · Yearly"}, "free"),
    ({"state": "active", "plan": "Pro · Lifetime", "source": "polar"}, "pro-lifetime"),
    ({"state": "active", "plan": "Pro · Yearly", "source": "polar", "renewable": False}, "pro-yearly"),
    ({"state": "active", "plan": "Pro · Yearly", "source": "polar", "renewable": True}, "pro-yearly-sub"),
    ({"state": "active", "plan": "Business · Yearly", "source": "polar", "renewable": True}, "business-yearly-sub"),
    ({"state": "active", "plan": "Pro · Patreon", "source": "patreon", "renewable": True}, "pro-patreon"),
    ({"state": "active", "plan": "", "source": "manual"}, "pro"),
])
def test_usage_stats_name_the_plan(monkeypatch, status, tier):
    """Each plan reaches the stats as its own label, which the Worker's pattern accepts."""
    import re
    from app.licensing import licence
    monkeypatch.setattr(licence, "status", lambda: status)
    assert checkin._tier() == tier and re.fullmatch(r"[a-z0-9_-]{1,30}", tier)
