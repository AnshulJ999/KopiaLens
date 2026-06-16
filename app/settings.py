"""Shared settings file support."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from config import SETTINGS_FILE

DEFAULT_SETTINGS: dict[str, Any] = {
    "last_repo": "1",
    "laptop_mode": "all",
    "verbose": False,
    "recent": "",
    "listing_backend": "subprocess",  # "subprocess" (default, no bg activity) | "server" (persistent kopia server)
    "kopialens": {
        "columns": [],
        "last_panel": "dashboard",
        "window": {},
        "time_format": "absolute",
        "tables": {},        # per-table Tabulator persistence (columns + sort), keyed by table id
        "page_size": 500,    # search default page size
    },
}


def load_settings() -> dict[str, Any]:
    settings = deepcopy(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as handle:
            loaded = json.load(handle)
        settings.update(loaded)
        settings["kopialens"] = {**DEFAULT_SETTINGS["kopialens"], **loaded.get("kopialens", {})}
    except Exception:
        pass
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
