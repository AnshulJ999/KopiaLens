"""KopiaLens release build, in steps. With no step it is the one-command local Windows release; the
release pipeline (.github/workflows/kopialens-release.yml) runs the steps one at a time on each
platform, then `update-file` and `checksums` once over every platform's files.

    python packaging\\release.py                     full local Windows release (DEFAULT_STEPS)
    python packaging/release.py <step> [<step>...]   only these steps, in the order given
    python packaging/release.py --help               this text

Steps: check, banned, tests, server, stage, desktop, portable, cli, smoke, leak-scan, installer-test,
update-file, checksums, cleanup. Run with the dev Python (it runs the tests); the server is compiled
with the build venv (KOPIALENS_BUILD_PYTHON).

Version (appversion.py): KOPIALENS_RELEASE_VERSION (the pipeline passes the release tag) > the newest
kopialens/v* git tag > the VERSION file. `check` writes it into VERSION (the server ships that file)
and the Home Assistant add-on's config.yaml; `desktop` stamps it into the Tauri build, so Cargo.toml
and tauri.conf.json are never edited by hand.

Output: <BUILD_ROOT>/release/<version>/ (or KOPIALENS_RELEASE_DIR) plus release.log there.
KOPIALENS_RELEASE_SKIP_TESTS=1 skips the test step while iterating. A build carries the Notes widget
(Settings > Experimental, off by default) when its sources are present. --testing (old name --with-notes) makes a testing
build: Experimental and Notes start switched on, its files are named -testing and it writes no
update file, so it can never be published as an update. --update-endpoint=<url> makes an update-test
build (files named -updatetest, no update file) that asks only that address for updates
(packaging/update_test.py). The banned licence list comes from the
LicenseKit issuing tool on this PC, or from the licence service with KOPIALENS_BANNED_FROM=service
(the pipeline). The updater key is private/updater.key (or TAURI_SIGNING_PRIVATE_KEY); its password
is asked once and can be remembered (packaging/updater_key.py).
"""
import ast
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appversion  # noqa: E402
import installer_test  # noqa: E402
import notes_bundle  # noqa: E402
import updater_key  # noqa: E402
from datetime import datetime, timezone

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(KL_ROOT))
SHELL = os.path.join(KL_ROOT, "shell", "src-tauri")
PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
ARCH = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
WINDOWS = PLATFORM == "windows"
EXE = ".exe" if WINDOWS else ""
BUILD_ROOT = os.environ.get("KOPIALENS_BUILD_ROOT") or (
    r"J:\Excluded\KopiaLens-Build" if WINDOWS else os.path.expanduser("~/KopiaLens-Build"))
BUILD_PYTHON = os.environ.get("KOPIALENS_BUILD_PYTHON") or os.path.join(
    BUILD_ROOT, "venv", *(("Scripts", "python.exe") if WINDOWS else ("bin", "python")))
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
# Tauri bundle formats and the updater's platform key, per build machine.
BUNDLES = {"windows": ["nsis"], "linux": ["appimage", "deb", "rpm"], "macos": ["app", "dmg"]}[PLATFORM]
UPDATER_TARGET = ({"windows": "windows", "linux": "linux", "macos": "darwin"}[PLATFORM] + "-"
                  + {"x64": "x86_64", "arm64": "aarch64"}[ARCH])
KOPIA_ASSET = {"windows": "windows-x64.zip", "linux": f"linux-{ARCH}.tar.gz", "macos": f"macOS-{ARCH}.tar.gz"}[PLATFORM]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

DEFAULT_STEPS = ["check", "banned", "tests", "server", "stage", "desktop", "update-file", "portable", "cli",
                 "smoke", "leak-scan", "installer-test", "checksums", "cleanup"]

_log_fh = None
_ctx: dict = {}


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
        if os.environ.get("GITHUB_ACTIONS"):
            with open(path, encoding="utf-8", errors="replace") as fh:
                print(fh.read()[-6000:], flush=True)
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


def release_version() -> str:
    """The release tag (without "v") when the pipeline passes one, else appversion's answer (the
    newest kopialens/v* tag, else the VERSION file). An unreadable tag is kept, so `check` refuses it."""
    tag = (os.environ.get("KOPIALENS_RELEASE_VERSION") or "").strip()
    return (appversion.normalize(tag) or tag) if tag else appversion.version()


HA_CONFIG = os.path.join(KL_ROOT, "packaging", "homeassistant", "kopialens", "config.yaml")


def stamp_ha_config(version: str) -> None:
    """The add-on's config.yaml carries the version Home Assistant shows; it must match the image."""
    with open(HA_CONFIG, encoding="utf-8") as fh:
        text = fh.read()
    new, n = re.subn(r'^version: .*$', f'version: "{version}"', text, count=1, flags=re.M)
    if n != 1:
        fail(f"no version line in {HA_CONFIG}")
    if new != text:
        with open(HA_CONFIG, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new)
        log(f"  Home Assistant add-on version set to {version}")


def updater_keys() -> tuple[str, str]:
    """The updater public key in tauri.conf.json and the server's copy in checkin.py."""
    with open(os.path.join(SHELL, "tauri.conf.json"), encoding="utf-8") as fh:
        shell_key = json.load(fh).get("plugins", {}).get("updater", {}).get("pubkey", "")
    with open(os.path.join(KL_ROOT, "app", "core", "checkin.py"), encoding="utf-8") as fh:
        m = re.search(r'^UPDATE_PUBKEY = "([^"]*)"', fh.read(), re.M)
    return shell_key, m.group(1) if m else ""


