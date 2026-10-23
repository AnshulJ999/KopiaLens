"""Build the KopiaLens server as a Nuitka standalone folder (Windows, Linux, macOS).

Run with the build venv's Python (it holds requirements.txt + Nuitka):
    J:\\Excluded\\KopiaLens-Build\\venv\\Scripts\\python.exe packaging\\build_server.py

Output: <OUT_DIR>/kopialens.dist/kopialens-server(.exe), plus build.log and a compilation report.
packaging/release.py runs this as its first build step; the Docker image runs it in its build stage.
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import time
from importlib import metadata

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("KOPIALENS_BUILD_OUT") or r"J:\Excluded\KopiaLens-Build\out"
COMMIT_FILE = os.path.join(OUT_DIR, "COMMIT")   # the commit shown in About, for a copy with no git
sys.path.insert(0, KL_ROOT)
import appversion  # noqa: E402
WINDOWS = os.name == "nt"
SERVER_EXE = "kopialens-server.exe" if WINDOWS else "kopialens-server"


def app_version() -> str:
    return appversion.version()


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


def notes_folder() -> str:
    """The Notes widget (Settings > Experimental) ships in every build: a folder staged by
    notes_bundle.py (KOPIALENS_BUILD_NOTES; the Docker build stages it first), else staged here."""
    sys.path.insert(0, os.path.join(KL_ROOT, "packaging"))
    import notes_bundle
    folder = os.environ.get("KOPIALENS_BUILD_NOTES", "").strip()
    if not folder:
        folder = notes_bundle.stage(os.path.join(OUT_DIR, "devnotes-stage"),
                                    testing=bool(os.environ.get("KOPIALENS_BUILD_TESTING")))
    if not notes_bundle.is_staged(folder):
        raise SystemExit(f"{folder} is not a staged Notes folder (python packaging/notes_bundle.py <folder>)")
    return folder


def tracked(name: str) -> str:
    """A copy of KL_ROOT/<name> holding only the files git tracks, so git-ignored leftovers in a
    developer's checkout never ship. Outside a git checkout (the Docker build) the folder itself."""
    src = os.path.join(KL_ROOT, name)
    try:
        out = subprocess.run(["git", "-C", KL_ROOT, "ls-files", "-z", "--", name],
                             capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return src
    files = [f for f in out.decode("utf-8").split("\0") if f]
    if not files:
        return src
    root = os.path.join(OUT_DIR, "tracked")
    shutil.rmtree(os.path.join(root, name), ignore_errors=True)   # our own copy from an earlier run
    for rel in files:
        path = os.path.join(KL_ROOT, rel)
        if os.path.isfile(path):   # a tracked file deleted in the checkout ships nothing
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            shutil.copy2(path, os.path.join(root, rel))
    return os.path.join(root, name)


def notes_args(folder: str) -> list[str]:
    return [f"--include-data-dir={os.path.join(folder, 'web')}=devnotes", "--include-package=notes_server"]


def nuitka_args(notes: str) -> list[str]:
    # Windows file details take numbers only: a beta (1.2.0-beta.1) is stamped 1.2.0 there.
    numeric = app_version().split("+")[0].split("-")[0]
    # Opt-in: Nuitka's own tool downloads (Dependency Walker; one-time, cached per user).
    downloads = ["--assume-yes-for-downloads"] if os.environ.get("KOPIALENS_BUILD_ALLOW_DOWNLOADS") else []
    windows = [
        "--windows-console-mode=attach",
        f"--windows-icon-from-ico={os.path.join(KL_ROOT, 'shell', 'src-tauri', 'icons', 'icon.ico')}",
        "--product-name=KopiaLens",
        "--file-description=KopiaLens server",
        f"--file-version={numeric}",
        f"--product-version={numeric}",
    ] if WINDOWS else []
    return [
        sys.executable, "-m", "nuitka",
        *downloads,
        "--standalone",
        f"--output-dir={OUT_DIR}",
        f"--output-filename={SERVER_EXE}",
        *windows,
        # kopialens.py dispatches "-m app.index.worker" itself; Nuitka blocks self "-m" calls by default.
        "--no-deployment-flag=self-execution",
        "--python-flag=no_docstrings",
        f"--include-data-dir={tracked('frontend')}=frontend",
        f"--include-data-dir={tracked('docs')}=docs",
        f"--include-data-files={os.path.join(KL_ROOT, 'VERSION')}=VERSION",
        *([f"--include-data-files={COMMIT_FILE}=COMMIT"] if appversion.commit() else []),
        f"--include-data-files={os.path.join(KL_ROOT, 'app', 'core', 'sample_profile.json')}=app/core/sample_profile.json",
        *[f"--include-module={m}" for m in upgrade_step_modules()],
        *notes_args(notes),
        # Our own test packages only - a *.tests wildcard also drops jinja2.tests, which jinja2 needs.
        "--nofollow-import-to=app.tests",
        "--nofollow-import-to=app.backblaze.tests",
        "--nofollow-import-to=pytest",
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
    appversion.stamp()
    commit = appversion.commit()
    if commit:
        with open(COMMIT_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(commit + "\n")
    elif os.path.exists(COMMIT_FILE):
        os.remove(COMMIT_FILE)   # our own build output from an earlier run
    notes = notes_folder()
    # notes_server is compiled in from the staged folder.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        p for p in (os.path.join(notes, "py"), os.environ.get("PYTHONPATH", "")) if p)}
    args = nuitka_args(notes)
    log_path = os.path.join(OUT_DIR, "build.log")
    started = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"build started {time.strftime('%Y-%m-%d %H:%M:%S')}\n{' '.join(args)}\n\n")
        log.flush()
        rc = subprocess.run(args, cwd=KL_ROOT, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env).returncode
        log.write(f"\nbuild finished rc={rc} in {time.time() - started:.0f}s\n")
    print(f"rc={rc}, {time.time() - started:.0f}s, log: {log_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
