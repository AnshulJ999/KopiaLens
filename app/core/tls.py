"""Certificate authorities for outgoing HTTPS (check-in, updates, the licence service).

A packaged macOS or Linux build carries an OpenSSL that looks for CA certificates where the build
machine had them (on macOS a python.org folder most Macs don't have). When that location is
missing, point OpenSSL at the system's own bundle, or the certifi bundle shipped with the app.
Windows reads the system store, and a user's own SSL_CERT_FILE / SSL_CERT_DIR is always kept.
"""
from __future__ import annotations

import os
import ssl
import sys
import time

# System bundles: macOS, Debian/Ubuntu/Arch/Gentoo, Fedora/RHEL, Fedora's extracted form, openSUSE, Alpine.
SYSTEM_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/ca-bundle.pem",
)

# What setup() decided: "default" (OpenSSL's own location works), "user" (SSL_CERT_FILE/DIR set
# before start), "system" or "certifi" (a bundle chosen here), "none" (nothing usable found).
STATE: dict = {"source": None, "file": None}


def _usable_file(path: str | None) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _usable_dir(path: str | None) -> bool:
    try:
        return bool(path) and os.path.isdir(path) and any(os.scandir(path))
    except OSError:
        return False


def _certifi() -> str | None:
    try:
        import certifi
        return certifi.where()
    except Exception:  # noqa: BLE001 - missing or broken certifi is the same as none
        return None


def setup() -> dict:
    """Choose the CA bundle once per process, before any HTTPS request. Safe to call again."""
    if STATE["source"]:
        return STATE
    if sys.platform == "win32":
        STATE.update(source="default", file=None)
    elif os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        STATE.update(source="user", file=os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"))
    else:
        paths = ssl.get_default_verify_paths()
        if _usable_file(paths.cafile) or _usable_dir(paths.capath):
            STATE.update(source="default", file=paths.cafile if _usable_file(paths.cafile) else paths.capath)
        else:
            chosen = next((p for p in SYSTEM_BUNDLES if _usable_file(p)), None)
            source = "system"
            if not chosen:
                chosen, source = _certifi(), "certifi"
            if _usable_file(chosen):
                os.environ["SSL_CERT_FILE"] = chosen   # read by OpenSSL for every new context, here and in workers
                STATE.update(source=source, file=chosen)
            else:
                STATE.update(source="none", file=None)
    return STATE


def describe() -> str:
    st = setup()
    return {"default": "the system default", "user": f"SSL_CERT_FILE/SSL_CERT_DIR ({st['file']})",
            "system": f"the system bundle {st['file']}", "certifi": f"the bundled certifi file {st['file']}",
            "none": "none found - HTTPS requests will fail"}.get(st["source"], "unknown")


_warned: set[str] = set()
LAST_ERRORS: dict[str, dict] = {}   # what -> {"at", "error", "cert"}: the latest failure, for diagnostics


def note_failure(what: str, exc: BaseException, log) -> None:
    """Record a failed outgoing request. Being offline stays quiet (debug); a certificate failure is a
    setup problem, so it is logged as a warning once per kind of request."""
    cert = is_cert_error(exc)
    LAST_ERRORS[what] = {"at": time.time(), "error": str(exc)[:300], "cert": cert}
    if cert and what not in _warned:
        _warned.add(what)
        log.warning("%s: HTTPS certificate check failed (%s); CA certificates: %s", what, exc, describe())
    else:
        log.debug("%s failed: %s", what, exc)


def is_cert_error(exc: BaseException) -> bool:
    """True for a certificate-verification failure, directly or wrapped (urllib's URLError)."""
    for _ in range(5):
        if exc is None:
            return False
        if isinstance(exc, ssl.SSLCertVerificationError) or (
                isinstance(exc, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc)):
            return True
        reason = getattr(exc, "reason", None)
        exc = reason if isinstance(reason, BaseException) else exc.__cause__
    return False
