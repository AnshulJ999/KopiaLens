"""The URL prefix KopiaLens is served under: Home Assistant's ingress path, or a reverse proxy's
sub-path (``KOPIALENS_BASE_PATH`` env > ``base_path`` in settings.json). Empty everywhere else, so
every URL stays exactly as it is without one.

The proxy strips the prefix before forwarding, so routes never change; only the URLs the browser
is handed (the page, redirects, the sign-in pages) carry it. The frontend gets it as
``window.KL_BASE`` and applies it through ``appUrl`` (``js/util.js``)."""
from __future__ import annotations

import contextvars
import os
import re

from .applog import get_logger

log = get_logger(__name__)

# Home Assistant's Supervisor: the only address ingress requests come from (developers.home-assistant.io).
HA_PROXY = "172.30.32.2"
INGRESS_HEADER = "x-ingress-path"
_SAFE = re.compile(r"^(/[A-Za-z0-9._~-]+)+$")
_CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar("kopialens_base_path", default="")
_warned: set[str] = set()


def ingress_enabled() -> bool:
    """Set by the Home Assistant add-on's start script; off for every other install."""
    return (os.environ.get("KOPIALENS_HA_INGRESS") or "").strip().lower() in ("1", "true", "yes", "on")


def from_ingress(client_host: str | None) -> bool:
    """A request from Home Assistant's ingress proxy: HA has already signed its user in."""
    return ingress_enabled() and client_host == HA_PROXY


def normalize(raw: str | None) -> str:
    """``/a/b`` (no trailing slash), or "" for none. Anything outside plain path characters is
    refused (it is stamped into the page), with one warning."""
    value = (raw or "").strip().rstrip("/")
    if not value:
        return ""
    if _SAFE.match(value):
        return value
    if value not in _warned:
        _warned.add(value)
        log.warning("base path %r ignored: use a plain path such as /kopialens", value)
    return ""


def configured() -> str:
    env = os.environ.get("KOPIALENS_BASE_PATH")
    if env is not None and env.strip():
        return normalize(env)
    from ..settings import load_settings
    try:
        return normalize(str(load_settings().get("kopialens", {}).get("base_path") or ""))
    except Exception:  # noqa: BLE001 - an unreadable settings file must not break every page
        return ""


def for_request(client_host: str | None, headers) -> str:
    """Ingress path when the request came through Home Assistant, else the configured prefix."""
    if from_ingress(client_host):
        ingress = normalize(headers.get(INGRESS_HEADER))
        if ingress:
            return ingress
    return configured()


def set_current(value: str) -> contextvars.Token:
    return _CURRENT.set(value)


def reset(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def current() -> str:
    return _CURRENT.get()


def url(path: str) -> str:
    """An app path (``/login``) as the browser must request it."""
    return current() + path
