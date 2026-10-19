"""Help menu: links, update status, what the usage stats send, the diagnostics bundle, and the
user docs for the in-app docs browser."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from config import APP_VERSION, DISCORD_URL, DOCS_URL, REPO_URL, SUPPORT_EMAIL

from ..core import checkin, diagnostics, exportname, netaccess
from ..core.kopia import page_kopia_ok

router = APIRouter(tags=["help"])

DOCS = Path(__file__).resolve().parents[2] / "docs"
_INDEX_LINK = re.compile(r"^\s*-\s*\[[^\]]+\]\(([^)#]+\.md)")


@router.get("/help/docs")
def docs() -> dict:
    """Every page of the user docs, grouped the way docs/README.md lists them (unlisted pages last)."""
    pages = {}
    for f in sorted(DOCS.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), f.stem)
        pages[f.stem] = {"title": title, "md": text}
    groups: list[dict] = []
    listed: set[str] = set()
    for line in pages.get("README", {}).get("md", "").splitlines():
        if line.startswith("## "):
            groups.append({"title": line[3:].strip(), "pages": []})
        elif groups and (m := _INDEX_LINK.match(line)):
            slug = Path(m.group(1)).stem
            if slug in pages and slug not in listed:
                groups[-1]["pages"].append(slug)
                listed.add(slug)
    rest = [s for s in pages if s != "README" and s not in listed]
    if rest:
        groups.append({"title": "More", "pages": rest})
    # Images and other files the pages may link to; a screenshot not in this list shows as a placeholder.
    files = sorted(p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*") if p.is_file() and p.suffix != ".md")
    return {"pages": pages, "groups": [g for g in groups if g["pages"]], "files": files}


@router.get("/help/docs/file")
def docs_file(path: str) -> FileResponse:
    """An image (or other file) a docs page links to, from inside the docs folder only."""
    root = DOCS.resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


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


@router.post("/help/install-update")
def install_update() -> dict:
    """Asks the desktop app to install the update (it waits for indexing and jobs to finish)."""
    st = checkin.status()
    if not st["can_install"]:
        raise HTTPException(status_code=400, detail="This copy of KopiaLens can't install updates itself. Download the new version instead.")
    checkin.request_install()
    return {"ok": True}


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
    with page_kopia_ok("the user is building a diagnostics report"):
        return diagnostics.build(_sections(sections), anonymise=anonymise, days=_days(days),
                                 browser=request.headers.get("user-agent", ""))


@router.get("/diagnostics/download")
def diagnostics_download(request: Request, sections: str = "", anonymise: bool = True, id: str = "",
                         days: int = 7) -> Response:
    with page_kopia_ok("the user is building a diagnostics report"):
        bundle = diagnostics.build(_sections(sections), anonymise=anonymise, bundle_id=id or None, days=_days(days),
                                   browser=request.headers.get("user-agent", ""))
    name = exportname.export_name("diagnostics", bundle["id"], ext="zip")
    return Response(diagnostics.zip_bytes(bundle), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
