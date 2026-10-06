"""Build frontend/css/tailwind.css with the Tailwind standalone CLI (v3.4.19).

    python packaging/build_css.py           build
    python packaging/build_css.py --check   exit 1 if a fresh build would differ from the file

The CLI is one download per OS from github.com/tailwindlabs/tailwindcss/releases (v3.4.19),
kept in tools/ (gitignored). KOPIALENS_TAILWIND points at another copy. Staleness is judged by
the CSS itself, so edits that add no new class never need a rebuild.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(KL_ROOT, "packaging", "tailwind.config.js")
SOURCE = os.path.join(KL_ROOT, "packaging", "tailwind.src.css")
OUTPUT = os.path.join(KL_ROOT, "frontend", "css", "tailwind.css")
def cli_path() -> str:
    override = os.environ.get("KOPIALENS_TAILWIND")
    if override:
        return override
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    name = {"win32": f"tailwindcss-windows-{arch}.exe",
            "darwin": f"tailwindcss-macos-{arch}"}.get(sys.platform, f"tailwindcss-linux-{arch}")
    return os.path.join(KL_ROOT, "tools", name)


def compile_css(out_path: str) -> int:
    """Run the CLI into ``out_path``; 2 when the CLI is missing."""
    cli = cli_path()
    if not os.path.exists(cli):
        print(f"Tailwind CLI not found: {cli}\nDownload v3.4.19 from "
              "https://github.com/tailwindlabs/tailwindcss/releases/tag/v3.4.19 into tools/.")
        return 2
    env = {**os.environ, "BROWSERSLIST_IGNORE_OLD_DATA": "1"}
    return subprocess.run([cli, "-c", CONFIG, "-i", SOURCE, "-o", out_path, "--minify"],
                          cwd=KL_ROOT, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def _css(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def is_current() -> bool | None:
    """True/False: would a fresh build equal the committed file? None when the CLI is missing."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "tailwind.css")
        rc = compile_css(out)
        if rc == 2:
            return None
        return rc == 0 and _css(out) == _css(OUTPUT)


def build() -> int:
    tmp = OUTPUT + ".tmp"
    rc = compile_css(tmp)
    if rc != 0:
        return rc
    css = _css(tmp) or ""
    changed = css != _css(OUTPUT)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(css + "\n")
    os.replace(tmp, OUTPUT)
    print(f"built {os.path.relpath(OUTPUT, KL_ROOT)} ({len(css) // 1024} KB)" if changed else "up to date")
    return 0


def main() -> int:
    if "--check" in sys.argv[1:]:
        current = is_current()
        print({True: "up to date", False: "stale: run python packaging/build_css.py",
               None: "Tailwind CLI missing"}[current])
        return 0 if current else 1
    return build()


if __name__ == "__main__":
    sys.exit(main())
