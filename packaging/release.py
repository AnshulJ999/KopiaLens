"""One command for a Windows release: banned licence ids, tests, server build, bundled kopia, desktop
app + installer, the signed in-app update file (latest.json), portable zip, a smoke test of the built app,
checksums.

    python packaging\\release.py

Run with the dev Python (it runs the tests); the server is compiled with the build venv
(KOPIALENS_BUILD_PYTHON). Output: <BUILD_ROOT>\\release\\<version>\\ plus release.log there.
KOPIALENS_RELEASE_SKIP_TESTS=1 skips the test step while iterating on packaging. --with-notes makes a
testing build with the Notes widget (packaging/notes_bundle.py): its files are named -testing and it
writes no latest.json, so it can never be published as an update. The updater key is
private/updater.key (or TAURI_SIGNING_PRIVATE_KEY); its password is asked once and can be remembered
(packaging/updater_key.py).
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notes_bundle  # noqa: E402
import updater_key  # noqa: E402
from datetime import datetime, timezone

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(KL_ROOT))
SHELL = os.path.join(KL_ROOT, "shell", "src-tauri")
BUILD_ROOT = os.environ.get("KOPIALENS_BUILD_ROOT") or r"J:\Excluded\KopiaLens-Build"
BUILD_PYTHON = os.environ.get("KOPIALENS_BUILD_PYTHON") or os.path.join(BUILD_ROOT, "venv", "Scripts", "python.exe")
OUT_DIR = os.path.join(BUILD_ROOT, "out")
# Inside the shell folder (git-ignored): Tauri resource paths must be relative to src-tauri.
STAGE = os.path.join(SHELL, "release-stage")
SHELL_TARGET = os.path.join(BUILD_ROOT, "shell-target")
PORTABLE_MARKER = "KopiaLens-portable.txt"
KEYS_FILE = os.path.join(KL_ROOT, "app", "licensing", "keys.py")
LICENSEKIT_DIR = os.path.join(REPO_ROOT, "LicenseKit")
SYNC_MAX_AGE_DAYS = 7
RELEASE_REPO_URL = "https://github.com/AnshulJ999/KopiaLens"   # config.REPO_URL
# tauri.conf.json carries the dev identity ("KopiaLens Dev"), so a source build never shares the
# installed app's single-instance lock, startup entry or browser profile; releases use the real one.
RELEASE_IDENTITY = {"productName": "KopiaLens", "identifier": "com.kopialens.desktop"}

_log_fh = None


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def fail(msg: str) -> None:
    log(f"FAILED: {msg}")
    sys.exit(1)


def run(args: list[str], cwd: str, log_name: str, env: dict | None = None) -> None:
    """Runs a step with its output in its own log file."""
    path = os.path.join(BUILD_ROOT, "logs", log_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    started = time.time()
    with open(path, "w", encoding="utf-8") as fh:
        rc = subprocess.run(args, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env={**os.environ, **(env or {})}).returncode
    log(f"  {log_name}: rc={rc} in {time.time() - started:.0f}s")
    if rc != 0:
        fail(f"{' '.join(args[:3])} failed - see {path}")


# ---------------------------------------------------------------- versions

def versions() -> dict:
    with open(os.path.join(KL_ROOT, "VERSION"), encoding="utf-8") as fh:
        app = fh.read().strip()
    with open(os.path.join(SHELL, "Cargo.toml"), encoding="utf-8") as fh:
        cargo = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M).group(1)
    with open(os.path.join(SHELL, "tauri.conf.json"), encoding="utf-8") as fh:
        tauri = json.load(fh)["version"]
    with open(os.path.join(KL_ROOT, "app", "core", "kopiabin.py"), encoding="utf-8") as fh:
        kopia = re.search(r'^TESTED_VERSION\s*=\s*"([^"]+)"', fh.read(), re.M).group(1)
    return {"app": app, "cargo": cargo, "tauri": tauri, "kopia": kopia}


def updater_keys() -> tuple[str, str]:
    """The updater public key in tauri.conf.json and the server's copy in checkin.py."""
    with open(os.path.join(SHELL, "tauri.conf.json"), encoding="utf-8") as fh:
        shell_key = json.load(fh).get("plugins", {}).get("updater", {}).get("pubkey", "")
    with open(os.path.join(KL_ROOT, "app", "core", "checkin.py"), encoding="utf-8") as fh:
        m = re.search(r'^UPDATE_PUBKEY = "([^"]*)"', fh.read(), re.M)
    return shell_key, m.group(1) if m else ""


