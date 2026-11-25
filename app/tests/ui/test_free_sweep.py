"""A Free install never receives what rebuilds a Pro delete: object ids, snapshot ids or kopia
remove/delete commands. Every read route (and the preview routes below) is called on a real,
indexed Free server and its reply scanned. A place ruled fine is listed in ALLOWED
with the reason; anything else fails here and needs a ruling first."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse

from . import _harness as h

_TOKEN = re.compile(r"[0-9A-Za-z]{16,}")
_CMD = re.compile(r"snapshot fix remove-files|--object-id", re.I)
_SNAP_DELETE = re.compile(r"snapshot delete", re.I)

# (route, key path) -> why a Free install may see it. Filled only from explicit rulings.
_SNAP = "snapshot id: Free deletes, restores and mounts whole snapshots"
ALLOWED: dict[tuple[str, str], str] = {
    ("/api/snapshots", "snapshots[].id"): _SNAP,
    ("/api/restore/plan", "items[].snapshot_id"): _SNAP,
    ("/api/restore/plan", "items[].source_id"): _SNAP,
    ("/api/search/detail", "versions[].snapshot_id"): _SNAP,
    ("/api/search/versions", "versions[].snapshot_id"): _SNAP,
    ("/api/jobs/status", "params.items[].source_id"): _SNAP,
    ("/api/jobs/list", "jobs[].params.items[].source_id"): _SNAP,
    ("/api/jobs/list", "jobs[].commands_text"): _SNAP,
    ("/api/audit", "records[].summary.source_id"): _SNAP,
}
# An allowance covers snapshot ids only: an object id or a content delete command never is.
# A Free snapshot delete's own job shows its command (allowed: Free deletes
# whole snapshots), so the job list may also carry "snapshot delete".
_ALLOWED_KINDS = {("/api/jobs/list", "jobs[].commands_text"): {"snapshot", "snapshot delete"}}

# Routes that stream files or change state although they answer GET; never called here.
_SKIP = {"/api/restore/download", "/api/search/export", "/api/logs/download", "/api/diagnostics/bundle"}


def _repo_ids(fixture_repo: dict, kopia: str) -> dict[str, str]:
    """Every snapshot id ("snapshot") and content id ("object") in the fixture repository (an object
    id is a content id, or ``I`` + one for a split file)."""
    run = lambda *a: h.kopia_run(kopia, fixture_repo["env"], fixture_repo["config"], *a)  # noqa: E731
    ids = {line.split()[0]: "object" for line in run("content", "list").splitlines() if line.strip()}
    ids.update({s["id"]: "snapshot" for s in json.loads(run("snapshot", "list", "--all", "--json"))})
    return ids


def _kinds(text: str, ids: dict[str, str]) -> set[str]:
    kinds = {"command"} if _CMD.search(text) else set()
    if _SNAP_DELETE.search(text):
        kinds.add("snapshot delete")
    for t in _TOKEN.findall(text):
        kind = ids.get(t) or ids.get(t[1:])
        if kind:
            kinds.add(kind)
    return kinds


def _scan(value, path: str, ids: dict[str, str], out: list[tuple[str, str, set]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _scan(str(k), (f"{path}." if path else "") + "<key>", ids, out)   # ids used as keys too
            _scan(v, f"{path}.{k}" if path else str(k), ids, out)
    elif isinstance(value, list):
        for v in value:
            _scan(v, path + "[]", ids, out)
    elif isinstance(value, str):
        kinds = _kinds(value, ids)
        if kinds:
            out.append((path, value[:120], kinds))


def _params(names: list[str], ctx: dict) -> dict | None:
    """Query values for a route's parameters, or None when one it needs is unknown here."""
    out = {}
    for name, required in names:
        if name in ctx:
            out[name] = ctx[name]
        elif required:
            return None
    return out


def _call(srv, method: str, path: str, body: dict | None = None):
    try:
        return srv.request(method, path, body)
    except (urllib.error.HTTPError, ValueError):   # refused, or not JSON (a file)
        return None


