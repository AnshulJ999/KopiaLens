"""Who can open KopiaLens, and over what: the access mode, HTTPS and trusted reverse proxies.

Access modes (setting ``access``, env ``KOPIALENS_ACCESS``):

* ``window``    - only the desktop app's own window (loopback, plus a per-launch window cookie
                  the app obtains with a one-time code; a browser on this PC is turned away);
* ``computer``  - any browser on this computer (loopback);
* ``addresses`` - all interfaces, but only the listed addresses / networks (and this computer);
* ``network``   - anyone who can reach the port.

The default follows the launch profile: desktop = computer, server = network. An explicit
``bind_host`` (setting or ``KOPIALENS_BIND_HOST``) still wins for the listening address - the
power-user override - and the address list still applies on top of it.

HTTPS (setting ``https``, env ``KOPIALENS_HTTPS``): ``off`` | ``auto`` (a certificate KopiaLens
makes and renews itself) | ``files`` (``https_cert`` + ``https_key``). Server installs default to
``auto``. The desktop app always serves plain HTTP: its tray talks to the server over loopback
HTTP, and a reverse proxy covers HTTPS for a desktop install reached from elsewhere.

All of it is read at start: a change needs a restart.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import ssl
import threading
import time

from ..settings import load_settings, raw_kopialens_settings
from .applog import get_logger

log = get_logger("netaccess")

MODES = ("window", "computer", "addresses", "network")
HTTPS_MODES = ("off", "auto", "files")
WINDOW_COOKIE = "kopialens_window"
CERT_DAYS = 825                 # the longest validity browsers accept for a leaf certificate
RENEW_DAYS = 30                 # a certificate this close to expiry is replaced at start
_CODE_TTL = 120.0


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _kl() -> dict:
    try:
        return load_settings().get("kopialens", {}) or {}
    except Exception:  # noqa: BLE001 - an unreadable settings file falls back to the defaults
        return {}


def _raw() -> dict:
    try:
        return raw_kopialens_settings()
    except Exception:  # noqa: BLE001
        return {}


def _profile() -> str:
    from . import auth
    return auth.profile()


# ---------------------------------------------------------------------------- access mode

def mode() -> str:
    """The access mode in effect. Env beats an explicit setting, which beats the profile."""
    for raw in (_env("KOPIALENS_ACCESS"), str(_raw().get("access") or "")):
        if raw.strip().lower() in MODES:
            m = raw.strip().lower()
            if m == "window" and _profile() != "desktop":
                log.warning("access 'window' needs the desktop app; using 'computer'")
                return "computer"
            return m
    bind = _env("KOPIALENS_BIND_HOST") or str(_raw().get("bind_host") or "").strip()
    if bind and bind not in ("127.0.0.1", "localhost", "::1"):
        return "network"           # an explicit listening address already opened it up
    return "computer" if _profile() == "desktop" else "network"


def bind_for_mode(m: str) -> str:
    return "127.0.0.1" if m in ("window", "computer") else "0.0.0.0"


def _networks(values) -> list:
    out = []
    for v in values or []:
        v = str(v).strip()
        if not v:
            continue
        try:
            out.append(ipaddress.ip_network(v, strict=False))
        except ValueError:
            log.warning("ignoring %r: not an address or network", v)
    return out


def addresses() -> list:
    """Allowed addresses / networks for the ``addresses`` mode."""
    env = _env("KOPIALENS_ACCESS_ADDRESSES")
    return _networks(env.split(",") if env else _kl().get("access_addresses") or [])


def parse_addresses(values) -> tuple[list[str], list[str]]:
    """(valid normalized entries, invalid entries) - for the Settings form."""
    good, bad = [], []
    for v in values or []:
        v = str(v).strip()
        if not v:
            continue
        try:
            good.append(str(ipaddress.ip_network(v, strict=False)))
        except ValueError:
            bad.append(v)
    return good, bad


def client_allowed(host: str | None, m: str | None = None) -> bool:
    """The address gate. Only the ``addresses`` mode filters; this computer always gets in."""
    if (m or mode()) != "addresses":
        return True
    try:
        ip = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in addresses())


def trusted_proxies() -> list[str]:
    """Reverse proxies whose X-Forwarded-For / -Proto headers are believed."""
    env = _env("KOPIALENS_TRUSTED_PROXIES")
    vals = env.split(",") if env else _kl().get("trusted_proxies") or []
    good, _bad = parse_addresses(vals)
    return good


# ---------------------------------------------------------------------------- window mode

_LOCK = threading.Lock()
_CODES: dict[str, float] = {}          # one-time code -> expiry
_WINDOWS: set[str] = set()             # window cookies issued since this start (no desktop app secret)


def issue_code() -> str:
    """A single-use code the desktop app exchanges for a window cookie (``/shell/open``)."""
    code = secrets.token_urlsafe(24)
    now = time.time()
    with _LOCK:
        for c, exp in list(_CODES.items()):
            if exp < now:
                _CODES.pop(c, None)
        _CODES[code] = now + _CODE_TTL
    return code


def redeem(code: str) -> str | None:
    """Exchange a one-time code for a window cookie value. None when unknown, used or expired."""
    with _LOCK:
        exp = _CODES.pop(code or "", None)
        if exp is None or exp < time.time():
            return None
    nonce = secrets.token_urlsafe(16)
    key = _window_key()
    if key:
        return f"{nonce}.{_window_sig(key, nonce)}"
    with _LOCK:
        _WINDOWS.add(nonce)
    return nonce


def _window_key() -> bytes | None:
    """The desktop app's per-launch secret. A cookie signed with it survives a server restart and
    dies when the app itself is restarted."""
    token = _env("KOPIALENS_SHELL_TOKEN")
    return token.encode("utf-8") if token else None


def _window_sig(key: bytes, nonce: str) -> str:
    return hmac.new(key, b"window:" + nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def window_ok(cookie: str | None) -> bool:
    if not cookie:
        return False
    key = _window_key()
    if key:
        nonce, _, sig = cookie.rpartition(".")
        return bool(nonce) and hmac.compare_digest(sig, _window_sig(key, nonce))
    with _LOCK:
        return cookie in _WINDOWS


# ---------------------------------------------------------------------------- HTTPS

def https_mode() -> str:
    for raw in (_env("KOPIALENS_HTTPS"), str(_raw().get("https") or "")):
        m = raw.strip().lower()
        if m in HTTPS_MODES:
            if m != "off" and _profile() == "desktop":
                log.warning("built-in HTTPS is for server installs; the desktop app serves plain "
                            "HTTP (use a reverse proxy for HTTPS)")
                return "off"
            return m
    return "off" if _profile() == "desktop" else "auto"


def _tls_dir() -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, "tls")


def _names() -> tuple[list[str], list[str]]:
    """DNS names and IP addresses this computer answers to, for the certificate."""
    dns = {"localhost"}
    ips = {"127.0.0.1", "::1"}
    try:
        host = socket.gethostname()
        dns.add(host)
        fq = socket.getfqdn()
        if fq and fq != host:
            dns.add(fq)
        for info in socket.getaddrinfo(host, None):
            ips.add(info[4][0].split("%")[0])
    except OSError:
        pass
    return sorted(dns), sorted(ips)


def _cert_ok(cert_path: str) -> bool:
    try:
        from cryptography import x509
        with open(cert_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        left = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
        return left > datetime.timedelta(days=RENEW_DAYS)
    except Exception:  # noqa: BLE001 - unreadable = make a new one
        return False


def ensure_self_signed() -> tuple[str, str]:
    """The certificate KopiaLens makes for itself: created on first use, renewed near expiry."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = _tls_dir()
    cert_path, key_path = os.path.join(d, "kopialens.crt"), os.path.join(d, "kopialens.key")
    if os.path.isfile(cert_path) and os.path.isfile(key_path) and _cert_ok(cert_path):
        return cert_path, key_path
    os.makedirs(d, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    dns, ips = _names()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "KopiaLens"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "KopiaLens (self-signed)")])
    alt = [x509.DNSName(n) for n in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    tmp = key_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key_pem)
    os.replace(tmp, key_path)
    with open(cert_path + ".tmp", "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    os.replace(cert_path + ".tmp", cert_path)
    log.info("made a self-signed HTTPS certificate for %s (valid %d days)", ", ".join(dns + ips), CERT_DAYS)
    return cert_path, key_path


def tls_files() -> tuple[str, str] | None:
    """(certificate, key) to serve HTTPS with, or None for plain HTTP. A broken own-certificate
    setting falls back to plain HTTP loudly rather than leaving KopiaLens unreachable."""
    m = https_mode()
    if m == "off":
        return None
    if m == "auto":
        try:
            pair = ensure_self_signed()
        except Exception as exc:  # noqa: BLE001
            log.error("could not make an HTTPS certificate (%s) - serving plain HTTP", exc)
            return None
        return pair if _loads(*pair) else None
    cert = _env("KOPIALENS_HTTPS_CERT") or str(_kl().get("https_cert") or "")
    key = _env("KOPIALENS_HTTPS_KEY") or str(_kl().get("https_key") or "")
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        return (cert, key) if _loads(cert, key) else None
    log.error("HTTPS is set to use certificate files, but %s - serving plain HTTP",
              "they are not set" if not (cert and key) else "a file is missing")
    return None


def _loads(cert: str, key: str) -> bool:
    """The server would refuse to start on a pair it can't load (bad file, key not matching)."""
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
        return True
    except (ssl.SSLError, OSError, ValueError) as exc:
        log.error("can't use the HTTPS certificate %s with key %s (%s) - serving plain HTTP", cert, key, exc)
        return False


def cert_info(cert_path: str) -> dict:
    """What the Settings screen shows about the certificate in use."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        with open(cert_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        try:
            names = [str(n.value) for n in cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value]
        except x509.ExtensionNotFound:
            names = []
        return {"subject": cert.subject.rfc4514_string(), "names": names,
                "expires": cert.not_valid_after_utc.isoformat(),
                "sha256": cert.fingerprint(hashes.SHA256()).hex(":").upper()}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# What the running server actually started with (set by main.run); Settings compares against it.
STARTED: dict = {}
