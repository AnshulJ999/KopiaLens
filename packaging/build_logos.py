"""Build the logo files from one set of shapes.

Writes:
  frontend/logos/<id>.svg          browser tab icons for the header logo choices (light and dark aware)
  frontend/logos/tray/<id>.png     previews of the tray icon choices (Settings > Desktop app)
  shell/src-tauri/icons/tray/<id>.rgba   the tray icons themselves, 64x64 raw RGBA, read by tray.rs
                                         (<id>-light.rgba for a light taskbar)

Shapes come from the logo mockups and the website.
Rendering needs Playwright's Chromium and Pillow.

    python packaging/build_logos.py
"""
from __future__ import annotations

import io
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAV = ROOT / "frontend" / "logos"
TRAY_PNG = FAV / "tray"
TRAY_RGBA = ROOT / "shell" / "src-tauri" / "icons" / "tray"
SIZE = 64

LIGHT = {"m": "#0f766e", "mid": "#5eaaa2", "a": "#f59e0b"}
DARK = {"m": "#2dd4bf", "mid": "#11806f", "a": "#fbbf24"}
MONO = {"m": "#f4f4f5", "mid": "#f4f4f5", "a": "#f4f4f5"}


def _pt(r: float, deg: float, c: float = 32) -> tuple[float, float]:
    t = math.radians(deg)
    return round(c + r * math.cos(t), 2), round(c + r * math.sin(t), 2)


def _arc(r: float, a0: float, a1: float, col: str, w: float, c: float = 32) -> str:
    (x0, y0), (x1, y1) = _pt(r, a0, c), _pt(r, a1, c)
    large = 1 if ((a1 - a0 + 720) % 360) > 180 else 0
    return (f'<path d="M{x0} {y0} A{r} {r} 0 {large} 1 {x1} {y1}" fill="none" '
            f'stroke="var(--{col})" stroke-width="{w}"/>')


K6 = ('<g transform="translate(-2.5 -0.5)"><rect x="10" y="8" width="12" height="48" rx="3" fill="var(--m)"/>'
      '<polygon points="25,31 46,8 57,8 36,31" fill="var(--mid)"/>'
      '<polygon points="31,38 42,38 59,57 48,57" fill="var(--a)" stroke="var(--a)" stroke-width="2" stroke-linejoin="round"/></g>')
ORIG7 = ('<rect x="10" y="8" width="11" height="48" rx="2.5" fill="var(--m)"/>'
         '<polygon points="24,30 45,8 57,8 36,30" fill="var(--m)"/><polygon points="28,35 40,35 60,56 48,56" fill="var(--a)"/>')
K3A = ('<circle cx="32" cy="32" r="26" fill="none" stroke="var(--m)" stroke-width="5"/>'
       '<g transform="translate(32 32) scale(0.46) translate(-32 -32)"><g transform="translate(-2.5 -0.5)">'
       '<rect x="10" y="8" width="12" height="48" rx="3" fill="var(--m)"/>'
       '<polygon points="25,31 46,8 57,8 36,31" fill="var(--m)" stroke="var(--m)" stroke-width="2" stroke-linejoin="round"/>'
       '<polygon points="31,38 42,38 59,57 48,57" fill="var(--a)" stroke="var(--a)" stroke-width="2" stroke-linejoin="round"/></g></g>')

MARKS = {
    "k6": K6,
    "orig7": ORIG7,
    "k3a": K3A,
    # Round 2 "Q1": the old ring-with-tail, slimmer, with an amber pupil.
    "q1": ('<circle cx="31" cy="31" r="18" fill="none" stroke="var(--m)" stroke-width="10"/>'
           '<rect x="39" y="45" width="19" height="9" rx="4.5" fill="var(--m)"/><circle cx="31" cy="31" r="6" fill="var(--a)"/>'),
    # Round 2 "S3": the slice comes out where the Q's tail was.
    "s3": _arc(18, 72, 378, "m", 11) + f'<g transform="translate(4.2 4.2)">{_arc(18, 23, 67, "a", 11)}</g>',
    # Round 2 "R4": one bold ring with a gap and an amber core.
    "r4": _arc(20, -20, 290, "m", 9) + '<circle cx="32" cy="32" r="7.5" fill="var(--a)"/>',
    # Round 1 #2: a ring with one slice pulled out.
    "slice": _arc(18, 4, 304, "m", 12) + f'<g transform="translate(4.6 -2.7)">{_arc(18, -52, -8, "a", 12)}</g>',
}
# Today's tray mark, kept as a choice: the teal "Q" on a light tile.
OLD = ('<rect width="64" height="64" fill="#fafaf9"/><circle cx="32" cy="32" r="24" fill="#0f766e"/>'
       '<circle cx="32" cy="32" r="10" fill="#fafaf9"/><rect x="42" y="42" width="16" height="8" fill="#0f766e"/>')

MONO_LIGHT = {"m": "#18181b", "mid": "#18181b", "a": "#18181b"}

TRAY = {**{k: (v, DARK) for k, v in MARKS.items()}, "mono": (K6, MONO), "old": (OLD, DARK)}
# Light taskbar versions (`<id>-light`), picked by tray.rs when the taskbar is light.
TRAY_LIGHT = {**{k: (v, LIGHT) for k, v in MARKS.items()}, "mono": (K6, MONO_LIGHT), "old": (OLD, DARK)}


def _vars(p: dict) -> str:
    return ";".join(f"--{k}:{v}" for k, v in p.items())


def _svg(inner: str, palette: dict, dark_aware: bool = False) -> str:
    style = f"svg{{{_vars(palette)}}}"
    if dark_aware:
        style += f"@media (prefers-color-scheme: dark){{svg{{{_vars(DARK)}}}}}"
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><style>{style}</style>{inner}</svg>'


def main() -> None:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    FAV.mkdir(parents=True, exist_ok=True)
    TRAY_PNG.mkdir(parents=True, exist_ok=True)
    TRAY_RGBA.mkdir(parents=True, exist_ok=True)
    for key in ("k6", "orig7", "k3a"):
        (FAV / f"{key}.svg").write_text(_svg(MARKS[key], LIGHT, dark_aware=True), encoding="utf-8", newline="\n")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": SIZE, "height": SIZE})
        sets = [(TRAY, ""), (TRAY_LIGHT, "-light")]
        for icons, suffix in sets:
            for key, (inner, palette) in icons.items():
                svg = _svg(inner, palette).replace("<svg ", f'<svg width="{SIZE}" height="{SIZE}" ', 1)
                page.set_content(f'<html><body style="margin:0;background:transparent">{svg}</body></html>')
                png = page.locator("svg").screenshot(omit_background=True)
                img = Image.open(io.BytesIO(png)).convert("RGBA").resize((SIZE, SIZE))
                img.save(TRAY_PNG / f"{key}{suffix}.png")
                (TRAY_RGBA / f"{key}{suffix}.rgba").write_bytes(img.tobytes())
        browser.close()
    print(f"{len(TRAY)} tray icons (dark and light taskbar), 3 tab icons")


if __name__ == "__main__":
    main()
