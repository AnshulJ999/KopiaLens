"""Data format 4 -> 5: settings.json keeps only values that differ from the defaults.

Older versions saved values the user never chose; while they stay, a changed default never reaches
that install. Removing a value equal to its default changes nothing on screen.
"""

from __future__ import annotations

import json

from config import SETTINGS_FILE

from . import dataupgrade
from .filelock import locked

# Defaults that changed: a saved copy of the old one was never chosen, so it goes too.
_OLD_DEFAULTS = {("kopialens", "light_interval"): 1800}


def trim() -> dict:
    from ..settings import drop_defaults
    with locked(SETTINGS_FILE):          # the same lock every settings writer holds
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"removed": 0}
        if not isinstance(data, dict):
            return {"removed": 0}
        removed = drop_defaults(data)
        for (block, key), old in _OLD_DEFAULTS.items():
            target = data.get(block)
            if isinstance(target, dict) and key in target and target[key] == old:
                del target[key]
                removed.append(f"{block}.{key}")
        if removed:
            dataupgrade.atomic_json(SETTINGS_FILE, data)
    return {"removed": len(removed)}


dataupgrade.register(from_version=4, fn=trim, name="settings-defaults",
                     description="settings.json keeps only values that differ from the defaults")
