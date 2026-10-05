"""Settings > Remote access: who can open KopiaLens, HTTPS, reverse proxies. Everything here is read
at start, so the reply says what is running now and whether a restart is pending."""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..core import auth, netaccess
from ..settings import mutate_settings, raw_kopialens_settings

router = APIRouter(tags=["access"])

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$")
_ENV = ("KOPIALENS_ACCESS", "KOPIALENS_ACCESS_ADDRESSES", "KOPIALENS_BIND_HOST", "KOPIALENS_HTTPS",
        "KOPIALENS_HTTPS_CERT", "KOPIALENS_HTTPS_KEY", "KOPIALENS_TRUSTED_PROXIES", "KOPIALENS_ALLOWED_HOSTS")


class AccessUpdate(BaseModel):
    access: str | None = None
    access_addresses: list[str] | None = None
    https: str | None = None
    https_cert: str | None = None
    https_key: str | None = None
    trusted_proxies: list[str] | None = None
    allowed_hosts: list[str] | None = None


def _state(request: Request) -> dict:
    raw = raw_kopialens_settings()
    profile = auth.profile()
    started = dict(netaccess.STARTED)
    want = {"mode": netaccess.mode(), "bind": auth.bind_host(), "https": netaccess.https_mode() != "off",
            "proxies": netaccess.trusted_proxies()}
    restart = bool(started) and (started.get("mode") != want["mode"] or started.get("bind") != want["bind"]
                                 or bool(started.get("https")) != want["https"]
                                 or started.get("proxies") != want["proxies"])
    client = request.client.host if request.client else ""
    try:
        import ipaddress
        remote = not ipaddress.ip_address(client.split("%")[0]).is_loopback
    except ValueError:
        remote = False
    return {
        "profile": profile,
        "access": netaccess.mode(),
        "modes": [m for m in netaccess.MODES if m != "window" or profile == "desktop"],
        "access_addresses": [str(n) for n in netaccess.addresses()],
        "https": netaccess.https_mode(),
        "https_allowed": profile != "desktop",
        "https_cert": str(raw.get("https_cert") or ""),
        "https_key": str(raw.get("https_key") or ""),
        "trusted_proxies": netaccess.trusted_proxies(),
        "allowed_hosts": auth.allowed_hosts(),
        "bind_override": (os.environ.get("KOPIALENS_BIND_HOST") or str(raw.get("bind_host") or "")).strip(),
        "env": [e for e in _ENV if (os.environ.get(e) or "").strip()],
        "running": {**started, "cert_info": netaccess.cert_info(started["cert"]) if started.get("cert") else None},
        "restart_needed": restart,
        "you_are_remote": remote,
        "you_are": client,
    }


@router.get("/access")
def get_access(request: Request) -> dict:
    return _state(request)


@router.post("/access")
def set_access(body: AccessUpdate, request: Request) -> dict:
    change: dict = {}
    if body.access is not None:
        if body.access not in netaccess.MODES or (body.access == "window" and auth.profile() != "desktop"):
            raise HTTPException(status_code=400, detail="Pick one of the listed choices.")
        change["access"] = body.access
    if body.access_addresses is not None:
        good, bad = netaccess.parse_addresses(body.access_addresses)
        if bad:
            raise HTTPException(status_code=400, detail="Not an address or network: " + ", ".join(bad))
        change["access_addresses"] = good
    mode_after = change.get("access") or netaccess.mode()
    addrs_after = change["access_addresses"] if "access_addresses" in change else netaccess.addresses()
    if mode_after == "addresses" and not addrs_after and ("access" in change or "access_addresses" in change):
        raise HTTPException(status_code=400, detail="Add at least one address first.")
    if body.https is not None:
        if body.https not in netaccess.HTTPS_MODES:
            raise HTTPException(status_code=400, detail="Pick one of the listed choices.")
        if body.https != "off" and auth.profile() == "desktop":
            raise HTTPException(status_code=400, detail="The desktop app serves plain HTTP. Use a reverse proxy for HTTPS.")
        change["https"] = body.https
    for key in ("https_cert", "https_key"):
        val = getattr(body, key)
        if val is not None:
            val = val.strip()
            if val and not os.path.isfile(val):
                raise HTTPException(status_code=400, detail=f"No file at {val}")
            change[key] = val
    if (change.get("https") == "files"
            and not ((change.get("https_cert") or raw_kopialens_settings().get("https_cert"))
                     and (change.get("https_key") or raw_kopialens_settings().get("https_key")))):
        raise HTTPException(status_code=400, detail="Set the certificate and key files first.")
    if body.trusted_proxies is not None:
        good, bad = netaccess.parse_addresses(body.trusted_proxies)
        if bad:
            raise HTTPException(status_code=400, detail="Not an address or network: " + ", ".join(bad))
        change["trusted_proxies"] = good
    if body.allowed_hosts is not None:
        hosts = [h.strip().lower() for h in body.allowed_hosts if h.strip()]
        bad = [h for h in hosts if not _HOST_RE.match(h)]
        if bad:
            raise HTTPException(status_code=400, detail="Not a hostname: " + ", ".join(bad))
        change["allowed_hosts"] = hosts
    if change:
        mutate_settings(lambda s: s.setdefault("kopialens", {}).update(change))
    return _state(request)
