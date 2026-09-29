"""Build the Windows standalone folder with Nuitka.

Run with the build venv's Python (it holds requirements.txt + Nuitka):
    J:\\Excluded\\KopiaLens-Build\\venv\\Scripts\\python.exe packaging\\build_windows.py

Output: <OUT_DIR>\\kopialens.dist\\kopialens.exe, plus build.log and a compilation report.
"""
import os
import subprocess
import sys
import time

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("KOPIALENS_BUILD_OUT") or r"J:\Excluded\KopiaLens-Build\out"
VERSION = "0.0.0.1"


def nuitka_args() -> list[str]:
    # Opt-in: Nuitka's own tool downloads (Dependency Walker; one-time, cached per user).
    downloads = ["--assume-yes-for-downloads"] if os.environ.get("KOPIALENS_BUILD_ALLOW_DOWNLOADS") else []
    return [
        sys.executable, "-m", "nuitka",
        *downloads,
        "--standalone",
        f"--output-dir={OUT_DIR}",
        "--output-filename=kopialens.exe",
        "--windows-console-mode=attach",
        # kopialens.py dispatches "-m app.index.worker" itself; Nuitka blocks self "-m" calls by default.
        "--no-deployment-flag=self-execution",
        f"--include-data-dir={os.path.join(KL_ROOT, 'frontend')}=frontend",
        # Loaded by name at runtime (dataupgrade._STEP_MODULES), so not found by import tracing.
        "--include-module=app.core.encryption_upgrade",
        # Our own test packages only - a *.tests wildcard also drops jinja2.tests, which jinja2 needs.
        "--nofollow-import-to=app.tests",
        "--nofollow-import-to=app.backblaze.tests",
        "--nofollow-import-to=pytest",
        "--product-name=KopiaLens",
        "--file-description=KopiaLens",
        f"--file-version={VERSION}",
        f"--product-version={VERSION}",
        f"--report={os.path.join(OUT_DIR, 'compilation-report.xml')}",
        os.path.join(KL_ROOT, "kopialens.py"),
    ]


def main() -> int:
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
