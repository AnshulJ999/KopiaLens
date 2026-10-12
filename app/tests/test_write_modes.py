"""Write modes (): every write route and job kind is sorted into a mode, and the server-side
gate refuses what a repository's mode does not allow."""

from __future__ import annotations

from app.core import jobs, writemode
from app.settings import mutate_settings

_WRITES = ("post", "put", "patch", "delete")


def _sorted(path: str) -> bool:
    return (any(("POST", path) == k or (m, path) == k for k in writemode.ROUTES for m in ("POST",))
            or path in writemode.NO_REPO_WRITE or path.startswith(writemode.NO_REPO_WRITE_PREFIXES))


def test_every_write_route_and_job_kind_has_a_mode():
    from app import main
    paths = main.app.openapi()["paths"]   # trap 49: app.routes misses included routers
    unsorted = sorted(f"{m.upper()} {p}" for p, ops in paths.items() for m in ops
                      if m in _WRITES and not _sorted(p))
    assert not unsorted, "write routes with no write mode (sort each into core/writemode.py):\n" + "\n".join(unsorted)
    assert set(jobs.KINDS) <= set(writemode.JOBS), set(jobs.KINDS) - set(writemode.JOBS)
    stale = sorted(p for (_m, p) in writemode.ROUTES if p not in paths)
    assert not stale, f"ROUTES names routes that no longer exist: {stale}"


def _mode(value):
    mutate_settings(lambda s: s.setdefault("kopialens", {}).__setitem__("write_mode", value))


def test_the_gate_refuses_what_the_mode_does_not_allow():
    from starlette.testclient import TestClient

    from app import main
    client = TestClient(main.app, base_url="http://127.0.0.1")
    body = {"kind": "snapshot_delete", "repo": "1", "params": {"snapshot_ids": ["x"]}}
    try:
        _mode("non_destructive")
        refused = client.post("/api/jobs/start", json=body)
        assert refused.status_code == 403 and "Full Access" in refused.json()["detail"]
        # A dry run is a preview: allowed in every mode.
        dry = client.post("/api/jobs/start", json={**body, "params": {**body["params"], "dry": True}})
        assert dry.status_code != 403
        _mode("read_only")
        assert client.post("/api/jobs/start", json={**body, "kind": "restore"}).status_code == 403
        _mode("full")
        assert client.post("/api/jobs/start", json=body).status_code != 403
    finally:
        _mode(None)


def test_raising_to_full_access_needs_the_password(monkeypatch):
    from app.core import users
    monkeypatch.setattr(users, "accounts", lambda: [{"username": "admin", "password_hash": "x"}])
    monkeypatch.setattr(writemode, "_password_ok", lambda pw: pw == "right")
    try:
        writemode.set_mode(None, "read_only")            # stricter: free
        try:
            writemode.set_mode(None, "full", "wrong")
            raise AssertionError("raised to Full Access without the password")
        except writemode.PasswordNeeded:
            pass
        assert writemode.global_mode() == "read_only"
        writemode.set_mode(None, "full", "right")
        assert writemode.global_mode() == "full"
    finally:
        _mode(None)
