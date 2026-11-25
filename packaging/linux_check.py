"""Linux container check: a Linux AppImage's server in throwaway Fedora, Arch and Ubuntu containers.

    python packaging/linux_check.py <AppImage> [--images fedora:43,archlinux:latest,ubuntu:24.04]

Each container unpacks the AppImage (no FUSE needed) and starts its server on plain HTTP; this PC then
checks /health, the page and an HTTPS request made by the server itself (/api/app/web-check, or the
update check on builds without it). Usage stats are off, so nothing is recorded anywhere. The containers
are removed afterwards. Exit code 1 when any image fails.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

IMAGES = ("fedora:43", "archlinux:latest", "ubuntu:24.04")
SERVER = "squashfs-root/usr/lib/KopiaLens/server/kopialens-server"
# Docker's minimal Debian/Ubuntu images lack the CA certificates every real install has; a local image
# with them is built once and reused, so package-mirror speed never counts against the start-up wait.
CERTS_RUN = "RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*"
NEEDS_CERTS = ("ubuntu", "debian")


def prepared(image: str) -> str:
    if image.split(":")[0] not in NEEDS_CERTS:
        return image
    local = "kl-linuxcheck-" + image.replace(":", "-").replace("/", "-")
    if subprocess.run(["docker", "image", "inspect", local], capture_output=True).returncode:
        print(f"  preparing {local} (once): {image} with ca-certificates", flush=True)
        built = subprocess.run(["docker", "build", "-q", "-t", local, "-"], input=f"FROM {image}\n{CERTS_RUN}\n",
                               capture_output=True, text=True)
        if built.returncode:
            raise RuntimeError(f"could not prepare {local}: {built.stderr.strip()[-300:]}")
    return local


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url: str, method: str = "GET", timeout: float = 10) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                 headers={"Origin": url.split("/api")[0]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_health(base: str, timeout: float) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            status, body = http(base + "/health", timeout=2)
            if status == 200:
                return json.loads(body)
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return None


def https_check(base: str) -> tuple[bool, str]:
    status, body = http(base + "/api/app/web-check", timeout=40)
    if status == 200:
        web = json.loads(body)
        if web.get("ok"):
            return True, f"web-check OK (certificates: {web.get('certificates')})"
        return False, f"web-check failed: {web.get('error')} (cert error: {web.get('cert_error')})"
    status, body = http(base + "/api/help/check-updates", "POST", timeout=40)
    if status != 200:
        return False, f"no web-check, update check answered {status}: {body[:150]!r}"
    res = json.loads(body)
    err = (res.get("update") or {}).get("error") or (res.get("update") or {}).get("last_error")
    return bool(res.get("ok")), "update check OK" if res.get("ok") else f"update check failed: {err or res}"


def check(appimage: Path, image: str) -> tuple[bool, str]:
    name = f"kl-linuxcheck-{image.split(':')[0].replace('/', '-')}-{os.getpid()}"
    port = free_port()
    env = {"KOPIALENS_PORT": "7123", "KOPIALENS_DATA_DIR": "/tmp/data", "KOPIALENS_HTTPS": "off",
           "KOPIALENS_BIND_HOST": "0.0.0.0", "KOPIALENS_NO_USAGE_STATS": "1", "HOME": "/tmp"}
    try:
        run_image = prepared(image)
    except RuntimeError as e:
        return False, str(e)
    inside = (f"cp '/kl/{appimage.name}' /tmp/app && chmod +x /tmp/app && cd /tmp && "
              f"./app --appimage-extract >/dev/null && exec {SERVER} --server")
    cmd = ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:7123",
           "-v", f"{appimage.parent.as_posix()}:/kl:ro"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [run_image, "sh", "-c", inside]
    run = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    if run.returncode:
        return False, f"container did not start: {run.stderr.strip()[:200]}"
    base = f"http://127.0.0.1:{port}"
    try:
        health = wait_health(base, 180)
        if not health:
            logs = subprocess.run(["docker", "logs", "--tail", "15", name], capture_output=True, text=True)
            return False, "server did not answer /health within 180 s:\n    " + (logs.stdout + logs.stderr).strip().replace("\n", "\n    ")[-1500:]
        status, page = http(base + "/")
        if status != 200 or b"<html" not in page[:2000].lower():
            return False, f"version {health.get('version')}: the page returned {status}"
        ok, note = https_check(base)
        if not ok:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, encoding="utf-8", errors="replace")
            hits = [l.strip() for l in (logs.stdout + logs.stderr).splitlines()
                    if any(w in l.lower() for w in ("certificate", "ssl", "tls"))]
            note = note.split(": {")[0] + ("\n    " + "\n    ".join(hits[-3:]) if hits else "")
        return ok, f"version {health.get('version')}, page OK, {note}"
    finally:
        subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True)


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    appimage = Path(args[0]).resolve()
    images = IMAGES
    if "--images" in args:
        images = tuple(args[args.index("--images") + 1].split(","))
    if not appimage.is_file():
        print(f"no such AppImage: {appimage}")
        return 2
    failed = 0
    for image in images:
        ok, note = check(appimage, image)
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'} {image}: {note}", flush=True)
    print(f"linux check: {len(images) - failed} of {len(images)} passed ({appimage.name})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
