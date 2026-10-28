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
import subprocess
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
    os.path.join(KL_ROOT, "shell", "cli-launcher", "src", "main.rs"),
    os.path.join(KL_ROOT, "shell", "cli-launcher", "src", "installer_path.rs"),
    os.path.abspath(__file__),
    SANDBOX_SCRIPT,
]
TIMEOUT = 20 * 60
START_TIMEOUT = 5 * 60   # no log line by then: the Sandbox never ran the script


def _sandbox_exe() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsSandbox.exe")


def available() -> bool:
    """The Windows Sandbox feature is on. Started directly: the .wsb file type isn't always registered."""
    return os.path.isfile(_sandbox_exe())


def _sandbox_running() -> bool:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return "windowssandbox" in out.lower()


def _wait_closed(seconds: int) -> bool:
    end = time.time() + seconds
    while _sandbox_running():
        if time.time() > end:
            return False
        time.sleep(2)
    return True


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


def _config(in_dir: str, out_dir: str, script: str) -> str:
    return f"""<Configuration>
  <MemoryInMB>4096</MemoryInMB>
  <vGPU>Disable</vGPU>
  <MappedFolders>
    <MappedFolder><HostFolder>{escape(in_dir)}</HostFolder><SandboxFolder>C:\\kltest\\in</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>{escape(out_dir)}</HostFolder><SandboxFolder>C:\\kltest\\out</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\kltest\\in\\{escape(script)}</Command></LogonCommand>
</Configuration>
"""


def run_sandbox(name: str, files: dict[str, str], script: str, work: str, log=print) -> dict | str:
    """Runs ``script`` (copied in from this folder) in Windows Sandbox with ``files`` ({name in the
    Sandbox's C:\\kltest\\in: path here}). The script's result.json, or "unavailable" / "timeout"."""
    if not available():
        log(f"  {name}: Windows Sandbox isn't set up (turn it on in Windows features, then restart)")
        return "unavailable"
    # Windows runs one Sandbox at a time; a second start only shows an error box.
    if not _wait_closed(60):
        log(f"  {name}: another Windows Sandbox is open; close it and run the test again")
        return "unavailable"
    run_dir = os.path.join(work, time.strftime("%Y%m%d-%H%M%S"))
    in_dir, out_dir = os.path.join(run_dir, "in"), os.path.join(run_dir, "out")
    os.makedirs(in_dir)
    os.makedirs(out_dir)
    for dest, src in files.items():
        shutil.copy2(src, os.path.join(in_dir, dest))
    shutil.copy2(os.path.join(HERE, script), in_dir)
    wsb = os.path.join(run_dir, "sandbox.wsb")
    with open(wsb, "w", encoding="utf-8") as fh:
        fh.write(_config(in_dir, out_dir, script))
    log(f"  {name}: Windows Sandbox ({run_dir})")
    try:
        subprocess.Popen([_sandbox_exe(), wsb])
    except OSError as e:
        log(f"  {name}: Windows Sandbox didn't start ({e})")
        return "unavailable"

    result_path, log_path = os.path.join(out_dir, "result.json"), os.path.join(out_dir, "log.txt")
    started = time.time()
    shown, end = 0, started + TIMEOUT
    while time.time() < end:
        time.sleep(3)
        if not os.path.isfile(log_path) and time.time() - started > START_TIMEOUT:
            log(f"  {name}: the Sandbox didn't run the test within {START_TIMEOUT // 60} min;"
                " close its window if one is open")
            return "unavailable"
        if os.path.isfile(log_path):
            try:
                with open(log_path, encoding="utf-8-sig", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except PermissionError:  # the Sandbox is writing to it right now
                continue
            for line in lines[shown:]:
                log(f"    {line}")
            shown = len(lines)
        if os.path.isfile(result_path):
            break
    else:
        log(f"  {name}: no result after {TIMEOUT // 60} min; close the Sandbox window yourself")
        return "timeout"

    with open(result_path, encoding="utf-8-sig") as fh:
        result = json.load(fh)
    _wait_closed(60)  # it shuts itself down after writing the result
    failed = [c for c in result.get("checks") or [] if not c.get("ok")]
    if result.get("error"):
        log(f"  {name}: the Sandbox script stopped: {result['error']}")
    for c in failed:
        log(f"  FAIL {c.get('name')}: {c.get('detail')}")
    crashes = os.path.join(out_dir, "crashes.txt")
    if os.path.isfile(crashes) and os.path.getsize(crashes):
        log(f"  program crashes inside the Sandbox: {crashes}")
    return result


def run(setup: str, log=print) -> str:
    """Runs the checks on one installer: "passed", "failed", "unavailable" (no Windows Sandbox) or
    "timeout". A pass is recorded, so release.py can skip an unchanged installer."""
    log(f"  installer test: {os.path.basename(setup)}")
    result = run_sandbox("installer test", {"setup.exe": setup}, os.path.basename(SANDBOX_SCRIPT), WORK, log)
    if isinstance(result, str):
        return result
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
