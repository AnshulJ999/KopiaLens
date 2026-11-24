"""Build the logo files from one set of shapes.

Writes:
  shell/src-tauri/icons/*          the desktop app's own icon set (window, taskbar, installer, exe): Disc
  shell/src-tauri/icons/shortcuts/ KopiaLens <name>.ico, one per logo, for changing a desktop shortcut's icon
  frontend/logos/<id>.svg          browser tab icons for the header logo choices
  frontend/favicon.svg             the default tab icon (Disc)
  frontend/logos/tray/<id>.png     previews of the tray icon choices (Settings > Desktop app)
  shell/src-tauri/icons/tray/<id>.rgba   the tray icons themselves, 64x64 raw RGBA, read by tray.rs
                                         (<id>-light.rgba for a light taskbar)

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
SHORTCUTS = ROOT / "shell" / "src-tauri" / "icons" / "shortcuts"
SIZE = 64

DEEP, BRIGHT, AMBER, AMBER_DARK, WHITE = "#0f766e", "#2dd4bf", "#f59e0b", "#fbbf24", "#ffffff"
# m = upright / main colour, mid = the K's arm, a = accent (leg), ring = K3a's ring (deep in both).
LIGHT = {"m": DEEP, "mid": "#2e9c91", "a": AMBER, "ring": DEEP}
DARK = {"m": BRIGHT, "mid": "#11806f", "a": AMBER_DARK, "ring": DEEP}
MONO_DARK = dict.fromkeys(LIGHT, "#f4f4f5")
MONO_LIGHT = dict.fromkeys(LIGHT, "#18181b")


def _pt(r: float, deg: float, c: float = 32) -> tuple[float, float]:
    t = math.radians(deg)
    return round(c + r * math.cos(t), 2), round(c + r * math.sin(t), 2)


def _arc(r: float, a0: float, a1: float, col: str, w: float, c: float = 32, cap: str = "butt") -> str:
    (x0, y0), (x1, y1) = _pt(r, a0, c), _pt(r, a1, c)
    large = 1 if ((a1 - a0 + 720) % 360) > 180 else 0
    return (f'<path d="M{x0} {y0} A{r} {r} 0 {large} 1 {x1} {y1}" fill="none" '
            f'stroke="var(--{col})" stroke-width="{w}" stroke-linecap="{cap}"/>')


def _scale(s: float, inner: str) -> str:
    return f'<g transform="translate(32 32) scale({s}) translate(-32 -32)">{inner}</g>'


def _k(up: str, arm: str, leg: str, arm_opacity: str = "1") -> str:
    """The sharp K (Original 7's shape), centred in the 64 box."""
    return (f'<g transform="translate(-3 0)"><rect x="10" y="8" width="11" height="48" rx="2" fill="{up}"/>'
            f'<polygon points="24,30 45,8 57,8 36,30" fill="{arm}" fill-opacity="{arm_opacity}"/>'
            f'<polygon points="28,35 40,35 60,56 48,56" fill="{leg}"/></g>')


K_VARS = _k("var(--m)", "var(--mid)", "var(--a)")
DISC = f'<circle cx="32" cy="32" r="27" fill="{DEEP}"/>' + _scale(0.52, _k(WHITE, WHITE, AMBER))
TILE = (f'<rect x="1" y="1" width="62" height="62" rx="14" fill="{DEEP}"/>'
        + _scale(0.7, _k(WHITE, WHITE, AMBER_DARK, ".62")))
RING = '<circle cx="32" cy="32" r="26" fill="none" stroke="var(--ring)" stroke-width="5"/>' + _scale(0.46, K_VARS)

# Header / tab / shortcut logos, in priority order. Ids are internal; the app shows the names.
LOGOS = {"k3g": ("Disc", DISC), "k6s": ("K", K_VARS), "k3a": ("Ring", RING), "tile": ("Tile", TILE)}

# Tray choices in menu order (first = default), each drawn for a dark and a light taskbar.
TRAY_MARKS = {
    "k3g": DISC, "k6s": K_VARS, "k3a": RING, "tile": TILE,
    "mono": K_VARS,
    # Q with a glint: a highlight inside the ring makes the Q read as a lens.
    "q3": ('<circle cx="31" cy="31" r="18" fill="none" stroke="var(--m)" stroke-width="10"/>'
           + _arc(7.5, 190, 270, "a", 5, 31, "round")
           + '<rect x="39" y="45" width="19" height="9" rx="4.5" fill="var(--m)"/>'),
    "q1": ('<circle cx="31" cy="31" r="18" fill="none" stroke="var(--m)" stroke-width="10"/>'
           '<rect x="39" y="45" width="19" height="9" rx="4.5" fill="var(--m)"/><circle cx="31" cy="31" r="6" fill="var(--a)"/>'),
    "s3": _arc(18, 72, 378, "m", 11) + f'<g transform="translate(4.2 4.2)">{_arc(18, 23, 67, "a", 11)}</g>',
    "r4": _arc(20, -20, 290, "m", 9) + '<circle cx="32" cy="32" r="7.5" fill="var(--a)"/>',
    "slice": _arc(18, 4, 304, "m", 12) + f'<g transform="translate(4.6 -2.7)">{_arc(18, -52, -8, "a", 12)}</g>',
    # The original tray Q, without its white tile: the centre is a real hole.
    "q": '<circle cx="32" cy="32" r="17" fill="none" stroke="var(--m)" stroke-width="14"/><rect x="42" y="42" width="16" height="8" fill="var(--m)"/>',
}
TRAY = {k: (v, MONO_DARK if k == "mono" else DARK) for k, v in TRAY_MARKS.items()}
TRAY_LIGHT = {k: (v, MONO_LIGHT if k == "mono" else LIGHT) for k, v in TRAY_MARKS.items()}

ICO_SIZES = (256, 128, 64, 48, 40, 32, 24, 20, 16)


def _vars(p: dict) -> str:
    return ";".join(f"--{k}:{v}" for k, v in p.items())


def _svg(inner: str, palette: dict, dark_aware: bool = False) -> str:
    style = f"svg{{{_vars(palette)}}}"
    if dark_aware:
        style += f"@media (prefers-color-scheme: dark){{svg{{{_vars(DARK)}}}}}"
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><style>{style}</style>{inner}</svg>'


def _render(page, inner: str, palette: dict, size: int):
    from PIL import Image
    svg = _svg(inner, palette).replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
    page.set_viewport_size({"width": size, "height": size})
    page.set_content(f'<html><body style="margin:0;background:transparent">{svg}</body></html>')
    png = page.locator("svg").screenshot(omit_background=True)
    return Image.open(io.BytesIO(png)).convert("RGBA").resize((size, size))


# Tray icons fill the square like other tray icons do; the logo drawings keep their margin.
TRAY_FILL = 60


def _fill_tray(page, inner: str, palette: dict) -> str:
    """The mark scaled and centred so its larger side spans TRAY_FILL of the 64 box."""
    x0, y0, x1, y1 = _render(page, inner, palette, SIZE).getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox()
    s = TRAY_FILL / max(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return f'<g transform="translate(32 32) scale({s:.4f}) translate({-cx:.2f} {-cy:.2f})">{inner}</g>'


def main() -> None:
    from playwright.sync_api import sync_playwright

    for d in (FAV, TRAY_PNG, TRAY_RGBA, SHORTCUTS):
        d.mkdir(parents=True, exist_ok=True)
    for key, (_name, inner) in LOGOS.items():
        (FAV / f"{key}.svg").write_text(_svg(inner, LIGHT, dark_aware=True), encoding="utf-8", newline="\n")
    (ROOT / "frontend" / "favicon.svg").write_text(_svg(DISC, LIGHT), encoding="utf-8", newline="\n")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        for icons, suffix in ((TRAY, ""), (TRAY_LIGHT, "-light")):
            for key, (inner, palette) in icons.items():
                img = _render(page, _fill_tray(page, inner, palette), palette, SIZE)
                img.save(TRAY_PNG / f"{key}{suffix}.png")
                (TRAY_RGBA / f"{key}{suffix}.rgba").write_bytes(img.tobytes())
        # Shortcut icons: every size drawn on its own (sharper than shrinking the largest).
        for key, (name, inner) in LOGOS.items():
            imgs = [_render(page, inner, LIGHT, s) for s in ICO_SIZES]
            imgs[0].save(SHORTCUTS / f"KopiaLens {name}.ico", format="ICO",
                         sizes=[(s, s) for s in ICO_SIZES], append_images=imgs[1:])
        app_png = io.BytesIO()
        _render(page, DISC, LIGHT, APP_ICON_SIZE).save(app_png, format="PNG")
        browser.close()
    _app_icon_set(app_png.getvalue())
    print(f"{len(TRAY)} tray icons (dark and light taskbar), {len(LOGOS)} tab icons + shortcut icons, "
          "the desktop app's icon set")


# The desktop app's own icon (window, taskbar, installer, exe): the Disc, no tile.
APP_ICON_SIZE = 1024
APP_ICONS = ROOT / "shell" / "src-tauri" / "icons"


def _app_icon_set(png: bytes) -> None:
    """Regenerate every icon file the app already has with `cargo tauri icon` (it also makes
    Android/iOS sets, which are not copied)."""
    import shutil
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "app-icon.png"
        src.write_bytes(png)
        out = Path(tmp) / "out"
        subprocess.run(["cargo", "tauri", "icon", str(src), "-o", str(out)], check=True, capture_output=True)
        for old in APP_ICONS.iterdir():
            if old.is_file() and (out / old.name).is_file():
                shutil.copyfile(out / old.name, old)


if __name__ == "__main__":
    main()
