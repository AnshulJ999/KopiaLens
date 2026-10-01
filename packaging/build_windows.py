"""Build the KopiaLens server as a Nuitka standalone folder.

Run with the build venv's Python (it holds requirements.txt + Nuitka):
    J:\\Excluded\\KopiaLens-Build\\venv\\Scripts\\python.exe packaging\\build_windows.py

Output: <OUT_DIR>\\kopialens.dist\\kopialens-server.exe, plus build.log and a compilation report.
packaging/release.py runs this as its first build step.
"""
import ast
import os
import re
import subprocess
import sys
import time
from importlib import metadata

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("KOPIALENS_BUILD_OUT") or r"J:\Excluded\KopiaLens-Build\out"
SERVER_EXE = "kopialens-server.exe"


def app_version() -> str:
    with open(os.path.join(KL_ROOT, "VERSION"), encoding="utf-8") as fh:
        return fh.read().strip()


def upgrade_step_modules() -> list[str]:
    """dataupgrade._STEP_MODULES, read from source: the steps are imported by name at runtime,
    so Nuitka's import tracing never sees them."""
    path = os.path.join(KL_ROOT, "app", "core", "dataupgrade.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        target = getattr(node, "target", None) or (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "_STEP_MODULES":
            return list(ast.literal_eval(node.value))
    raise RuntimeError("_STEP_MODULES not found in app/core/dataupgrade.py")


def pin_mismatches() -> list[str]:
    """Installed packages that differ from the pins in requirements.txt."""
    out = []
    with open(os.path.join(KL_ROOT, "requirements.txt"), encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s#;]+)", line)
            if not m:
                continue
            name, want = m.groups()
            try:
                have = metadata.version(name)
            except metadata.PackageNotFoundError:
                have = "not installed"
            if have != want:
                out.append(f"{name}: requirements.txt pins {want}, build venv has {have}")
    return out


def nuitka_args() -> list[str]:
    version = app_version()
    # Opt-in: Nuitka's own tool downloads (Dependency Walker; one-time, cached per user).
    downloads = ["--assume-yes-for-downloads"] if os.environ.get("KOPIALENS_BUILD_ALLOW_DOWNLOADS") else []
    return [
        sys.executable, "-m", "nuitka",
        *downloads,
        "--standalone",
        f"--output-dir={OUT_DIR}",
        f"--output-filename={SERVER_EXE}",
        "--windows-console-mode=attach",
        # kopialens.py dispatches "-m app.index.worker" itself; Nuitka blocks self "-m" calls by default.
        "--no-deployment-flag=self-execution",
        "--python-flag=no_docstrings",
        f"--include-data-dir={os.path.join(KL_ROOT, 'frontend')}=frontend",
        f"--include-data-files={os.path.join(KL_ROOT, 'VERSION')}=VERSION",
        *[f"--include-module={m}" for m in upgrade_step_modules()],
        # Our own test packages only - a *.tests wildcard also drops jinja2.tests, which jinja2 needs.
        "--nofollow-import-to=app.tests",
        "--nofollow-import-to=app.backblaze.tests",
        "--nofollow-import-to=pytest",
        f"--windows-icon-from-ico={os.path.join(KL_ROOT, 'shell', 'src-tauri', 'icons', 'icon.ico')}",
        "--product-name=KopiaLens",
        "--file-description=KopiaLens server",
        f"--file-version={version}",
        f"--product-version={version}",
        f"--report={os.path.join(OUT_DIR, 'compilation-report.xml')}",
        os.path.join(KL_ROOT, "kopialens.py"),
    ]


def main() -> int:
    bad = pin_mismatches()
    if bad and not os.environ.get("KOPIALENS_BUILD_ALLOW_UNPINNED"):
        print("Build venv does not match requirements.txt:\n  " + "\n  ".join(bad), file=sys.stderr)
        print("Run: <venv python> -m pip install -r requirements.txt", file=sys.stderr)
        return 3
    os.makedirs(OUT_DIR, exist_ok=True)
    log_path = os.path.join(OUT_DIR, "build.log")
    started = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"build started {time.strftime('%Y-%m-%d %H:%M:%S')}\n{' '.join(nuitka_args())}\n\n")
        log.flush()
        rc = subprocess.run(nuitka_args(), cwd=KL_ROOT, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL).returncode
        log.write(f"\nbuild finished rc={rc} in {time.time() - started:.0f}s\n")
    print(f"rc={rc}, {time.time() - started:.0f}s, log: {log_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
