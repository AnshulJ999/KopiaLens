"""The shipped stylesheet must match a fresh build of the current UI files.

frontend/css/tailwind.css only holds the classes the build found in the partials and JS; a class
added after the last build is silently unstyled. Edits that add no class leave the build unchanged.
"""
import importlib.util
import os

import pytest

_KL = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_css():
    spec = importlib.util.spec_from_file_location("build_css", os.path.join(_KL, "packaging", "build_css.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_compiled_css_matches_a_fresh_build():
    current = _build_css().is_current()
    if current is None:
        pytest.skip("Tailwind CLI not in tools/ - cannot rebuild the stylesheet here")
    assert current, "frontend/css/tailwind.css is out of date with the UI files: run python packaging/build_css.py"
