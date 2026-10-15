"""Tests the Windows installer inside Windows Sandbox, a throwaway Windows that shares nothing with this
PC except two folders: PATH edits on PATHs longer than an NSIS string in both install modes (install,
reinstall, update, uninstall, no PATH), and closing a running copy without touching an unrelated
program of the same name. The checks themselves: installer_test_sandbox.ps1.

    python packaging\\installer_test.py [path\\to\\setup.exe]

Without a path it tests the newest installer under <BUILD_ROOT>\\release. release.py runs it when the
installer's files changed since the last pass, or with --installer-test. Needs the Windows Sandbox
feature; only one Sandbox runs at a time.
"""
import glob
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
KL_ROOT = os.path.dirname(HERE)
BUILD_ROOT = os.environ.get("KOPIALENS_BUILD_ROOT") or r"J:\Excluded\KopiaLens-Build"
WORK = os.path.join(BUILD_ROOT, "installer-test")
PASS_FILE = os.path.join(WORK, "passed.json")
SANDBOX_SCRIPT = os.path.join(HERE, "installer_test_sandbox.ps1")
# A change to any of these means the installer needs testing again.
INPUTS = [
    os.path.join(KL_ROOT, "shell", "src-tauri", "windows", "installer.nsi"),
    os.path.join(KL_ROOT, "shell", "src-tauri", "windows", "nsis-plugins", "x86-unicode", "EnVar.dll"),
    os.path.abspath(__file__),
    SANDBOX_SCRIPT,
]
TIMEOUT = 20 * 60
START_TIMEOUT = 5 * 60   # no log line by then: the Sandbox never ran the script


def available() -> bool:
    """Windows Sandbox registers the .wsb file type once the feature is on (after a restart).
    Without it, opening a .wsb shows Windows' "open with" box instead of failing."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ".wsb"):
            return True
    except OSError:
        return False


def inputs_hash() -> str:
    h = hashlib.sha256()
    for path in INPUTS:
        with open(path, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def last_pass() -> dict | None:
    try:
        with open(PASS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def needed() -> bool:
    """True when the installer's files changed since the last passing run."""
    passed = last_pass()
    return not passed or passed.get("hash") != inputs_hash()


def _config(in_dir: str, out_dir: str) -> str:
    return f"""<Configuration>
  <MemoryInMB>4096</MemoryInMB>
  <MappedFolders>
    <MappedFolder><HostFolder>{escape(in_dir)}</HostFolder><SandboxFolder>C:\\kltest\\in</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>{escape(out_dir)}</HostFolder><SandboxFolder>C:\\kltest\\out</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\kltest\\in\\installer_test_sandbox.ps1</Command></LogonCommand>
</Configuration>
"""


def run(setup: str, log=print) -> str:
    """Runs the checks on one installer: "passed", "failed", "unavailable" (no Windows Sandbox) or
    "timeout". A pass is recorded, so release.py can skip an unchanged installer."""
    if not available():
        log("  installer test: Windows Sandbox isn't set up (turn it on in Windows features, then restart)")
        return "unavailable"
    run_dir = os.path.join(WORK, time.strftime("%Y%m%d-%H%M%S"))
    in_dir, out_dir = os.path.join(run_dir, "in"), os.path.join(run_dir, "out")
    os.makedirs(in_dir)
    os.makedirs(out_dir)
    shutil.copy2(setup, os.path.join(in_dir, "setup.exe"))
    shutil.copy2(SANDBOX_SCRIPT, in_dir)
    wsb = os.path.join(run_dir, "installer-test.wsb")
    with open(wsb, "w", encoding="utf-8") as fh:
        fh.write(_config(in_dir, out_dir))
    log(f"  installer test: {os.path.basename(setup)} in Windows Sandbox ({run_dir})")
    try:
        os.startfile(wsb)
    except OSError as e:
        log(f"  installer test: Windows Sandbox didn't start ({e})")
        return "unavailable"

    result_path, log_path = os.path.join(out_dir, "result.json"), os.path.join(out_dir, "log.txt")
    started = time.time()
    shown, end = 0, started + TIMEOUT
    while time.time() < end:
        time.sleep(3)
        if not os.path.isfile(log_path) and time.time() - started > START_TIMEOUT:
            log(f"  installer test: the Sandbox didn't run the test within {START_TIMEOUT // 60} min;"
                " close its window if one is open")
            return "unavailable"
        if os.path.isfile(log_path):
            with open(log_path, encoding="utf-8-sig", errors="replace") as fh:
                lines = fh.read().splitlines()
            for line in lines[shown:]:
                log(f"    {line}")
            shown = len(lines)
        if os.path.isfile(result_path):
            break
    else:
        log(f"  installer test: no result after {TIMEOUT // 60} min; close the Sandbox window yourself")
        return "timeout"

    with open(result_path, encoding="utf-8-sig") as fh:
        result = json.load(fh)
    failed = [c for c in result.get("checks") or [] if not c.get("ok")]
    if result.get("error"):
        log(f"  installer test: the Sandbox script stopped: {result['error']}")
    for c in failed:
        log(f"  FAIL {c.get('name')}: {c.get('detail')}")
    if not result.get("ok"):
        return "failed"
    with open(PASS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"hash": inputs_hash(), "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "installer": os.path.basename(setup), "checks": len(result["checks"])}, fh, indent=2)
    log(f"  installer test: all {len(result['checks'])} checks passed")
    return "passed"


def newest_installer() -> str | None:
    found = glob.glob(os.path.join(BUILD_ROOT, "release", "*", "*-setup.exe"))
    return max(found, key=os.path.getmtime) if found else None


def main() -> int:
    setup = sys.argv[1] if len(sys.argv) > 1 else newest_installer()
    if not setup or not os.path.isfile(setup):
        print("No installer found. Pass the path of a KopiaLens setup.exe.")
        return 2
    return 0 if run(setup) == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
