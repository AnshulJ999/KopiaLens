"""Onboarding end to end, on a fresh server: the found-connection path with protection and a first
index, and the create path that skips both."""

from __future__ import annotations

from playwright.sync_api import expect

from . import _harness as h


def _footer(page, label: str):
    return page.locator(".kl-footer button", has_text=label)


def test_attach_found_repository_protect_and_index(fresh_server, open_page):
    page = open_page(fresh_server, signed_in=False)
    page.goto(fresh_server.url + "/")

    expect(page.get_by_role("button", name="Try with sample data")).to_be_disabled()
    page.locator("label", has_text="repository.config").locator("input[type=checkbox]").check()
    page.get_by_role("button", name="Use this repository").click()
    expect(page.get_by_text("1 source · 3 snapshots", exact=False)).to_be_visible(timeout=60_000)
    _footer(page, "Continue").click()

    expect(page.get_by_role("textbox", name="Username")).to_have_value(h.USERNAME)
    page.get_by_role("textbox", name="KopiaLens password", exact=True).fill(h.PASSWORD)
    page.get_by_label("Confirm password").fill(h.PASSWORD)
    page.get_by_label("Unlock automatically on this computer").uncheck()
    _footer(page, "Continue").click()

    expect(page.get_by_text("Save your recovery key")).to_be_visible(timeout=30_000)
    continue_btn = page.locator(".kl-footer button:visible", has_text="Continue")
    expect(continue_btn).to_be_disabled()
    page.get_by_label("I've saved my recovery key").check()
    continue_btn.click()

    page.get_by_role("button", name="Start indexing").click()
    expect(page.get_by_text("Building the first index…")).to_be_visible(timeout=30_000)
    # The app lands on the dashboard and fills it in by itself once the first index is published.
    expect(page.get_by_role("row", name="3 33 35", exact=False)).to_be_visible(timeout=120_000)

    fresh_server.sign_in(h.USERNAME, h.PASSWORD)
    account = fresh_server.get("/api/account")
    assert account["sign_in_on"] and account["unlocks_encryption"]
    assert account["encryption"]["enabled"]


def test_create_repository_then_skip_protection_and_indexing(fresh_server, open_page, tmp_path):
    page = open_page(fresh_server, signed_in=False)
    page.goto(fresh_server.url + "/")

    page.get_by_role("button", name="Create a new repository").click()
    page.get_by_role("textbox", name="Folder The folder").fill(str(tmp_path / "new-repo"))
    page.get_by_label("Repository password").first.fill("new repo password")
    page.get_by_role("button", name="Create repository").click()
    expect(page.get_by_text("No snapshots yet")).to_be_visible(timeout=60_000)
    _footer(page, "Continue").click()

    page.get_by_role("button", name="Skip protection").click()
    page.get_by_role("button", name="Continue without a password").click()

    _footer(page, "Skip for now").click()
    page.get_by_role("button", name="Skip for now").last.click()

    expect(page.get_by_text("This repository isn't indexed yet")).to_be_visible(timeout=30_000)
    assert fresh_server.get("/api/onboarding/state")["needed"] is False