def test_free_replies_carry_no_delete_ingredients(app_server, fixture_repo, kopia):
    srv = app_server
    ids = _repo_ids(fixture_repo, kopia)
    assert len(ids) > 10, "could not list the fixture repository's ids"
    repo = srv.get("/api/repos")["repos"][0]["id"]
    row = srv.get(f"/api/search?repo={repo}&q=needle")["results"][0]
    sep = "\\" if "\\" in row["full_path"] else "/"
    folder = row["full_path"].rsplit(sep, 1)[0]
    snap = srv.get(f"/api/search/versions?repo={repo}&source={urllib.parse.quote(row['source'])}"
                   f"&full_path={urllib.parse.quote(row['full_path'])}")["versions"][0]["snapshot_id"]
    ctx = {"repo": repo, "source": row["source"], "full_path": row["full_path"], "path": row["full_path"],
           "prefix": folder, "folder": folder, "q": "needle", "snapshot_id": snap}

    found: list[tuple[str, str, str]] = []
    called = 0
    spec = srv.get("/openapi.json")
    for route, ops in sorted(spec["paths"].items()):
        op = ops.get("get")
        if not op or "{" in route or route in _SKIP or not route.startswith("/api/"):
            continue
        names = [(p["name"], p.get("required", False)) for p in op.get("parameters", [])
                 if p.get("in") == "query"]
        q = _params(names, ctx)
        if q is None:
            continue
        reply = _call(srv, "GET", route + ("?" + urllib.parse.urlencode(q) if q else ""))
        if reply is None:
            continue
        called += 1
        hits: list[tuple[str, str, set]] = []
        _scan(reply, "", ids, hits)
        found += [(route, k, v, kinds) for k, v, kinds in hits]

    item = {"source": row["source"], "full_path": row["full_path"]}
    version = {**item, "snapshot_id": snap, "version": True}
    # A real restore of the file version into the test folder, so job replies are scanned too.
    plan = srv.post("/api/restore/plan", {"repo": repo, "items": [version], "target_mode": "folder",
                                          "target": str(srv.data / "restore-out"), "preview": False})
    job = srv.post("/api/jobs/start", {"kind": "restore", "repo": repo, "params": {
        "items": [{"source_id": i["source_id"], "target": i["target"]} for i in plan["items"]]}})
    assert h.wait_until(lambda: srv.get(f"/api/jobs/status?job_id={job['job_id']}").get("state")
                        not in ("queued", "running"), wait=60)
    ctx["job_id"] = job["job_id"]
    prune_plan = {"repo": repo, "source": row["source"], "full_path": row["full_path"]}
    prune_token = (_call(srv, "POST", "/api/prune/plan", prune_plan) or {}).get("token")
    previews = [
        ("/api/prune/plan", prune_plan),
        *([("/api/prune/price", {"token": prune_token, "remove": [0]})] if prune_token else []),
        ("/api/policies/preview", {"repo": repo, "target": row["source"], "pattern": "*.txt"}),
        ("/api/delete/plan", {"repo": repo, "files": [item], "method": "remove"}),
        ("/api/delete/plan", {"repo": repo, "files": [item], "method": "remove", "by": "filename"}),
        ("/api/delete/plan", {"repo": repo, "files": [item], "method": "snapshots"}),
        ("/api/delete/stats", {"repo": repo, "files": [item]}),
        ("/api/actions/remove-plan", {"repo": repo, "prefix": folder, "all_versions": True}),
        ("/api/actions/remove-plan", {"repo": repo, "prefix": folder, "by": "filename"}),
        ("/api/actions/remove-detail", {"repo": repo, "prefix": folder, "all_versions": True}),
        ("/api/actions/batch-detail", {"repo": repo, "files": [item]}),
        ("/api/search/versions", {"repo": repo, "rows": [item]}),
        ("/api/search/folder-values", {"repo": repo, "folder": folder}),
        ("/api/folder/detail", {"repo": repo, "prefix": folder}),
        ("/api/restore/plan", {"repo": repo, "items": [item], "target_mode": "folder",
                               "target": str(srv.data / "restore-preview")}),
        ("/api/restore/plan", {"repo": repo, "items": [version], "target_mode": "folder",
                               "target": str(srv.data / "restore-preview")}),
    ]
    for route, body in previews:
        reply = _call(srv, "POST", route, body)
        if reply is None:
            continue
        called += 1
        hits = []
        _scan(reply, "", ids, hits)
        found += [(route, k, v, kinds) for k, v, kinds in hits]

    assert called > 40, f"only {called} routes answered - the sweep is not reaching the app"
    new = sorted({(r, k) for r, k, _v, kinds in found
                  if (r, k) not in ALLOWED or not kinds <= _ALLOWED_KINDS.get((r, k), {"snapshot"})})
    examples = {(r, k): f"{sorted(kinds)}: {v}" for r, k, v, kinds in found}
    assert not new, "Free replies carry ids or commands (get a ruling before allowing any):\n" + "\n".join(
        f"  {r}  {k}  e.g. {examples[(r, k)]}" for r, k in new)
