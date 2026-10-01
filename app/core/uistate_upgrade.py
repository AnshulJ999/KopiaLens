"""Data-folder step v3 -> v4: table layouts move from settings.json into ui_state.json."""

import json
import os

from config import SETTINGS_FILE

from . import dataupgrade, uistate
from .filelock import locked


def move_tables() -> dict:
    """Copy first, then remove from settings: a stop in between leaves a duplicate, never a loss."""
    with locked(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"moved": 0}
        kl = data.get("kopialens") if isinstance(data, dict) else None
        tables = kl.get("tables") if isinstance(kl, dict) else None
        if not isinstance(tables, dict):
            return {"moved": 0}
        added = uistate.merge_tables(tables)
        kl.pop("tables", None)
        dataupgrade.atomic_json(SETTINGS_FILE, data)
    return {"moved": len(tables), "added": added, "file": os.path.basename(uistate.UI_STATE_FILE)}


dataupgrade.register(from_version=3, fn=move_tables, name="ui-state",
                     description="move table layouts out of settings.json into ui_state.json")
