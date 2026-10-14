"""API tokens: every write route is sorted for tokens, and a token gets exactly its limits through
the real app (sign-in, the write gate, window-only mode)."""

from __future__ import annotations

import time

import pytest

from app.core import auth, netaccess, tokens, users


def test_every_write_route_is_sorted_for_tokens():
    from app import main
    paths = main.app.openapi()["paths"]   # trap 49: app.routes misses included routers
    missing = [f"{m.upper()} {p}" for p, ops in paths.items() for m in ops
               if m.upper() in ("POST", "PUT", "PATCH", "DELETE") and f"{m.upper()} {p}" not in tokens._WRITES]
    assert not missing, f"sort these routes in core/tokens._WRITES: {missing}"


@pytest.fixture
def client(monkeypatch):
    from starlette.testclient import TestClient

    from app import main as m
    monkeypatch.setattr(auth, "_STARTED", time.time())
    monkeypatch.setitem(netaccess.STARTED, "mode", "computer")
    return TestClient(m.app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000))


def _token(mode: str, days: int = 90) -> dict:
    secret, _rec = tokens.create(f"{mode}-{time.time_ns() % 10**9}", mode, days)
    return {"Authorization": f"Bearer {secret}"}


def _refused(resp, needs: str) -> bool:
    return resp.status_code == 403 and resp.headers.get("x-kopialens-needs") == needs


def test_a_token_reads_and_gets_its_write_mode(client):
    ro, nd = _token("read_only"), _token("non_destructive")
    assert client.get("/api/write-mode", headers=ro).status_code == 200
    # An app-side write: Read-only is refused at the gate, Non-destructive reaches the route (400:
    # no such repository).
    assert _refused(client.patch("/api/setup/repos/zz", json={"label": "x"}, headers=ro), "mode")
    assert client.patch("/api/setup/repos/zz", json={"label": "x"}, headers=nd).status_code == 400
    # Approval never lifts a token above its permission: a delete needs a Full Access token.
    delete = {"kind": "remove_files", "repo": "1", "params": {}}
    assert _refused(client.post("/api/jobs/start", json=delete, headers=nd), "mode")
    assert _refused(client.post("/api/settings", json={"kopialens": {"session_days": 7}}, headers=ro), "mode")


@pytest.mark.parametrize("method,path,kw,needs", [
    ("post", "/api/actions/snapshot-delete", {"json": {"repo": "1", "snapshot_ids": ["k1"]}}, "approval"),
    ("post", "/api/jobs/start", {"json": {"kind": "remove_files", "repo": "1", "params": {}}}, "approval"),
    ("post", "/api/settings", {"json": {"kopialens": {"auth_enabled": False}}}, "approval"),
    ("post", "/api/settings", {"json": {"kopialens": {"allowed_hosts": ["evil.example"]}}}, "approval"),
    ("post", "/api/write-mode", {"json": {"repo": None, "mode": "full"}}, "approval"),
    ("delete", "/api/licence", {}, "approval"),
    ("post", "/api/tokens", {"json": {"name": "more", "mode": "full"}}, "never"),
    ("patch", "/api/tokens/abc", {"json": {"mode": "full"}}, "never"),
    ("get", "/api/tokens", {}, "never"),
    ("get", "/api/approvals", {}, "never"),
    ("post", "/api/approvals/abc/approve", {"json": {}}, "never"),
    ("get", "/api/config-backups/export?groups=settings,accounts", {}, "never"),
    ("get", "/api/config-backups/export?groups=settings,keys", {}, "never"),
    ("post", "/api/account/password", {"json": {}}, "never"),
    ("post", "/api/encryption/recovery-key", {"json": {}}, "never"),
])
def test_a_full_access_token_cannot_do_the_unsafe_side(client, method, path, kw, needs, monkeypatch):
    monkeypatch.setattr("app.core.writemode.global_mode", lambda: "read_only")
    resp = getattr(client, method)(path, headers=_token("full"), **kw)
    assert _refused(resp, needs), (resp.status_code, resp.headers.get("x-kopialens-needs"), resp.text[:200])


def test_an_approval_runs_once_for_that_exact_request_and_only_a_person_decides(client, monkeypatch):
    from app.core import approvals
    from app.api import dashboard
    monkeypatch.setattr(dashboard, "mutate_settings", lambda fn: {})
    tok = _token("non_destructive")
    body = {"kopialens": {"session_days": 7}}           # a person-only setting for a token
    first = client.post("/api/settings", json=body, headers=tok)
    aid = first.headers.get(approvals.HEADER)
    assert _refused(first, "approval") and aid
    assert client.get(f"/api/approvals/{aid}", headers=tok).json()["status"] == "waiting"
    assert client.get(f"/api/approvals/{aid}", headers=_token("read_only")).status_code == 404
    early = client.post("/api/settings", json=body, headers={**tok, approvals.HEADER: aid})
    assert _refused(early, "approval")
    assert _refused(client.post(f"/api/approvals/{aid}/approve", json={}, headers=tok), "never")
    assert client.post(f"/api/approvals/{aid}/approve", json={}).status_code == 200   # a person
    changed = client.post("/api/settings", json={"kopialens": {"session_days": 1}},
                          headers={**tok, approvals.HEADER: aid})
    assert _refused(changed, "approval")
    assert client.post("/api/settings", json=body, headers={**tok, approvals.HEADER: aid}).status_code == 200
    again = client.post("/api/settings", json=body, headers={**tok, approvals.HEADER: aid})
    assert _refused(again, "approval")


