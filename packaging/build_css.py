"""Build frontend/css/tailwind.css with the Tailwind standalone CLI (v3.4.17).

    python packaging/build_css.py           build
    python packaging/build_css.py --check   exit 1 if the built file is older than its inputs

The CLI is one download per OS from github.com/tailwindlabs/tailwindcss/releases (v3.4.17),
kept in tools/ (gitignored). KOPIALENS_TAILWIND points at another copy. The built file starts
with a digest of every input, so a test can tell when a UI edit needs a rebuild.
"""
from __future__ import annotations

import glob
import hashlib
import os
import platform
import subprocess
import sys

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(KL_ROOT, "packaging", "tailwind.config.js")
SOURCE = os.path.join(KL_ROOT, "packaging", "tailwind.src.css")
OUTPUT = os.path.join(KL_ROOT, "frontend", "css", "tailwind.css")
CONTENT_GLOBS = ("frontend/index.html", "frontend/partials/**/*.html", "frontend/js/**/*.js")
STAMP_PREFIX = "/* kopialens-css "


def input_files() -> list[str]:
    files = {CONFIG, SOURCE}
    for pattern in CONTENT_GLOBS:
        files.update(glob.glob(os.path.join(KL_ROOT, pattern), recursive=True))
    return sorted(files)


def input_digest() -> str:
    """Line endings are normalised so a CRLF checkout hashes the same as LF."""
    h = hashlib.sha256()
    for path in input_files():
        h.update(os.path.relpath(path, KL_ROOT).replace("\\", "/").encode())
        with open(path, "rb") as fh:
            h.update(fh.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def built_digest() -> str | None:
    try:
        with open(OUTPUT, encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    if not first.startswith(STAMP_PREFIX):
        return None
    return first[len(STAMP_PREFIX):].split()[0]


def cli_path() -> str:
    override = os.environ.get("KOPIALENS_TAILWIND")
    if override:
        return override
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    name = {"win32": f"tailwindcss-windows-{arch}.exe",
            "darwin": f"tailwindcss-macos-{arch}"}.get(sys.platform, f"tailwindcss-linux-{arch}")
    return os.path.join(KL_ROOT, "tools", name)


def build() -> int:
    cli = cli_path()
    if not os.path.exists(cli):
        print(f"Tailwind CLI not found: {cli}\nDownload v3.4.17 from "
              "https://github.com/tailwindlabs/tailwindcss/releases/tag/v3.4.17 into tools/.")
        return 2
    digest = input_digest()
    tmp = OUTPUT + ".tmp"
    env = {**os.environ, "BROWSERSLIST_IGNORE_OLD_DATA": "1"}
    rc = subprocess.run([cli, "-c", CONFIG, "-i", SOURCE, "-o", tmp, "--minify"],
                        cwd=KL_ROOT, env=env, stdin=subprocess.DEVNULL).returncode
    if rc != 0:
        return rc
    with open(tmp, encoding="utf-8") as fh:
        css = fh.read()
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{STAMP_PREFIX}{digest} - built by packaging/build_css.py, do not edit */\n{css}\n")
    os.replace(tmp, OUTPUT)
    print(f"built {os.path.relpath(OUTPUT, KL_ROOT)} ({len(css) // 1024} KB, inputs {digest})")
    return 0


def main() -> int:
    if "--check" in sys.argv[1:]:
        current = input_digest() == built_digest()
        print("up to date" if current else "stale: run python packaging/build_css.py")
        return 0 if current else 1
    return build()


if __name__ == "__main__":
    sys.exit(main())
