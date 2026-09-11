"""Frontend theme + job-chip invariants, pinned at source level (no JS test runner here).

The 2026-09-09 theming pass shipped with the System path untested: the valid-theme check did
not know "system", themeIsDark stripped the pre-paint dark class on a dark-mode OS, and an
explicit pick never refreshed the in-page quick-switch slots. The Settings panel also carried
a second Light/Dark toggle calling the same method as the nav button, and the batch Remove
button was still gated on the old .bat-era batch-count guard. Source-level pins for all of it
(the KL64 batch, 2026-09-12) - they catch the next edit that reverts any of these.
"""

from __future__ import annotations

import os

import config


def _read(*parts: str) -> str:
    with open(os.path.join(config.HERE, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_system_is_a_known_theme_and_resolves_through_the_os():
    src = _read("frontend", "js", "theme.js")
    assert 'id: "system"' in src, "System must be a first-class entry in THEMES"
    assert 'matchMedia("(prefers-color-scheme: dark)")' in src
    # themeIsDark must answer for "system" BEFORE the THEMES lookup (whose entry says dark: false).
    body = src[src.index("themeIsDark(id)"):]
    assert body.index('=== "system"') < body.index("_themeIsDark(t)")


def test_apply_theme_stamps_the_resolved_theme_not_the_literal():
    src = _read("frontend", "js", "theme.js")
    body = src[src.index("applyTheme(id)"):src.index("setTheme(id)")]
    assert "effective" in body and "this.darkTheme" in body and "this.lightTheme" in body


def test_an_explicit_pick_updates_the_quick_switch_slots():
    src = _read("frontend", "js", "theme.js")
    body = src[src.index("setTheme(id)"):src.index("quickTheme()")]
    assert "this.darkTheme = id" in body and "this.lightTheme = id" in body


def test_the_settings_panel_has_no_second_light_dark_toggle():
    src = _read("frontend", "partials", "settings.html")
    assert "toggleDark" not in src


def test_the_batch_remove_button_is_not_gated_on_batch_count():
    src = _read("frontend", "partials", "modals.html")
    assert "batches <= 1" not in src


def test_a_job_is_only_acknowledged_in_a_terminal_state():
    src = _read("frontend", "js", "jobs.js")
    open_body = src[src.index("async openJobPanel"):src.index("closeJob()")]
    assert 's.state !== "running"' in open_body
    apply_body = src[src.index("_jobApply(s) {"):src.index("jobView() {")]
    assert 's.state !== "running"' in apply_body