def rust_env(*flags: str) -> dict:
    """Compiler flags for a Rust build: the builder's home folder is written as "~" and the checkout
    as "." in the binaries (cargo embeds the source paths of dependencies, the vendored ones included)."""
    home = os.path.expanduser("~")
    return {"CARGO_ENCODED_RUSTFLAGS": "\x1f".join(
        [*flags, f"--remap-path-prefix={home}=~", f"--remap-path-prefix={KL_ROOT}=."])}


def signing_env() -> dict:
    """Tauri's signing variables, checked once per run (the key must open)."""
    if "signing" not in _ctx:
        key = updater_key.key_path()
        if not key:
            fail(f"no updater key: {updater_key.KEY_FILE} is missing and TAURI_SIGNING_PRIVATE_KEY is not set")
        key_password = updater_key.password()
        if key_password is None:
            fail("the updater key could not be opened, so installs could not update to this release")
        _ctx["signing"] = {"TAURI_SIGNING_PRIVATE_KEY": key, "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": key_password}
    return _ctx["signing"]


# ---------------------------------------------------------------- banned licences

BANNED_LINE = re.compile(r"^BANNED_IDS: frozenset = frozenset\((?:\{[^}]*\})?\)$", re.M)


def with_banned_ids(keys_text: str, ids) -> str:
    """keys.py text with BANNED_IDS set to ids."""
    body = "{\n" + "".join(f'    "{i}",\n' for i in sorted(ids)) + "}" if ids else ""
    new, n = BANNED_LINE.subn(f"BANNED_IDS: frozenset = frozenset({body})", keys_text)
    if n != 1:
        fail("BANNED_IDS line not found in app/licensing/keys.py")
    return new


def built_in_banned_ids(keys_text: str) -> set[str]:
    """The ids keys.py carries now."""
    for node in ast.parse(keys_text).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "BANNED_IDS":
            args = node.value.args
            return set(ast.literal_eval(args[0])) if args else set()
    fail("BANNED_IDS not found in app/licensing/keys.py")


def issuer_banned_ids() -> set[str]:
    """The issuing tool's banned list (own bans + last service sync) on this PC."""
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
    log(f"  issuing tool: {len(ids)} banned (service: {snap.get('url') or 'not synced'})")
    return set(ids)


_SERVICE_FETCH = """
import json, sys, urllib.request
import config
from app.licensing import revoked
url = config.LICENCE_SERVICE_URL.rstrip("/") + "/v1/revoked?product=kopialens"
req = urllib.request.Request(url, headers={"User-Agent": "KopiaLens-release"})
token = json.loads(urllib.request.urlopen(req, timeout=20).read(262144)).get("token") or ""
payload = revoked._decode(token)
print(json.dumps({"url": url, "ids": sorted(payload["revoked"]) if payload else None}))
"""


def service_banned_ids() -> set[str] | None:
    """The licence service's signed revoked list (the one installs fetch weekly), verified with the
    keys this build trusts. None when it can't be fetched or verified."""
    with tempfile.TemporaryDirectory() as tmp:
        done = subprocess.run([sys.executable, "-c", _SERVICE_FETCH], cwd=KL_ROOT, capture_output=True, text=True,
                              timeout=90, env={**os.environ, "KOPIALENS_DATA_DIR": tmp}, stdin=subprocess.DEVNULL)
    try:
        out = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        log(f"  service list not fetched: {(done.stderr or '').strip()[-300:]}")
        return None
    if out["ids"] is None:
        log(f"  service list from {out['url']} is not signed by a key this build trusts")
        return None
    log(f"  licence service: {len(out['ids'])} revoked ({out['url']})")
    return set(out["ids"])


