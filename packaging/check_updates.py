"""List every KopiaLens dependency that is behind its latest release (the first step of a release). Read-only: it changes nothing.

    python packaging\\check_updates.py
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL = os.path.join(KL_ROOT, "shell", "src-tauri")
BUILD_PYTHON = os.environ.get("KOPIALENS_BUILD_PYTHON") or r"J:\Excluded\KopiaLens-Build\venv\Scripts\python.exe"
VENDOR_PACKAGES = {"alpine": "alpinejs", "tabulator": "tabulator-tables"}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "kopialens-check-updates"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def pypi_latest(name: str) -> str:
    return fetch_json(f"https://pypi.org/pypi/{name}/json")["info"]["version"]


def npm_versions(name: str) -> tuple[str, list[str]]:
    data = fetch_json(f"https://registry.npmjs.org/{name}")
    return data["dist-tags"]["latest"], list(data["versions"])


def _key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v)[:3])


def python_pins() -> list[tuple[str, str]]:
    out = []
    with open(os.path.join(KL_ROOT, "requirements.txt"), encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s#;]+)", line)
            if m:
                out.append(m.groups())
    return out


def rows() -> list[tuple[str, str, str, str]]:
    """(group, name, current, latest) for everything checked."""
    out = []
    for name, have in python_pins():
        out.append(("python", name, have, pypi_latest(name)))
    try:
        listed = subprocess.run([BUILD_PYTHON, "-m", "pip", "list", "--format", "json"], capture_output=True,
                                text=True, timeout=60).stdout
        nuitka = next((p["version"] for p in json.loads(listed) if p["name"].lower() == "nuitka"), "?")
        out.append(("build", "nuitka", nuitka, pypi_latest("nuitka")))
    except (OSError, ValueError, subprocess.SubprocessError):
        out.append(("build", "nuitka", "build venv not found", pypi_latest("nuitka")))

    for fname in sorted(os.listdir(os.path.join(KL_ROOT, "frontend", "vendor"))):
        m = re.match(r"^([a-z]+)-(\d+\.\d+\.\d+)", fname)
        if m and m.group(1) in VENDOR_PACKAGES and fname.endswith(".js"):
            out.append(("web", VENDOR_PACKAGES[m.group(1)], m.group(2), npm_versions(VENDOR_PACKAGES[m.group(1)])[0]))
    with open(os.path.join(KL_ROOT, "packaging", "build_css.py"), encoding="utf-8") as fh:
        tw = re.search(r"\(v(\d+\.\d+\.\d+)\)", fh.read()).group(1)
    latest, versions = npm_versions("tailwindcss")
    same_major = max((v for v in versions if v.split(".")[0] == tw.split(".")[0] and re.fullmatch(r"[\d.]+", v)), key=_key)
    out.append(("web", "tailwindcss", tw, latest if same_major == tw else f"{latest} (newest {tw.split('.')[0]}.x: {same_major})"))

    with open(os.path.join(KL_ROOT, "app", "core", "kopiabin.py"), encoding="utf-8") as fh:
        kopia = re.search(r'^TESTED_VERSION\s*=\s*"([^"]+)"', fh.read(), re.M).group(1)
    out.append(("bundled", "kopia", kopia, fetch_json("https://api.github.com/repos/kopia/kopia/releases/latest")["tag_name"].lstrip("v")))
    return out


def rust_updates() -> list[str]:
    """`cargo update --dry-run`: compatible updates, plus newer major versions it will not take."""
    res = subprocess.run(["cargo", "update", "--dry-run", "--verbose"], cwd=SHELL, capture_output=True, text=True, timeout=300)
    lines = (res.stdout + res.stderr).splitlines()
    return [ln.strip() for ln in lines if ln.strip().startswith(("Updating ", "Adding ", "Removing ")) and "index" not in ln
            or "(available:" in ln]


def main() -> int:
    behind = 0
    print(f"{'group':<8} {'package':<20} {'current':<14} latest")
    for group, name, have, latest in rows():
        mark = "" if have == latest else "  <- behind"
        behind += bool(mark)
        print(f"{group:<8} {name:<20} {have:<14} {latest}{mark}")
    rust = rust_updates()
    print(f"\nRust crates (shell/src-tauri): {len(rust)} change(s) available")
    for ln in rust:
        print(f"  {ln}")
    print(f"\n{behind} package(s) behind, {len(rust)} Rust crate change(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
