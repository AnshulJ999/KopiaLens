"""Developer add-ons: extra tools loaded only when running from source with an explicit switch.

``KOPIALENS_DEV_ADDONS`` (environment, else a git-ignored ``.env.dev`` beside ``main.py``) lists
add-on folders separated by ";". Each folder holds ``kopialens_addon.py`` with
``register(app, add_html, root)``. Compiled builds never load add-ons.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from config import COMPILED

from .applog import get_logger

log = get_logger("devaddons")

ENV = "KOPIALENS_DEV_ADDONS"
HTML: list[str] = []   # extra markup placed before </body> on every page


def _setting(root: Path) -> str:
    value = os.environ.get(ENV)
    if value is not None:
        return value
    try:
        lines = (root / ".env.dev").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        key, sep, val = line.partition("=")
        if sep and key.strip() == ENV:
            return val.strip().strip('"')
    return ""


def load(app, root: Path) -> None:
    if COMPILED:
        return
    for i, folder in enumerate(p.strip() for p in _setting(root).split(";")):
        if not folder:
            continue
        entry = Path(folder) / "kopialens_addon.py"
        if not entry.is_file():
            log.warning("dev add-on not found: %s", entry)
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"kopialens_dev_addon_{i}", entry)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.register(app=app, add_html=HTML.append, root=root)
            log.info("dev add-on loaded: %s", folder)
        except Exception:
            log.exception("dev add-on failed to load: %s", folder)