def step_banned() -> None:
    """Builds the banned licence ids into this release, for installs that never fetch the revoked
    list. The ids already in keys.py stay: a release never unbans anyone by accident."""
    with open(KEYS_FILE, encoding="utf-8") as fh:
        text = fh.read()
    have = built_in_banned_ids(text)
    if (os.environ.get("KOPIALENS_BANNED_FROM") or "issuer").strip().lower() == "service":
        got = service_banned_ids()
        if got is None:
            log("  " + "!" * 70)
            log(f"  WARNING: the revoked list was not fetched; this build keeps the {len(have)} id(s) in keys.py")
            log("  " + "!" * 70)
            return
    else:
        got = issuer_banned_ids()
    ids = have | got
    with open(KEYS_FILE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(with_banned_ids(text, ids))
    log(f"  {len(ids)} banned licence(s) built in ({len(ids - have)} new)")


# ---------------------------------------------------------------- kopia

def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_kopia(version: str) -> str:
    """This platform's pinned kopia release archive, downloaded once and checked against kopia's
    checksums.txt."""
    cache = os.path.join(BUILD_ROOT, "cache", f"kopia-{version}")
    os.makedirs(cache, exist_ok=True)
    name = f"kopia-{version}-{KOPIA_ASSET}"
    base = f"https://github.com/kopia/kopia/releases/download/v{version}/"
    for fname in ("checksums.txt", name):
        dest = os.path.join(cache, fname)
        if not os.path.isfile(dest):
            log(f"  downloading {base}{fname}")
            urllib.request.urlretrieve(base + fname, dest + ".part")
            os.replace(dest + ".part", dest)
    with open(os.path.join(cache, "checksums.txt"), encoding="utf-8") as fh:
        want = next((ln.split()[0] for ln in fh if ln.strip().endswith(name)), None)
    path = os.path.join(cache, name)
    if not want or sha256(path) != want:
        fail(f"kopia {name} does not match kopia's checksums.txt")
    log(f"  kopia {version} ({KOPIA_ASSET}) checksum OK")
    return path


# Everything's command-line client (voidtools/ES, MIT), shipped on Windows so users only install
# Everything itself. Pinned release + checksums.
ES_VERSION = "1.1.0.37"
ES_FILES = {
    f"ES-{ES_VERSION}.x64.zip": "7a57670d9152068d05876c58858c82fe6d3915a9df2c819f4de8801e2929d3a7",
    "LICENSE": "238dd2a1ba45db2355d3a43c139f0c0fae597ae63ee04a2bd63eda9e370937d5",
}


def stage_es(dest: str) -> None:
    """es.exe and its licence next to the server program, where core/everything looks first."""
    cache = os.path.join(BUILD_ROOT, "cache", f"es-{ES_VERSION}")
    os.makedirs(cache, exist_ok=True)
    urls = {f"ES-{ES_VERSION}.x64.zip": f"https://github.com/voidtools/ES/releases/download/{ES_VERSION}/ES-{ES_VERSION}.x64.zip",
            "LICENSE": f"https://raw.githubusercontent.com/voidtools/ES/{ES_VERSION}/LICENSE"}
    for fname, want in ES_FILES.items():
        path = os.path.join(cache, fname)
        if not os.path.isfile(path):
            log(f"  downloading {urls[fname]}")
            urllib.request.urlretrieve(urls[fname], path + ".part")
            os.replace(path + ".part", path)
        if sha256(path) != want:
            fail(f"es {fname} does not match its pinned checksum")
    with zipfile.ZipFile(os.path.join(cache, f"ES-{ES_VERSION}.x64.zip")) as zf, \
            zf.open("es.exe") as src, open(os.path.join(dest, "es.exe"), "wb") as out:
        shutil.copyfileobj(src, out)
    shutil.copy2(os.path.join(cache, "LICENSE"), os.path.join(dest, "es-LICENSE.txt"))
    log(f"  es.exe {ES_VERSION} checksum OK")


def stage_kopia(archive: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    wanted = {"kopia" + EXE, "LICENSE"}
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if os.path.basename(member) in wanted:
                    with zf.open(member) as src, open(os.path.join(dest, os.path.basename(member)), "wb") as out:
                        shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if member.isfile() and os.path.basename(member.name) in wanted:
                    with tf.extractfile(member) as src, open(os.path.join(dest, os.path.basename(member.name)), "wb") as out:
                        shutil.copyfileobj(src, out)
    for need in wanted:
        if not os.path.isfile(os.path.join(dest, need)):
            fail(f"{need} not found in {archive}")
    if not WINDOWS:
        os.chmod(os.path.join(dest, "kopia"), 0o755)


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


def check_running(base: str, version: str, data_root: str, kopia_root: str) -> None:
    """The server answers with this version, renders the page, keeps its data inside data_root and
    uses the kopia bundled under kopia_root."""
    if not wait(lambda: http(base + "/health")[0] == 200, 90):
        fail("smoke: the server did not answer /health within 90 s")
    health = json.loads(http(base + "/health")[1])
    if health.get("version") != version:
        fail(f"smoke: /health reports {health.get('version')}, expected {version}")
    status, page = http(base + "/")
    if status != 200 or b"<html" not in page[:2000].lower():
        fail(f"smoke: / returned {status}")
    status, body = http(base + "/api/app/about")
    if status != 200:
        fail(f"smoke: /api/app/about returned {status}")
    about = json.loads(body)
    if not os.path.normcase(about["data_dir"]).startswith(os.path.normcase(data_root)):
        fail(f"smoke: data folder {about['data_dir']} is outside {data_root}")
    if os.path.normcase(kopia_root) not in os.path.normcase(json.dumps(about.get("kopia"))).replace("\\\\", "\\"):
        fail(f"smoke: bundled kopia not in use: {about.get('kopia')}")
    if not about.get("compiled"):
        fail("smoke: the server is not the compiled build")
    for attempt in range(2):   # one retry: the licence service it calls may blink
        status, body = http(base + "/api/app/web-check", timeout=30)
        web = json.loads(body) if status == 200 else {}
        if web.get("ok") or web.get("cert_error"):
            break
        time.sleep(10)
    if not web.get("ok"):
        hint = ("" if web.get("cert_error") else
                " - if licence.kopialens.com is down, the build itself may be fine; check it and run smoke again")
        fail(f"smoke: the server can't make an HTTPS request ({web.get('error') or status}); "
             f"certificates: {web.get('certificates')}{hint}")
    log(f"  smoke: HTTPS certificates: {web.get('certificates')}")
    smoke_sample(base)


def smoke_sample(base: str) -> None:
    """The sample data end to end: a worker process builds a small repository and its index, then a
    search reads it - the packaged build's own subprocess, DuckDB and Parquet paths."""
    status, body = http(base + "/api/setup/sample", "POST", timeout=30)
    if status != 200:
        fail(f"smoke: sample data did not start ({status}): {body[:200]!r}")

    def ready() -> bool:
        st = json.loads(http(base + "/api/setup/sample")[1])
        if st.get("state") == "error":
            fail(f"smoke: sample data failed: {st.get('error')}")
        return st.get("state") == "done"

    started = time.time()
    if not wait(ready, 600):
        fail("smoke: sample data was not ready within 10 minutes")
    repo = json.loads(http(base + "/api/setup/sample")[1]).get("repo")
    status, body = http(f"{base}/api/search?repo={repo}&q=a", timeout=60)
    found = json.loads(body) if status == 200 else {}
    if not (found.get("results") if isinstance(found, dict) else None):
        fail(f"smoke: searching the sample data returned nothing ({status}): {body[:200]!r}")
    log(f"  smoke: sample data built and indexed in {time.time() - started:.0f} s, search OK")


def smoke_portable(zip_path: str, version: str) -> None:
    """Windows: unpacks the portable zip and runs it isolated (own ports and data, beside any running
    KopiaLens), checks the server and klens, then a clean quit through the control port."""
    root = os.path.join(BUILD_ROOT, "smoke", time.strftime("%Y%m%d-%H%M%S"))
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(root)
    app_dir = os.path.join(root, "KopiaLens")
    port, ctl = free_port(), free_port()
    env = {**os.environ, "KOPIALENS_PORT": str(port), "KOPIALENS_TRAY_PORT": str(ctl),
           "KOPIALENS_SINGLE_INSTANCE": "0", "KOPIALENS_CONTROL_API": "1"}
    for k in ("KOPIALENS_DATA_DIR", "KOPIALENS_CONFIG_DIR", "KOPIALENS_HOME", "KOPIALENS_REPOS_FILE"):
        env.pop(k, None)
    proc = subprocess.Popen([os.path.join(app_dir, "KopiaLens.exe"), "--hidden"], cwd=app_dir, env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        check_running(base, version, app_dir, app_dir)
        cli = subprocess.run([os.path.join(app_dir, "bin", "klens.exe"), "--url", base, "version"],
                             capture_output=True, text=True, timeout=60)
        if cli.returncode != 0 or json.loads(cli.stdout or "{}").get("kopialens") != version:
            fail(f"smoke: bin/klens.exe version failed ({cli.returncode}): {cli.stderr.strip()[:200]}")
        log(f"  smoke: version {version}, page OK, portable data + bundled kopia OK, klens OK, HTTPS OK")
        http(f"http://127.0.0.1:{ctl}/quit", "POST")
        if not wait(lambda: proc.poll() is not None, 30):
            fail("smoke: the desktop app did not quit within 30 s")
        if not wait(lambda: not is_up(base + "/health"), 20):
            fail("smoke: the server kept running after quit")
    finally:
        if proc.poll() is None:
            proc.kill()
    log("  smoke: clean quit OK")


def smoke_server(version: str) -> None:
    """Linux/macOS: the staged server with its bundled kopia and klens, started on its own port with
    a throwaway data folder (the desktop window needs a display, so the shell is not started)."""
    root = tempfile.mkdtemp(prefix="kopialens-smoke-")
    data = os.path.join(root, "data")
    port = free_port()
    env = {**os.environ, "KOPIALENS_PORT": str(port), "KOPIALENS_DATA_DIR": data, "KOPIALENS_HTTPS": "off",
           "KOPIALENS_BIND_HOST": "127.0.0.1", "KOPIALENS_NO_UPDATE_CHECK": "1", "KOPIALENS_NO_USAGE_STATS": "1",
           "HOME": root}
    for k in ("KOPIALENS_CONFIG_DIR", "KOPIALENS_HOME", "KOPIALENS_REPOS_FILE", "KOPIALENS_KOPIA"):
        env.pop(k, None)
    server = os.path.join(STAGE, "server", "kopialens-server")
    with open(os.path.join(root, "server.log"), "w", encoding="utf-8") as out:
        proc = subprocess.Popen([server, "--server"], cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        check_running(base, version, root, os.path.join(STAGE, "server"))
        cli = subprocess.run([os.path.join(STAGE, "bin", "klens"), "--url", base, "version"],
                             capture_output=True, text=True, timeout=60, env=env)
        if cli.returncode != 0 or json.loads(cli.stdout or "{}").get("kopialens") != version:
            fail(f"smoke: bin/klens version failed ({cli.returncode}): {cli.stderr.strip()[:200]}")
        log(f"  smoke: version {version}, page OK, data folder + bundled kopia OK, klens OK, HTTPS OK")
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
            fail("smoke: the server did not stop within 20 s of SIGTERM")
    log("  smoke: clean stop OK")


# ---------------------------------------------------------------- steps

def tag() -> str:
    return "-testing" if _ctx["testing"] else "-updatetest" if _ctx["update_endpoint"] else ""


def no_update_file() -> bool:
    """Testing and update-test builds are never offered as an update."""
    return _ctx["testing"] or bool(_ctx["update_endpoint"])


def bundles() -> list[str]:
    """This platform's packages. A beta on Linux is the AppImage only: deb and rpm would sort
    1.0.0-beta.1 after 1.0.0, so the package manager would refuse the final release as a downgrade."""
    if PLATFORM == "linux" and "-" in _ctx["version"]:
        return ["appimage"]
    return BUNDLES


def asset(kind: str) -> str:
    """A release file name: KopiaLens-<version>[-testing|-updatetest]-<platform>-<arch><kind>."""
    return f"KopiaLens-{_ctx['version']}{tag()}-{PLATFORM}-{ARCH}{kind}"


def sandbox_licensing() -> str:
    """What still points at the sandbox licence service in the shipped code ("" when nothing)."""
    found = []
    with open(os.path.join(KL_ROOT, "config.py"), encoding="utf-8") as fh:
        if re.search(r'^LICENCE_SERVICE_URL\b.*sandbox', fh.read(), re.M):
            found.append("LICENCE_SERVICE_URL in config.py")
    with open(KEYS_FILE, encoding="utf-8") as fh:
        if re.search(r'^\s*"s1"\s*:', fh.read(), re.M):
            found.append("key s1 in app/licensing/keys.py")
    return ", ".join(found)


def step_check() -> None:
    v = versions()
    version = _ctx["version"]
    if not SEMVER.match(version):
        fail(f"release version {version!r} is not like 1.2.3")
    if version != v["app"]:
        with open(os.path.join(KL_ROOT, "VERSION"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(version + "\n")
        log(f"  VERSION file set to {version} (was {v['app']})")
    stamp_ha_config(version)
    newest = appversion.tag_version()
    if os.environ.get("KOPIALENS_RELEASE_VERSION") and newest and newest != version:
        log(f"  WARNING: building {version}, but the newest kopialens/v* tag here is {newest}")
    if v["cargo"] != version or v["tauri"] != version:
        log(f"  note: Cargo.toml {v['cargo']} / tauri.conf.json {v['tauri']}; the build is stamped {version}")
    sandbox = sandbox_licensing()
    if sandbox and "-" not in version and not _ctx["testing"] and not _ctx["update_endpoint"]:
        msg = f"a final version ({version}) still uses the sandbox licence service: {sandbox}"
        if os.environ.get("GITHUB_ACTIONS"):
            fail(msg)
        log(f"  WARNING: {msg}. Fine for a local test build; the release pipeline refuses it.")
    shell_key, server_key = updater_keys()
    if not shell_key:
        fail("no updater public key in shell/src-tauri/tauri.conf.json (plugins.updater.pubkey)")
    if shell_key != server_key:
        fail("UPDATE_PUBKEY in app/core/checkin.py differs from the updater key in tauri.conf.json")
    if "desktop" in _ctx["steps"]:
        signing_env()   # asks for the key password now, not after the server build
    log(f"  {PLATFORM}-{ARCH}, kopia {v['kopia']}, updater key matches")


def step_tests() -> None:
    if os.environ.get("KOPIALENS_RELEASE_SKIP_TESTS"):
        log("  SKIPPED (KOPIALENS_RELEASE_SKIP_TESTS)")
        return
    run([sys.executable, "-m", "pytest", os.path.relpath(KL_ROOT, REPO_ROOT).replace("\\", "/"), "-q",
         "-p", "no:cacheprovider"], REPO_ROOT, "tests.log")


def step_server() -> None:
    notes = ""
    if notes_bundle.available():
        notes = notes_bundle.stage(os.path.join(BUILD_ROOT, "devnotes-stage"), testing=_ctx["testing"])
    else:
        log("WARNING: Notes widget sources not found; this build ships without Notes")
    run([BUILD_PYTHON, os.path.join(KL_ROOT, "packaging", "build_server.py")], KL_ROOT, "nuitka.log",
        {"KOPIALENS_BUILD_OUT": OUT_DIR, "KOPIALENS_BUILD_NOTES": notes})


def step_stage() -> None:
    """The compiled server + bundled kopia, the command-line launchers and (Windows) the shortcut
    icons, in the folder Tauri bundles from."""
    server = os.path.join(STAGE, "server")
    shutil.rmtree(STAGE, ignore_errors=True)
    shutil.copytree(os.path.join(OUT_DIR, "kopialens.dist"), server, symlinks=True)
    stage_kopia(fetch_kopia(versions()["kopia"]), os.path.join(server, "kopia"))
    launcher_target = os.path.join(BUILD_ROOT, "launcher-target")
    # Windows: static C runtime, since a fresh Windows has no VCRUNTIME140.dll and bin\ ships none.
    run(["cargo", "build", "--release", "--manifest-path", os.path.join(KL_ROOT, "shell", "cli-launcher", "Cargo.toml")],
        KL_ROOT, "launcher.log",
        {"CARGO_TARGET_DIR": launcher_target, **rust_env(*(["-C", "target-feature=+crt-static"] if WINDOWS else []))})
    bindir = os.path.join(STAGE, "bin")
    os.makedirs(bindir)
    for name in ("klens", "kopialens-cli"):
        dest = os.path.join(bindir, name + EXE)
        shutil.copy2(os.path.join(launcher_target, "release", "klens" + EXE), dest)
        if WINDOWS:
            with open(dest, "rb") as fh:
                needs = sorted(set(re.findall(rb"(?i)(?:vcruntime|msvcp)\d+[a-z0-9_]*\.dll", fh.read())))
            if needs:
                fail(f"bin/{name}.exe needs {b', '.join(needs).decode()}, which a fresh Windows doesn't have")
    # The disk scanner sits next to the server program, where core/disklookup looks for it.
    scanner_target = os.path.join(BUILD_ROOT, "scanner-target")
    run(["cargo", "build", "--release", "--manifest-path", os.path.join(KL_ROOT, "shell", "disk-scanner", "Cargo.toml")],
        KL_ROOT, "scanner.log",
        {"CARGO_TARGET_DIR": scanner_target, **rust_env(*(["-C", "target-feature=+crt-static"] if WINDOWS else []))})
    scanner = os.path.join(server, "kopialens-scan" + EXE)
    shutil.copy2(os.path.join(scanner_target, "release", "kopialens-scan" + EXE), scanner)
    probe = subprocess.run([scanner, "--version"], capture_output=True, text=True, timeout=30)
    if probe.returncode != 0:
        fail(f"kopialens-scan --version failed ({probe.returncode}): {probe.stderr.strip()[:200]}")
    if WINDOWS:
        stage_es(server)
        # Logo icons for desktop shortcuts (Properties > Change Icon); built by build_logos.py.
        shutil.copytree(os.path.join(SHELL, "icons", "shortcuts"), os.path.join(STAGE, "icons"))
    notices = [sys.executable, os.path.join(KL_ROOT, "packaging", "notices.py"),
               os.path.join(server, "THIRD-PARTY-NOTICES.txt"), "--python", BUILD_PYTHON,
               "--kopia-license", os.path.join(server, "kopia", "LICENSE"),
               "--parts", os.path.join(_ctx["dest"], f"{NOTICES_PART}{PLATFORM}-{ARCH}.json")]
    if WINDOWS:
        notices += ["--es-license", os.path.join(server, "es-LICENSE.txt")]
    for crate in (SHELL, os.path.join(KL_ROOT, "shell", "cli-launcher"), os.path.join(KL_ROOT, "shell", "disk-scanner")):
        notices += ["--cargo", os.path.join(crate, "Cargo.toml")]
    run(notices, KL_ROOT, "notices.log")
    log(f"  staged {server}")


def _bundle_files(pattern: str) -> list[str]:
    return sorted(glob.glob(os.path.join(SHELL_TARGET, "release", "bundle", pattern)))


def _one(pattern: str) -> str:
    found = [p for p in _bundle_files(pattern) if not p.endswith(".sig")]
    if len(found) != 1:
        fail(f"Tauri output: expected one {pattern}, found {found}")
    return found[0]


def step_desktop() -> None:
    """The desktop app and this platform's packages (Tauri), stamped with the release version and
    identity, plus the signed update piece for this platform (update-<target>.json)."""
    version, dest = _ctx["version"], _ctx["dest"]
    resources = {"release-stage/server/": "server/", "release-stage/bin/": "bin/"}
    if WINDOWS:
        resources["release-stage/icons/"] = "icons/"
    release_conf = os.path.join(STAGE, "tauri.release.json")
    conf = {**RELEASE_IDENTITY, "version": version,
            "bundle": {"active": True, "targets": bundles(), "createUpdaterArtifacts": True, "resources": resources}}
    if _ctx["update_endpoint"]:
        # Update-test build: asks only this address (packaging/update_test.py), plain http allowed.
        conf["plugins"] = {"updater": {"endpoints": [_ctx["update_endpoint"]],
                                       "dangerousInsecureTransportProtocol": _ctx["update_endpoint"].startswith("http:")}}
    with open(release_conf, "w", encoding="utf-8") as fh:
        json.dump(conf, fh)
    shutil.rmtree(os.path.join(SHELL_TARGET, "release", "bundle"), ignore_errors=True)
    run(["cargo", "tauri", "build", "--config", release_conf, "--bundles", ",".join(bundles())], SHELL, "tauri.log",
        {"CARGO_TARGET_DIR": SHELL_TARGET, **rust_env(), **signing_env()})
    exe = os.path.join(SHELL_TARGET, "release", "KopiaLens" + EXE)
    if not os.path.isfile(exe):
        fail(f"Tauri output missing: {exe}")
    if WINDOWS:
        update = _one(f"nsis/*_{version}_*-setup.exe")
        shipped = {update: asset("-setup.exe")}
    elif PLATFORM == "linux":
        update = _one("appimage/*.AppImage")
        shipped = {update: asset(".AppImage")}
        if "deb" in bundles():
            shipped.update({_one("deb/*.deb"): asset(".deb"), _one("rpm/*.rpm"): asset(".rpm")})
    else:
        update = _one("macos/*.app.tar.gz")
        shipped = {update: asset(".app.tar.gz"), _one("dmg/*.dmg"): asset(".dmg")}
    for src, name in shipped.items():
        shutil.copy2(src, os.path.join(dest, name))
        log(f"  {name}  {os.path.getsize(src) / 1e6:.0f} MB")
    if no_update_file():
        return
    sig = update + ".sig"
    if not os.path.isfile(sig):
        fail(f"no updater signature next to {update}")
    with open(sig, encoding="utf-8") as fh:
        signature = fh.read().strip()
    piece = {UPDATER_TARGET: {"signature": signature,
                              "url": f"{RELEASE_REPO_URL}/releases/download/v{version}/{shipped[update]}"}}
    with open(os.path.join(dest, f"update-{UPDATER_TARGET}.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(piece, fh, indent=2)


def step_update_file() -> None:
    """latest.json for the in-app updater, attached to the GitHub release (the check-in Worker passes
    it along): every platform's update-<target>.json in the release folder, merged."""
    if no_update_file():
        log("  skipped: testing and update-test builds write no update file")
        return
    dest = _ctx["dest"]
    platforms = {}
    for path in sorted(glob.glob(os.path.join(dest, "update-*.json"))):
        with open(path, encoding="utf-8") as fh:
            platforms.update(json.load(fh))
    if not platforms:
        fail(f"no update-*.json in {dest}: run the desktop step first")
    doc = {"version": _ctx["version"], "notes": "",
           "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "platforms": platforms}
    with open(os.path.join(dest, "latest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2)
    log(f"  latest.json: {', '.join(sorted(platforms))}")


def step_portable() -> None:
    if not WINDOWS:
        log("  skipped: the portable zip is Windows-only")
        return
    exe = os.path.join(SHELL_TARGET, "release", "KopiaLens.exe")
    server, bindir = os.path.join(STAGE, "server"), os.path.join(STAGE, "bin")
    zip_path = os.path.join(_ctx["dest"], asset("-portable.zip"))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(exe, "KopiaLens/KopiaLens.exe")
        zf.writestr(f"KopiaLens/{PORTABLE_MARKER}",
                    "This file makes KopiaLens keep its settings and index in the data folder next to it.\r\n"
                    "Without it, KopiaLens uses its normal per-user folder.\r\n"
                    "To keep them somewhere else, create KopiaLens-data.txt next to this file with a line like: data=D:\\KopiaLens data\r\n"
                    "To update, close KopiaLens, then extract the new zip over this folder and replace all files.\r\n")
        for dirpath, _dirs, files in os.walk(server):
            for f in files:
                full = os.path.join(dirpath, f)
                zf.write(full, "KopiaLens/server/" + os.path.relpath(full, server).replace("\\", "/"))
        for f in os.listdir(bindir):
            zf.write(os.path.join(bindir, f), "KopiaLens/bin/" + f)
        for f in os.listdir(os.path.join(STAGE, "icons")):
            zf.write(os.path.join(STAGE, "icons", f), "KopiaLens/icons/" + f)
    log(f"  {os.path.basename(zip_path)}  {os.path.getsize(zip_path) / 1e6:.0f} MB")


def step_cli() -> None:
    """The command line on its own, for any machine with Python 3 (standard library only)."""
    with open(os.path.join(KL_ROOT, "klens.py"), encoding="utf-8") as fh:
        cli_src = fh.read()
    if '__version__ = "dev"' not in cli_src:
        fail("klens.py: its version line changed; update the stamp in release.py")
    with open(os.path.join(_ctx["dest"], "klens.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(cli_src.replace('__version__ = "dev"', f'__version__ = "{_ctx["version"]}"', 1))


def step_smoke() -> None:
    if WINDOWS:
        smoke_portable(os.path.join(_ctx["dest"], asset("-portable.zip")), _ctx["version"])
    else:
        smoke_server(_ctx["version"])


# Text that must never ship: the private repo, this PC's folders, the developer's accounts. Matched
# case-insensitively in UTF-8 and UTF-16 (Windows resources).
LEAK_TEXT = ["personal-stuff", "g:\\github", "h:\\claude", "j:\\excluded", "kopialens-experiments",
             "\\users\\anshul", "/users/anshul", "/home/anshul", "anshulaj999"]


def leak_hits(paths: list[str]) -> dict[str, list[str]]:
    """{file: [text found]} for every file under paths (folders are walked)."""
    needles = [(t, t.encode()) for t in LEAK_TEXT] + [(t, t.encode("utf-16-le")) for t in LEAK_TEXT]
    files = []
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
        for dirpath, _dirs, names in os.walk(p):
            files += [os.path.join(dirpath, n) for n in names]
    hits: dict[str, list[str]] = {}
    for path in files:
        with open(path, "rb") as fh:
            data = fh.read().lower()
        found = sorted({t for t, needle in needles if needle in data})
        if found:
            hits[path] = found
    return hits


def step_leak_scan() -> None:
    """Warns loudly when a built file carries private text; it never stops the build."""
    hits = leak_hits([STAGE, os.path.join(SHELL_TARGET, "release", "KopiaLens" + EXE)])
    if not hits:
        log("  no private text in the built files")
        return
    _ctx["leaks"] = hits
    log("  " + "!" * 70)
    log(f"  WARNING: private text in {len(hits)} built file(s). Do not publish until it is removed:")
    for path, found in sorted(hits.items()):
        log(f"    {os.path.relpath(path, SHELL)}: {', '.join(found)}")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::private text in {os.path.basename(path)}: {', '.join(found)}", flush=True)
    log("  " + "!" * 70)


def step_installer_test() -> None:
    if not WINDOWS:
        log("  skipped: the installer test is Windows-only")
        return
    if os.environ.get("KOPIALENS_RELEASE_SKIP_INSTALLER_TEST") == "1":
        log("  " + "!" * 70)
        log("  installer test SKIPPED (KOPIALENS_RELEASE_SKIP_INSTALLER_TEST=1): this installer is untested")
        log("  " + "!" * 70)
        _ctx["installer_untested"] = True
        return
    if "--installer-test" not in sys.argv[1:] and not installer_test.needed():
        log(f"  skipped: the installer is unchanged since it passed on {installer_test.last_pass()['date']}"
            " (--installer-test runs it anyway)")
        return
    outcome = installer_test.run(os.path.join(_ctx["dest"], asset("-setup.exe")), log)
    if outcome == "failed":
        fail("the installer failed its Sandbox test - see the lines above")
    if outcome != "passed":
        _ctx["installer_untested"] = True
        log("  " + "!" * 70)
        log("  WARNING: the installer was NOT tested. Run python packaging\\installer_test.py before publishing.")
        log("  " + "!" * 70)


_NOT_SHIPPED = re.compile(r"(\.sig|\.log|^update-.*\.json|^SHA256SUMS\.txt)$")


NOTICES_PART = "THIRD-PARTY-NOTICES.part-"


def merge_notices(dest: str) -> None:
    """One THIRD-PARTY-NOTICES.txt for the release, joined from every platform build's parts."""
    parts = sorted(glob.glob(os.path.join(dest, NOTICES_PART + "*.json")))
    if not parts:
        log("  THIRD-PARTY-NOTICES.txt: no platform parts in the folder, left as it is")
        return
    run([sys.executable, os.path.join(KL_ROOT, "packaging", "notices.py"), "merge",
         os.path.join(dest, "THIRD-PARTY-NOTICES.txt"), *parts], KL_ROOT, "notices.log")
    for p in parts:
        os.remove(p)
    log(f"  THIRD-PARTY-NOTICES.txt: merged from {len(parts)} platform(s)")


def step_checksums() -> None:
    """SHA256SUMS.txt over every release file in the folder (all platforms, when merged)."""
    dest = _ctx["dest"]
    merge_notices(dest)
    names = sorted(n for n in os.listdir(dest) if os.path.isfile(os.path.join(dest, n)) and not _NOT_SHIPPED.search(n))
    with open(os.path.join(dest, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as fh:
        for name in names:
            fh.write(f"{sha256(os.path.join(dest, name))}  {name}\n")
    log(f"  SHA256SUMS.txt: {len(names)} file(s)")


def step_cleanup() -> None:
    shutil.rmtree(STAGE, ignore_errors=True)  # ~200 MB copy inside the repo folder, rebuilt each run


STEPS = {"check": step_check, "banned": step_banned, "tests": step_tests, "server": step_server,
         "stage": step_stage, "desktop": step_desktop, "update-file": step_update_file, "portable": step_portable,
         "cli": step_cli, "smoke": step_smoke, "leak-scan": step_leak_scan, "installer-test": step_installer_test,
         "checksums": step_checksums,
         "cleanup": step_cleanup}


# ---------------------------------------------------------------- main

FLAGS = ("--testing", "--with-notes", "--installer-test", "--update-endpoint=")


def main() -> int:
    global _log_fh
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return 0
    bad = [a for a in sys.argv[1:] if a.startswith("-") and not any(
        a == f or (f.endswith("=") and a.startswith(f)) for f in FLAGS)]
    if bad:
        fail(f"unknown option(s) {', '.join(bad)}; options: {', '.join(FLAGS)}, --help")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    unknown = [a for a in args if a not in STEPS]
    if unknown:
        fail(f"unknown step(s) {', '.join(unknown)}; steps: {', '.join(STEPS)}")
    steps = args or DEFAULT_STEPS
    if not args and not WINDOWS:
        fail("the one-command release is Windows-only; name the steps (see the docstring)")
    endpoint = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--update-endpoint=")), "")
    testing = any(a in ("--testing", "--with-notes") for a in sys.argv[1:])
    _ctx.update(version=release_version(), testing=testing, update_endpoint=endpoint, steps=steps)
    dest = os.environ.get("KOPIALENS_RELEASE_DIR") or os.path.join(BUILD_ROOT, "release", _ctx["version"] + tag())
    _ctx["dest"] = dest
    os.makedirs(dest, exist_ok=True)
    _log_fh = open(os.path.join(dest, "release.log"), "a" if args else "w", encoding="utf-8")
    log(f"KopiaLens {_ctx['version']} release ({PLATFORM}-{ARCH}): {', '.join(steps)}")
    if _ctx["testing"]:
        log("  TESTING BUILD: Experimental and Notes start on. Never publish it; it writes no latest.json.")
    if endpoint:
        log(f"  UPDATE-TEST BUILD: updates only from {endpoint}. Never publish it; it writes no latest.json.")
    for n, name in enumerate(steps, 1):
        log(f"{n}/{len(steps)} {name}")
        STEPS[name]()
    log(f"DONE: {dest}")
    if _ctx.get("installer_untested"):
        log("  WARNING: the installer was NOT tested in Windows Sandbox (installer-test).")
    if _ctx.get("leaks"):
        log(f"  WARNING: private text in {len(_ctx['leaks'])} built file(s) (leak-scan above). Do not publish.")
    if no_update_file():
        log("  TESTING BUILD: for test copies only; do not publish.")
    elif not args:
        log(f"  publish: a GitHub release tagged v{_ctx['version']} on {RELEASE_REPO_URL} with the files in"
            " SHA256SUMS.txt, SHA256SUMS.txt itself and latest.json (installs update from its latest.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
