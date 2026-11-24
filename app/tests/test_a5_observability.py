"""The three observability fixes: a 5xx logs its cause, the cold listing is single-flight,
and a request's kopia spawn count belongs to that request."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest


# ---- 5xx logs the cause ------------------------------------------------------------------------

def _http_request(path: str = "/api/tree"):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": path, "headers": [],
                    "query_string": b"", "scheme": "http",
                    "server": ("127.0.0.1", 7123), "client": ("127.0.0.1", 1234)})


def _raise_with_cause(status: int):
    from fastapi import HTTPException

    try:
        raise ValueError("the real cause")
    except ValueError as exc:
        try:
            raise HTTPException(status_code=status, detail=f"{type(exc).__name__}: {exc}") from exc
        except HTTPException as http_exc:
            return http_exc


def test_a_500_logs_the_original_traceback(caplog):
    from app import main as app_main

    with caplog.at_level("ERROR"):
        resp = asyncio.run(app_main._http_exception_handler(_http_request(), _raise_with_cause(500)))
    assert resp.status_code == 500
    assert b"the real cause" in resp.body                 # the client still gets the detail
    assert "ValueError: the real cause" in caplog.text     # and the log gets the traceback
    assert "Traceback" in caplog.text and "test_a5_observability" in caplog.text


def test_a_4xx_stays_quiet(caplog):
    from app import main as app_main

    with caplog.at_level("ERROR"):
        resp = asyncio.run(app_main._http_exception_handler(_http_request(), _raise_with_cause(401)))
    assert resp.status_code == 401 and caplog.text == ""


# ---- cold listing single-flight ------------------------------------------------------------------

def test_concurrent_cold_listings_build_once(monkeypatch):
    from app.core import sources

    builds: list[str] = []

    def slow_build(repo: str):
        builds.append(repo)
        time.sleep(0.05)                                  # long enough for the others to pile up
        return {"a@h:C:": [{"id": "x"}]}

    monkeypatch.setattr(sources, "_build_from_subprocess", slow_build)
    monkeypatch.setattr(sources, "_CACHE", {})
    monkeypatch.setattr(sources, "_COLD_LOCKS", {})
    out: list[dict] = []
    threads = [threading.Thread(target=lambda: out.append(sources.sources_index("9")))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert builds == ["9"], "one listing per cold cache, not one per caller"
    assert len(out) == 8 and all(o == {"a@h:C:": [{"id": "x"}]} for o in out)


# ---- the per-request spawn counter ---------------------------------------------------------------

def test_spawns_are_charged_to_the_current_request():
    from app.core import kopia as k

    before = k.KOPIA_CALLS
    slot = [0, 0.0]
    token = k.REQUEST_CALLS.set(slot)
    try:
        k._count_spawn(0.25)
        k._count_spawn(0.25)
    finally:
        k.REQUEST_CALLS.reset(token)
    assert slot == [2, 0.5]
    assert k.KOPIA_CALLS == before + 2          # the process total keeps its meaning

    k._count_spawn(0.1)                          # outside a request: nothing is charged to one
    assert slot == [2, 0.5]
    assert k.KOPIA_CALLS == before + 3


# ---- index query time per request ---------------------------------------------------------------

class _FakeCursor:
    def execute(self, sql):
        time.sleep(0.01)
        return self

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class _FakeGen:
    def release(self):
        pass


def test_index_queries_are_charged_to_the_current_request():
    from app.index import store

    cur = store._PinnedCursor(_FakeGen(), _FakeCursor())
    cur.execute("x")                                 # outside a request: runs, nothing charged
    slot = [0, 0.0]
    token = store.REQUEST_DB.set(slot)
    try:
        assert cur.execute("x").fetchall() == [(1,)]  # chained fetch still goes through the wrapper
    finally:
        store.REQUEST_DB.reset(token)
    assert slot[0] == 1 and slot[1] >= 0.01


@pytest.mark.parametrize("debug", [False, True])
def test_the_timing_header_follows_the_debug_log_switch(debug, monkeypatch):
    from starlette.testclient import TestClient
    from app import main as m

    monkeypatch.setattr(m, "debug_on", lambda: debug)
    resp = TestClient(m.app, base_url="http://127.0.0.1").get("/api/no-such-route")
    assert ("db;dur=" in resp.headers.get("server-timing", "")) is debug


# ---- compressed API replies ---------------------------------------------------------------------

@pytest.mark.parametrize("path, size, gzipped", [
    ("/api/snapshots", 50_000, True),
    ("/api/repos", 200, False),                     # small replies are not worth compressing
    ("/api/restore/download", 50_000, False),       # file downloads pass through untouched
    ("/static/app.js", 50_000, False),
])
def test_only_large_api_replies_are_compressed(path, size, gzipped):
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from app import main as m

    async def body(request):
        return Response("x" * size, media_type="application/json")

    inner = Starlette(routes=[Route(path, body)])
    client = TestClient(m._ApiGZip(inner))
    resp = client.get(path, headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200 and len(resp.text) == size
    assert (resp.headers.get("content-encoding") == "gzip") is gzipped


@pytest.mark.parametrize("client, gzipped", [("192.168.1.20", True), ("127.0.0.1", False), ("::1", False)])
def test_replies_to_a_browser_on_the_same_pc_are_not_compressed(client, gzipped):
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from app import main as m

    async def body(request):
        return Response("x" * 50_000, media_type="application/json")

    inner = Starlette(routes=[Route("/api/snapshots", body)])
    resp = TestClient(m._ApiGZip(inner), client=(client, 50000)).get(
        "/api/snapshots", headers={"Accept-Encoding": "gzip"})
    assert (resp.headers.get("content-encoding") == "gzip") is gzipped
