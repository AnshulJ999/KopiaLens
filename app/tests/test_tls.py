"""Packaged macOS/Linux builds carry an OpenSSL whose CA location is a build-machine path. When it is
missing, outgoing HTTPS (check-in, updates, licence) must still find CA certificates."""
import ssl

import certifi

from app.core import tls


def _ca_count() -> int:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.set_default_verify_paths()   # what urllib's default context does on macOS and Linux
    return ctx.cert_store_stats()["x509_ca"]


def _missing_default(monkeypatch, tmp_path, bundles=()):
    monkeypatch.setattr(tls.sys, "platform", "darwin")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    gone = str(tmp_path / "Python.framework" / "etc" / "openssl")
    monkeypatch.setattr(tls.ssl, "get_default_verify_paths",
                        lambda: ssl.DefaultVerifyPaths(gone + "/cert.pem", gone + "/certs", "", "", "", ""))
    monkeypatch.setattr(tls, "SYSTEM_BUNDLES", tuple(bundles))
    monkeypatch.setattr(tls, "STATE", {"source": None, "file": None})


def test_a_missing_build_machine_location_falls_back_to_the_bundled_certificates(monkeypatch, tmp_path):
    _missing_default(monkeypatch, tmp_path)
    st = tls.setup()
    assert st["source"] == "certifi" and st["file"] == certifi.where()
    assert _ca_count() > 100


def test_the_system_bundle_wins_over_certifi_and_a_users_own_setting_is_kept(monkeypatch, tmp_path):
    system = tmp_path / "cert.pem"
    system.write_bytes(open(certifi.where(), "rb").read())
    _missing_default(monkeypatch, tmp_path, bundles=[str(tmp_path / "absent.pem"), str(system)])
    assert tls.setup()["file"] == str(system)

    _missing_default(monkeypatch, tmp_path, bundles=[str(system)])
    monkeypatch.setenv("SSL_CERT_FILE", "/my/corporate.pem")
    assert tls.setup()["source"] == "user"
    import os
    assert os.environ["SSL_CERT_FILE"] == "/my/corporate.pem"


def test_certificate_failures_are_told_apart_from_being_offline():
    import urllib.error
    cert = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer"))
    offline = urllib.error.URLError(OSError("getaddrinfo failed"))
    assert tls.is_cert_error(cert) and not tls.is_cert_error(offline)
