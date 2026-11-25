"""The revoked-licence list is trusted only when a known key signed it for this product, and a newer
list replaces an older one, never the reverse."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pyseto
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pyseto import Key

from app.licensing import licence, revoked


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    pub = priv.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return Key.new(4, "public", pem), Key.new(4, "public", pub).to_paserk()


def _token(key, ids, days_ago=0, product="kopialens", kid="t1"):
    iat = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return pyseto.encode(key, json.dumps({"product": product, "revoked": ids, "iat": iat}),
                         footer=json.dumps({"kid": kid})).decode()


@pytest.fixture
def service(monkeypatch, tmp_path):
    key, paserk = _keypair()
    monkeypatch.setattr(revoked, "PUBLIC_KEYS", {"t1": paserk})
    monkeypatch.setattr(revoked, "LIST_FILE", str(tmp_path / "revoked.json"))
    monkeypatch.setattr(revoked, "_cache", {"mtime": None, "ids": frozenset()})
    serving = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        serving["request"] = req
        return _Resp(json.dumps({"token": serving["token"]}).encode())

    monkeypatch.setattr(revoked.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(licence, "held_tokens", lambda: [])
    return key, serving


def test_a_signed_list_revokes_its_ids(service):
    key, serving = service
    serving["token"] = _token(key, ["L1", "L2"])
    assert revoked.refresh() is True
    assert revoked.banned() == {"L1", "L2"}


def test_an_older_list_never_replaces_a_newer_one(service):
    key, serving = service
    serving["token"] = _token(key, ["L1"], days_ago=0)
    revoked.refresh()
    serving["token"] = _token(key, [], days_ago=5)
    assert revoked.refresh() is False
    assert revoked.banned() == {"L1"}


@pytest.mark.parametrize("bad", ["unknown key", "other product"])
def test_an_untrusted_list_is_ignored(service, bad):
    key, serving = service
    serving["token"] = _token(key, ["L1"])
    revoked.refresh()
    other_key, _ = _keypair()
    serving["token"] = (_token(other_key, ["L1", "L9"]) if bad == "unknown key"
                        else _token(key, ["L9"], product="someapp"))
    assert revoked.refresh() is False
    assert revoked.banned() == {"L1"}


def test_the_fetch_is_the_licence_check_in_only_when_licences_are_held(service, monkeypatch):
    key, serving = service
    serving["token"] = _token(key, [])
    revoked.refresh()
    assert serving["request"].get_method() == "GET" and serving["request"].data is None   # Free: nothing sent

    monkeypatch.setattr(licence, "held_tokens", lambda: ["v4.public.A", "v4.public.B"])
    monkeypatch.setattr(licence, "_install_id", lambda: "f" * 32)
    revoked.refresh()
    req = serving["request"]
    assert req.get_method() == "POST" and req.full_url.endswith("/v1/revoked")
    assert json.loads(req.data) == {"product": "kopialens", "licences": ["v4.public.A", "v4.public.B"], "install": "f" * 32}
    assert revoked.last_check() is not None
