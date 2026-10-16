"""Serving under a prefix (Home Assistant ingress, a reverse proxy sub-path, core/basepath.py).

The guard fails on any app URL handed to the browser without the prefix: in the JS (``appUrl``),
the page templates (``{{ base }}``), the CSS (relative paths) and the server's own redirects and
sign-in pages (``basepath.url`` / ``__BASE__``). The ingress tests pin who is trusted: only Home
Assistant's proxy address, only when the add-on turned ingress on."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from app import main as m
from app.core import auth, basepath

KL = Path(__file__).resolve().parents[2]
FRONT = KL / "frontend"

_APP_PATH = r"""[`'"]/(?![/{])"""          # a quoted "/..." that is not "//host" or a template
JS_SINKS = [
    re.compile(r"(?:window\.open|location\.(?:assign|replace)|navigator\.sendBeacon|new\s+(?:EventSource|WebSocket|Worker))\(\s*" + _APP_PATH),
    re.compile(r"\.(?:href|src|action)\s*=\s*" + _APP_PATH),
    re.compile(r"\b(?:src|href|action)=\\?" + _APP_PATH),
    re.compile(r"location\.origin\s*\+\s*" + _APP_PATH + r"|\$\{location\.origin\}/"),
    re.compile(r"ajaxURL\s*:\s*" + _APP_PATH),
]
HTML_SINK = re.compile(r"""(?:^|\s)(?::|x-bind:)?(?:src|href|action|poster)="'?/(?!/)""")
CSS_SINK = re.compile(r"""url\(\s*["']?/(?!/)""")
PY_SINKS = [
    re.compile(r"""RedirectResponse\(\s*f?["']/"""),
    re.compile(r"""\b(?:src|href|action)=\\?["']/(?!/)"""),
]


def _hits(paths, patterns) -> list[str]:
    out = []
    for path in paths:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(p.search(line) for p in patterns):
                out.append(f"{path.relative_to(KL)}:{n}: {line.strip()[:120]}")
    return out


def _js():
    return [p for p in (FRONT / "js").rglob("*.js") if "vendor" not in p.parts]


def test_every_browser_url_carries_the_prefix():
    hits = _hits(_js(), JS_SINKS)
    hits += _hits([FRONT / "index.html", *(FRONT / "partials").rglob("*.html")], [HTML_SINK])
    hits += _hits([p for p in (FRONT / "css").glob("*.css") if p.name != "tailwind.css"], [CSS_SINK])
    # devnotes: the testing-build Notes widget (Widgets/, never in a release) does not support a prefix.
    hits += _hits([p for p in (KL / "app").rglob("*.py") if "tests" not in p.parts and p.name != "devnotes.py"],
                  PY_SINKS)
    assert not hits, "app URLs without the prefix (use appUrl / {{ base }} / basepath.url):\n" + "\n".join(hits)


def test_requests_go_through_get_json_only():
    calls = [(p.name, line.strip()) for p in _js()
             for line in p.read_text(encoding="utf-8").splitlines() if re.search(r"\bfetch\(", line)]
    assert len(calls) == 1 and calls[0][0] == "util.js" and "fetch(appUrl(url)" in calls[0][1], calls


# ---- Home Assistant ingress ----------------------------------------------------------------------

HA_PATH = "/api/hassio_ingress/Abc-123_x"


@pytest.fixture
def signin_on(monkeypatch):
    monkeypatch.setattr(auth, "enabled", lambda: True)
    monkeypatch.setattr(auth, "_STARTED", time.time())
    monkeypatch.delenv("KOPIALENS_BASE_PATH", raising=False)


def _client(address: str):
    from starlette.testclient import TestClient
    return TestClient(m.app, base_url="http://127.0.0.1", client=(address, 5000))


def test_ingress_trusts_only_home_assistants_proxy(signin_on, monkeypatch):
    monkeypatch.setenv("KOPIALENS_HA_INGRESS", "1")
    head = {basepath.INGRESS_HEADER: HA_PATH}
    assert _client(basepath.HA_PROXY).get("/api/index/pause", headers=head).status_code == 200
    # HA forwards the browser's Host, so a hostname must not be refused (421).
    named = {**head, "host": "homeassistant.local:8123"}
    assert _client(basepath.HA_PROXY).get("/api/index/pause", headers=named).status_code == 200
    assert _client("172.30.33.5").get("/api/index/pause", headers=head).status_code == 401
    assert _client(basepath.HA_PROXY).get("/api/index/pause").status_code == 401   # no ingress header


def test_ingress_is_off_unless_the_add_on_turns_it_on(signin_on, monkeypatch):
    monkeypatch.delenv("KOPIALENS_HA_INGRESS", raising=False)
    r = _client(basepath.HA_PROXY).get("/api/index/pause", headers={basepath.INGRESS_HEADER: HA_PATH})
    assert r.status_code == 401


def test_the_page_carries_the_ingress_prefix(monkeypatch):
    monkeypatch.setenv("KOPIALENS_HA_INGRESS", "1")
    monkeypatch.setattr(auth, "_STARTED", time.time())
    monkeypatch.delenv("KOPIALENS_BASE_PATH", raising=False)
    page = _client(basepath.HA_PROXY).get("/", headers={basepath.INGRESS_HEADER: HA_PATH}).text
    assert f'window.KL_BASE = "{HA_PATH}"' in page
    assert f'src="{HA_PATH}/static/js/util.js' in page
    assert '"/static/' not in page
    other = _client("192.168.1.20").get("/", headers={basepath.INGRESS_HEADER: HA_PATH}).text
    assert 'window.KL_BASE = ""' in other and 'src="/static/js/util.js' in other


def test_a_proxy_prefix_reaches_redirects_and_the_sign_in_page(signin_on, monkeypatch):
    monkeypatch.setenv("KOPIALENS_BASE_PATH", "/kopialens/")
    login = _client("192.168.1.20").get("/")
    assert login.status_code == 401
    assert 'action="/kopialens/api/auth/login"' in login.text and 'href="/kopialens/login/recover"' in login.text
    r = _client("127.0.0.1").get("/shell/open", params={"code": "x", "next": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/kopialens/"


@pytest.mark.parametrize("raw,want", [("", ""), ("/", ""), ("/a/b/", "/a/b"), ("/x y", ""),
                                      ('/"><script>', ""), ("relative", ""), (HA_PATH, HA_PATH)])
def test_only_plain_paths_become_a_prefix(raw, want):
    assert basepath.normalize(raw) == want