def write_update_file(dest: str, version: str, installer: str, setup_name: str) -> None:
    """latest.json for the in-app updater, attached to the GitHub release (the check-in Worker passes
    it along). The signature is Tauri's .sig of the installer."""
    sig_path = installer + ".sig"
    if not os.path.isfile(sig_path):
        fail(f"no updater signature next to {installer}")
    with open(sig_path, encoding="utf-8") as fh:
        signature = fh.read().strip()
    doc = {"version": version, "notes": "", "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "platforms": {"windows-x86_64": {
               "signature": signature,
               "url": f"{RELEASE_REPO_URL}/releases/download/v{version}/{setup_name}"}}}
    with open(os.path.join(dest, "latest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2)


# ---------------------------------------------------------------- banned licences

BANNED_LINE = re.compile(r"^BANNED_IDS: frozenset = frozenset\((?:\{[^}]*\})?\)$", re.M)


def with_banned_ids(keys_text: str, ids) -> str:
    """keys.py text with BANNED_IDS set to ids."""
    body = "{\n" + "".join(f'    "{i}",\n' for i in sorted(ids)) + "}" if ids else ""
    new, n = BANNED_LINE.subn(f"BANNED_IDS: frozenset = frozenset({body})", keys_text)
    if n != 1:
        fail("BANNED_IDS line not found in app/licensing/keys.py")
    return new


def bake_banned_ids() -> None:
    """Builds the issuing tool's banned list (own bans + last service sync) into this release, for installs
    that never fetch the revoked list."""
    sys.path.insert(0, LICENSEKIT_DIR)
    from issuer.core import Issuer
    issuer = Issuer()
    ids = issuer.banned_ids()
    snap = issuer.service_snapshot() or {}
    fetched = snap.get("fetched_at")
    age = None
    if fetched:
        synced = datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - synced).total_seconds() / 86400
    if age is None or age > SYNC_MAX_AGE_DAYS:
        log(f"  WARNING: licence service last synced {'never' if age is None else f'{age:.0f} days ago'}; "
            "sync in the issuing tool first so recent refunds are included")
    with open(KEYS_FILE, encoding="utf-8") as fh:
        text = fh.read()
    with open(KEYS_FILE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(with_banned_ids(text, ids))
    log(f"  {len(ids)} banned licence(s) built in (service: {snap.get('url') or 'not synced'})")


# ---------------------------------------------------------------- kopia

def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_kopia(version: str) -> str:
    """The pinned kopia release zip, downloaded once and checked against kopia's checksums.txt."""
    cache = os.path.join(BUILD_ROOT, "cache", f"kopia-{version}")
    os.makedirs(cache, exist_ok=True)
    name = f"kopia-{version}-windows-x64.zip"
    base = f"https://github.com/kopia/kopia/releases/download/v{version}/"
    for fname in ("checksums.txt", name):
        dest = os.path.join(cache, fname)
        if not os.path.isfile(dest):
            log(f"  downloading {base}{fname}")
            urllib.request.urlretrieve(base + fname, dest + ".part")
            os.replace(dest + ".part", dest)
    with open(os.path.join(cache, "checksums.txt"), encoding="utf-8") as fh:
        want = next((ln.split()[0] for ln in fh if ln.strip().endswith(name)), None)
    zpath = os.path.join(cache, name)
    if not want or sha256(zpath) != want:
        fail(f"kopia {name} does not match kopia's checksums.txt")
    log(f"  kopia {version} checksum OK")
    return zpath


def stage_kopia(zpath: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        for member in zf.namelist():
            base = os.path.basename(member)
            if base in ("kopia.exe", "LICENSE"):
                with zf.open(member) as src, open(os.path.join(dest, base), "wb") as out:
                    shutil.copyfileobj(src, out)
    for need in ("kopia.exe", "LICENSE"):
        if not os.path.isfile(os.path.join(dest, need)):
            fail(f"{need} not found in {zpath}")


# ---------------------------------------------------------------- smoke test

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url: str, method: str = "GET", timeout: float = 5.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def is_up(url: str) -> bool:
    try:
        return http(url, timeout=1)[0] == 200
    except OSError:
        return False


def wait(check, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if check():
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def smoke_test(zip_path: str, version: str) -> None:
    """Unpacks the portable zip and runs it isolated (own ports and data, beside any running
    KopiaLens): server up with the right version, page renders, data stays in the portable
    folder, bundled kopia found, then a clean quit through the control port."""
    root = os.path.join(BUILD_ROOT, "smoke", time.strftime("%Y%m%d-%H%M%S"))
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(root)
    app_dir = os.path.join(root, "KopiaLens")
    port, ctl = free_port(), free_port()
    env = {**os.environ, "KOPIALENS_PORT": str(port), "KOPIALENS_TRAY_PORT": str(ctl),
           "KOPIALENS_SINGLE_INSTANCE": "0"}
    for k in ("KOPIALENS_DATA_DIR", "KOPIALENS_CONFIG_DIR", "KOPIALENS_HOME", "KOPIALENS_REPOS_FILE"):
        env.pop(k, None)
    proc = subprocess.Popen([os.path.join(app_dir, "KopiaLens.exe"), "--hidden"], cwd=app_dir, env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait(lambda: http(base + "/health")[0] == 200, 90):
            fail("smoke: the server did not answer /health within 90 s")
        health = json.loads(http(base + "/health")[1])
        if health.get("version") != version:
            fail(f"smoke: /health reports {health.get('version')}, expected {version}")
        status, page = http(base + "/")
        if status != 200 or b"<html" not in page[:2000].lower():
            fail(f"smoke: / returned {status}")
        status, body = http(base + "/api/app/about")
        if status == 200:
            about = json.loads(body)
            if not os.path.normcase(about["data_dir"]).startswith(os.path.normcase(app_dir)):
                fail(f"smoke: data folder {about['data_dir']} is outside the portable folder")
            if os.path.normcase(app_dir) not in os.path.normcase(json.dumps(about.get("kopia"))).replace("\\\\", "\\"):
                fail(f"smoke: bundled kopia not in use: {about.get('kopia')}")
            if not about.get("compiled"):
                fail("smoke: the server is not the compiled build")
        else:
            fail(f"smoke: /api/app/about returned {status}")
        cli = subprocess.run([os.path.join(app_dir, "bin", "klens.exe"), "--url", base, "version"],
                             capture_output=True, text=True, timeout=60)
        if cli.returncode != 0 or json.loads(cli.stdout or "{}").get("kopialens") != version:
            fail(f"smoke: bin/klens.exe version failed ({cli.returncode}): {cli.stderr.strip()[:200]}")
        log(f"  smoke: version {version}, page OK, portable data + bundled kopia OK, klens OK")
        http(f"http://127.0.0.1:{ctl}/quit", "POST")
        if not wait(lambda: proc.poll() is not None, 30):
            fail("smoke: the desktop app did not quit within 30 s")
        if not wait(lambda: not is_up(base + "/health"), 20):
            fail("smoke: the server kept running after quit")
    finally:
        if proc.poll() is None:
            proc.kill()
    log("  smoke: clean quit OK")


# ---------------------------------------------------------------- main

def main() -> int:
    global _log_fh
    v = versions()
    version = v["app"]
    with_notes = "--with-notes" in sys.argv[1:]
    tag = "-testing" if with_notes else ""
    dest = os.path.join(BUILD_ROOT, "release", version + tag)
    os.makedirs(dest, exist_ok=True)
    _log_fh = open(os.path.join(dest, "release.log"), "w", encoding="utf-8")
    log(f"KopiaLens {version} release build (kopia {v['kopia']})")
    if with_notes:
        log("  TESTING BUILD: the Notes widget is included. Never publish it; it writes no latest.json.")

    if len({v["app"], v["cargo"], v["tauri"]}) != 1:
        fail(f"versions differ: VERSION {v['app']}, Cargo.toml {v['cargo']}, tauri.conf.json {v['tauri']}")
    shell_key, server_key = updater_keys()
    if not shell_key:
        fail("no updater public key in shell/src-tauri/tauri.conf.json (plugins.updater.pubkey)")
    if shell_key != server_key:
        fail("UPDATE_PUBKEY in app/core/checkin.py differs from the updater key in tauri.conf.json")
    key = updater_key.key_path()
    if not key:
        fail(f"no updater key: {updater_key.KEY_FILE} is missing and TAURI_SIGNING_PRIVATE_KEY is not set")
    key_password = updater_key.password()
    if key_password is None:
        fail("the updater key could not be opened, so installs could not update to this release")
    signing_env = {"TAURI_SIGNING_PRIVATE_KEY": key, "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": key_password}

    log("0/6 banned licence ids")
    bake_banned_ids()

    if os.environ.get("KOPIALENS_RELEASE_SKIP_TESTS"):
        log("1/6 tests SKIPPED (KOPIALENS_RELEASE_SKIP_TESTS)")
    else:
        log("1/6 tests")
        run([sys.executable, "-m", "pytest", os.path.relpath(KL_ROOT, REPO_ROOT).replace("\\", "/"), "-q",
             "-p", "no:cacheprovider"], REPO_ROOT, "tests.log")

    log("2/6 server build (Nuitka)")
    notes_env = {}
    if with_notes:
        notes_env["KOPIALENS_BUILD_NOTES"] = notes_bundle.stage(os.path.join(BUILD_ROOT, "devnotes-stage"))
    run([BUILD_PYTHON, os.path.join(KL_ROOT, "packaging", "build_server.py")], KL_ROOT, "nuitka.log",
        {"KOPIALENS_BUILD_OUT": OUT_DIR, "KOPIALENS_BUILD_NOTES": "", **notes_env})

    log("3/6 stage server + bundled kopia + command-line launchers")
    server = os.path.join(STAGE, "server")
    shutil.rmtree(STAGE, ignore_errors=True)
    shutil.copytree(os.path.join(OUT_DIR, "kopialens.dist"), server)
    stage_kopia(fetch_kopia(v["kopia"]), os.path.join(server, "kopia"))
    launcher_target = os.path.join(BUILD_ROOT, "launcher-target")
    run(["cargo", "build", "--release", "--manifest-path", os.path.join(KL_ROOT, "shell", "cli-launcher", "Cargo.toml")],
        KL_ROOT, "launcher.log", {"CARGO_TARGET_DIR": launcher_target})
    bindir = os.path.join(STAGE, "bin")
    os.makedirs(bindir)
    for name in ("klens.exe", "kopialens-cli.exe"):
        shutil.copy2(os.path.join(launcher_target, "release", "klens.exe"), os.path.join(bindir, name))

    log("4/6 desktop app + installer (Tauri)")
    release_conf = os.path.join(STAGE, "tauri.release.json")
    with open(release_conf, "w", encoding="utf-8") as fh:
        json.dump({**RELEASE_IDENTITY,
                   "bundle": {"active": True, "targets": ["nsis"], "createUpdaterArtifacts": True,
                              "resources": {"release-stage/server/": "server/",
                                            "release-stage/bin/": "bin/"}}}, fh)
    run(["cargo", "tauri", "build", "--config", release_conf, "--bundles", "nsis"], SHELL, "tauri.log",
        {"CARGO_TARGET_DIR": SHELL_TARGET, **signing_env})
    exe = os.path.join(SHELL_TARGET, "release", "KopiaLens.exe")
    installers = [os.path.join(SHELL_TARGET, "release", "bundle", "nsis", f)
                  for f in os.listdir(os.path.join(SHELL_TARGET, "release", "bundle", "nsis"))
                  if f.endswith("-setup.exe") and f"_{version}_" in f]
    if not os.path.isfile(exe) or len(installers) != 1:
        fail(f"Tauri output missing: {exe}, installers {installers}")
    setup_name = f"KopiaLens-{version}{tag}-windows-x64-setup.exe"
    shutil.copy2(installers[0], os.path.join(dest, setup_name))
    if not with_notes:
        write_update_file(dest, version, installers[0], setup_name)

    log("5/6 portable zip")
    zip_name = f"KopiaLens-{version}{tag}-windows-x64-portable.zip"
    zip_path = os.path.join(dest, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(exe, "KopiaLens/KopiaLens.exe")
        zf.writestr(f"KopiaLens/{PORTABLE_MARKER}",
                    "This file makes KopiaLens keep its settings and index in the data folder next to it.\r\n"
                    "Without it, KopiaLens uses its normal per-user folder.\r\n"
                    "To keep them somewhere else, add a line like: data=D:\\KopiaLens data\r\n")
        for dirpath, _dirs, files in os.walk(server):
            for f in files:
                full = os.path.join(dirpath, f)
                zf.write(full, "KopiaLens/server/" + os.path.relpath(full, server).replace("\\", "/"))
        for f in os.listdir(bindir):
            zf.write(os.path.join(bindir, f), "KopiaLens/bin/" + f)

    # The command line on its own, for any machine with Python 3 (standard library only).
    with open(os.path.join(KL_ROOT, "klens.py"), encoding="utf-8") as fh:
        cli_src = fh.read()
    if '__version__ = "dev"' not in cli_src:
        fail("klens.py: its version line changed; update the stamp in release.py")
    with open(os.path.join(dest, "klens.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(cli_src.replace('__version__ = "dev"', f'__version__ = "{version}"', 1))

    log("6/6 smoke test of the portable build")
    smoke_test(zip_path, version)

    with open(os.path.join(dest, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as fh:
        for name in (setup_name, zip_name, "klens.py"):
            fh.write(f"{sha256(os.path.join(dest, name))}  {name}\n")
    for name in (setup_name, zip_name):
        log(f"  {name}  {os.path.getsize(os.path.join(dest, name)) / 1e6:.0f} MB")
    shutil.rmtree(STAGE, ignore_errors=True)  # ~200 MB copy inside the repo folder, rebuilt each run
    log(f"DONE: {dest}")
    if with_notes:
        log("  TESTING BUILD: for test copies only; do not publish.")
        return 0
    log(f"  publish: a GitHub release tagged v{version} on {RELEASE_REPO_URL} with {setup_name}, {zip_name},"
        " SHA256SUMS.txt and latest.json (installs update from its latest.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
