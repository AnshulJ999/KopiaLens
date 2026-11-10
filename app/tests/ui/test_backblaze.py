"""The Backblaze screens, on fake Windows and fake Mac data (the first browser flow for this side).

The demo generator makes the data; the real server indexes it; the test opens every Backblaze tab
and fails on an empty drive table, an empty Tree, or any page error. That is the check the empty
Drive column on 2026-11-10 would have caught.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect

from app.core import demobz

from . import _harness as h

BZ_TABS = ["Dashboard", "Search", "Tree", "Excluded", "Exclusions", "Churn", "Queue", "Sessions", "Health"]


def _idle(page) -> None:
    page.wait_for_function("() => !Alpine.$data(document.getElementById('app')).busy")


def _add_backblaze(srv: h.Server, tmp_path: Path, *, platform: str, host: str) -> None:
    root = tmp_path / f"bzdata-{platform}"
    demobz.write(str(root), [], user="demo", host=host, seed=3, platform=platform, profile="test")
    srv.post("/api/setup/backblaze/folder", {"path": str(root)})
    srv.post("/api/setup/backblaze", {"index": False})
    srv.post("/api/backblaze/refresh?force=true")
    deadline = 120
    import time
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        st = srv.get("/api/backblaze/status")
        if st.get("built") and not st.get("building"):
            return
        time.sleep(0.5)
    raise AssertionError(f"Backblaze did not build: {srv.tail()}")


def _open_bz(page) -> None:
    page.wait_for_function("() => (Alpine.$data(document.getElementById('app')).repos || []).some((r) => r.id === 'bz')",
                           timeout=30_000)
    page.evaluate("() => Alpine.$data(document.getElementById('app')).pickRepo('bz')")
    page.wait_for_function("() => Alpine.$data(document.getElementById('app')).repo === 'bz'")
    expect(page.locator("#bzDashTable .tabulator-row").first).to_be_visible(timeout=30_000)


def _every_tab(page) -> None:
    for label in BZ_TABS:
        h.go_tab(page, label)
        _idle(page)
        expect(page.locator("span.text-danger[x-text='error']")).to_be_hidden()
    h.go_tab(page, "Dashboard")
    _idle(page)


def test_fake_windows_data_fills_every_backblaze_tab(indexed_server, open_page, tmp_path):
    _add_backblaze(indexed_server, tmp_path, platform="windows", host="DEMO-PC")
    page = open_page(indexed_server)
    _open_bz(page)
    rows = page.locator("#bzDashTable .tabulator-row")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("C:")
    h.go_tab(page, "Tree")
    expect(page.locator("#bzTreeTable .tabulator-row").first).to_be_visible(timeout=30_000)
    expect(page.locator("#bzTreeTable")).not_to_contain_text("undefined")
    _every_tab(page)


def test_fake_mac_data_fills_every_backblaze_tab(indexed_server, open_page, tmp_path):
    _add_backblaze(indexed_server, tmp_path, platform="mac", host="studio-mac")
    page = open_page(indexed_server)
    _open_bz(page)
    rows = page.locator("#bzDashTable .tabulator-row")
    expect(rows).to_have_count(2)
    expect(page.locator("#bzDashTable")).to_contain_text("/Volumes/Archive")
    h.go_tab(page, "Tree")
    expect(page.locator("#bzTreeTable .tabulator-row").first).to_be_visible(timeout=30_000)
    expect(page.locator("#bzTreeTable")).not_to_contain_text("undefined")
    _every_tab(page)