def test_approving_a_password_action_asks_the_person_for_it(client, monkeypatch):
    from app.core import approvals, writemode
    monkeypatch.setattr(writemode, "global_mode", lambda: "read_only")
    monkeypatch.setattr(writemode, "mode", lambda repo=None: "read_only")
    users.create("admin", auth.hash_password("correct horse"))
    tok = _token("non_destructive")
    resp = client.post("/api/write-mode", json={"repo": None, "mode": "full"}, headers=tok)
    aid = resp.headers.get(approvals.HEADER)
    with pytest.raises(approvals.PasswordNeeded):
        approvals.decide(aid, True, "admin", None)
    approvals.decide(aid, True, "admin", "correct horse")
    ok = client.post("/api/write-mode", json={"repo": None, "mode": "full"}, headers={**tok, approvals.HEADER: aid})
    assert ok.status_code == 200, ok.text[:200]


def test_the_settings_save_refuses_settings_with_their_own_route(client, monkeypatch):
    """Sign-in, remote access, encryption and the kopia program have their own checks; the general
    save must not skip them, for a person either."""
    from app.api import dashboard
    writes = []
    monkeypatch.setattr(dashboard, "mutate_settings", lambda fn: writes.append(fn) or {})
    for key in ("auth_enabled", "allowed_hosts", "encryption_level", "kopia_path", "write_mode"):
        resp = client.post("/api/settings", json={"kopialens": {key: "x"}})
        assert resp.status_code == 400 and key in resp.json()["detail"], key
    assert not writes
    assert client.post("/api/settings", json={"kopialens": {"session_days": 7}}).status_code == 200


def test_revoked_expired_and_unknown_tokens_are_refused(client):
    secret, rec = tokens.create("gone", "read_only", 30)
    tokens.revoke(rec["id"])
    assert client.get("/api/write-mode", headers={"Authorization": f"Bearer {secret}"}).status_code == 401
    secret, rec = tokens.create("old", "read_only", 30)
    tokens._mutate(lambda d: d["tokens"][-1].__setitem__("expires", time.time() - 1))
    assert client.get("/api/write-mode", headers={"Authorization": f"Bearer {secret}"}).status_code == 401
    assert client.get("/api/write-mode", headers={"Authorization": "Bearer kl_nope"}).status_code == 401


def test_with_sign_in_on_a_token_needs_no_cookie_and_others_still_do(client):
    users.create("admin", auth.hash_password("correct horse"))
    assert auth.enabled()
    assert client.get("/api/write-mode").status_code == 401
    assert client.get("/api/write-mode", headers=_token("read_only")).status_code == 200


def test_window_only_mode_accepts_a_token_from_this_computer(client, monkeypatch):
    monkeypatch.setitem(netaccess.STARTED, "mode", "window")
    assert client.get("/api/write-mode").status_code == 403
    assert client.get("/api/write-mode", headers=_token("read_only")).status_code == 200


def test_a_strong_token_asks_for_the_password_while_sign_in_is_on(client):
    users.create("admin", auth.hash_password("correct horse"))
    from app.api import tokens as api
    with pytest.raises(Exception) as exc:
        api.create_token(api.CreateRequest(name="agent", mode="non_destructive"))
    assert getattr(exc.value, "status_code", None) == 401
    out = api.create_token(api.CreateRequest(name="agent", mode="non_destructive", password="correct horse"))
    assert out["secret"].startswith("kl_") and out["created"]["mode"] == "non_destructive"
    assert api.create_token(api.CreateRequest(name="reader"))["created"]["mode"] == "read_only"


def test_editing_a_token_asks_for_the_password_to_raise_or_extend(client):
    users.create("admin", auth.hash_password("correct horse"))
    from app.api import tokens as api
    _secret, rec = tokens.create("edit-me", "read_only", 30)
    api.edit_token(rec["id"], api.EditRequest(name="renamed", days=30))   # same length: no password
    for change in ({"mode": "restores"}, {"days": 0}, {"days": 365}):
        with pytest.raises(Exception) as exc:
            api.edit_token(rec["id"], api.EditRequest(**change))
        assert getattr(exc.value, "status_code", None) == 401, change
    api.edit_token(rec["id"], api.EditRequest(mode="restores", days=0, password="correct horse"))
    after = next(t for t in tokens.list_tokens() if t["id"] == rec["id"])
    assert (after["name"], after["mode"], after["expires"]) == ("renamed", "restores", None)
