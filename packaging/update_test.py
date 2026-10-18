"""Tests a real in-app update inside Windows Sandbox: installs the old build, lets it find the new one
on a small update server inside the Sandbox, installs it through the app's Install button and checks
that the new version runs with the settings kept. Nothing on this PC is touched.

    python packaging\\update_test.py <old -updatetest setup.exe> <new release folder>

The old build is an update-test build pointed at the Sandbox's server:
    release.py --update-endpoint=http://127.0.0.1:8765/v1/update/{{target}}-{{arch}}/{{current_version}}
The new one is a normal release build (its folder has latest.json and the setup.exe it names).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_test  # noqa: E402

PORT = 8765
WORK = os.path.join(installer_test.BUILD_ROOT, "update-test")
SCRIPT = "update_test_sandbox.ps1"
SERVER = "update_test_server.ps1"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    old_setup, new_dir = sys.argv[1], sys.argv[2]
    with open(os.path.join(new_dir, "latest.json"), encoding="utf-8") as fh:
        latest = json.load(fh)
    win = latest["platforms"]["windows-x86_64"]
    new_setup = os.path.join(new_dir, win["url"].rsplit("/", 1)[1])
    if not os.path.isfile(old_setup) or not os.path.isfile(new_setup):
        print(f"Missing installer: {old_setup if not os.path.isfile(old_setup) else new_setup}")
        return 2
    # The update is downloaded from the Sandbox's own server; the signature covers the file, not the address.
    win["url"] = f"http://127.0.0.1:{PORT}/files/new-setup.exe"
    tmp = tempfile.mkdtemp(prefix="kl-update-test-")
    served = os.path.join(tmp, "latest.json")
    with open(served, "w", encoding="utf-8") as fh:
        json.dump(latest, fh)
    files = {"old-setup.exe": old_setup, "new-setup.exe": new_setup, "latest.json": served,
             SERVER: os.path.join(installer_test.HERE, SERVER)}
    print(f"Update test: {os.path.basename(old_setup)} -> {latest['version']}")
    result = installer_test.run_sandbox("update test", files, SCRIPT, WORK)
    if isinstance(result, str):
        return 1
    print(f"Update test {'passed' if result.get('ok') else 'FAILED'}: "
          f"{sum(1 for c in result.get('checks') or [] if c.get('ok'))}/{len(result.get('checks') or [])} checks")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
