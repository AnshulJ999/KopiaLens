"""The shipped stylesheet must be built from the current UI files.

frontend/css/tailwind.css only holds the classes the build found in the partials and JS; a class
added after the last build is silently unstyled. The build stamps a digest of its inputs.
"""
import importlib.util
import os

_KL = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_css():
    spec = importlib.util.spec_from_file_location("build_css", os.path.join(_KL, "packaging", "build_css.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_compiled_css_matches_the_ui_files():
    mod = _build_css()
    assert mod.built_digest() == mod.input_digest(), (
        "frontend/css/tailwind.css is out of date with the UI files: run python packaging/build_css.py")
