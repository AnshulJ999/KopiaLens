"""Help menu: links, update status, what the usage stats send, and the diagnostics bundle."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from config import APP_VERSION, DISCORD_URL, DOCS_URL, REPO_URL, SUPPORT_EMAIL

from ..core import checkin, diagnostics, netaccess

router = APIRouter(tags=["help"])


@router.get("/help/info")
def info() -> dict:
    from ..licensing import licence
    state = licence.status().get("state")
    return {
        "version": APP_VERSION,
        "install_type": checkin.install_type(),
        "pro": state == "active",
        "access": netaccess.STARTED.get("mode") or netaccess.mode(),
        "links": {"docs": DOCS_URL, "repo": REPO_URL, "issues": REPO_URL.rstrip("/") + "/issues/new/choose",
                  "support_email": SUPPORT_EMAIL, "discord": DISCORD_URL},
        "update": checkin.status(),
    }


@router.post("/help/check-updates")
def check_updates() -> dict:
    """Check now (the Settings button). The daily schedule is unchanged."""
    ok = checkin.check_now(manual=True)
    return {"ok": ok, "update": checkin.status()}


@router.get("/help/usage-preview")
def usage_preview() -> dict:
    """Exactly what the next check-in sends with the current settings."""
    if not (checkin.updates_enabled() or checkin.stats_enabled()):
        return {"sends": False, "payload": None}
    return {"sends": True, "payload": checkin.build_payload(checkin.stats_enabled())}


def _sections(sections: str) -> list[str]:
    return [s for s in (x.strip() for x in sections.split(",")) if s in diagnostics.SECTIONS]


def _days(days: int) -> int:
    return min(max(int(days or 7), 1), 90)


@router.get("/diagnostics/preview")
def diagnostics_preview(request: Request, sections: str = "", anonymise: bool = True, days: int = 7) -> dict:
    return diagnostics.build(_sections(sections), anonymise=anonymise, days=_days(days),
                             browser=request.headers.get("user-agent", ""))


@router.get("/diagnostics/download")
def diagnostics_download(request: Request, sections: str = "", anonymise: bool = True, id: str = "",
                         days: int = 7) -> Response:
    bundle = diagnostics.build(_sections(sections), anonymise=anonymise, bundle_id=id or None, days=_days(days),
                               browser=request.headers.get("user-agent", ""))
    name = f"kopialens-diagnostics-{bundle['id']}.zip"
    return Response(diagnostics.zip_bytes(bundle), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
