"""KopiaLens's own notification channel: a webhook for the events kopia does not send itself
(KopiaLens jobs, another app taking over backups, KopiaLens's server stopping, failed backups
that KopiaLens recorded). Plain text works with ntfy (title in the Title header); JSON suits
other services. Settings: `kopialens.notify_webhook` {url, format, headers, level}.
"""

from __future__ import annotations

import threading
import time

import httpx

from .applog import get_logger

log = get_logger("notify")

_RANK = {"info": 0, "warn": 1, "error": 2}


def _config() -> dict:
    from ..settings import load_settings
    cfg = (load_settings().get("kopialens") or {}).get("notify_webhook") or {}
    return cfg if isinstance(cfg, dict) else {}


def send(title: str, body: str = "", level: str = "info", force: bool = False) -> None:
    """Post one message now (raises on failure). ``force`` ignores the level filter (tests)."""
    cfg = _config()
    url = (cfg.get("url") or "").strip()
    if not url:
        raise ValueError("no webhook address is set")
    if not force and _RANK.get(level, 0) < _RANK.get(cfg.get("level") or "warn", 1):
        return
    headers = {}
    for h in cfg.get("headers") or []:
        k, _, v = str(h).partition(":")
        if k.strip() and v.strip():
            headers[k.strip()] = v.strip()
    if cfg.get("format") == "json":
        r = httpx.post(url, json={"app": "KopiaLens", "title": title, "message": body, "level": level,
                                  "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, headers=headers, timeout=15.0)
    else:
        headers.setdefault("Title", title.encode("ascii", "replace").decode())
        if level == "error":
            headers.setdefault("Priority", "high")
        r = httpx.post(url, content=(body or title).encode("utf-8"), headers=headers, timeout=15.0)
    r.raise_for_status()


def send_later(title: str, body: str = "", level: str = "info") -> None:
    """Fire and forget: alerts must never wait on, or fail because of, the network."""
    if not (_config().get("url") or "").strip():
        return

    def run() -> None:
        try:
            send(title, body, level)
        except Exception as exc:  # noqa: BLE001 - logged, never raised into the caller
            log.warning("notification webhook failed: %s", exc)

    threading.Thread(target=run, name="notify", daemon=True).start()
