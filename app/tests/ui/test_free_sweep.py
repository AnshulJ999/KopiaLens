"""A Free install never receives what rebuilds a Pro delete: object ids, snapshot ids or kopia
remove/delete commands. Every read route (and the preview routes below) is called on a real,
indexed Free server and its reply scanned. A place ruled fine is listed in ALLOWED
with the reason; anything else fails here and goes to him first."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse

_ID = re.compile(r"(?<![0-9A-Za-z])[A-Za-z]?[0-9a-f]{32,}(?![0-9A-Za-z])")
_CMD = re.compile(r"snapshot fix remove-files|--object-id|snapshot delete", re.I)

# (route, key path) -> why a Free install may see it. Filled only from explicit rulings.
ALLOWED: dict[tuple[str, str], str] = {}

# Routes that stream files or change state although they answer GET; never called here.
_SKIP = {"/api/restore/download", "/api/search/export", "/api/logs/download", "/api/diagnostics/bundle"}


def _scan(value, path: str, out: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _scan(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(value, list):
        for v in value:
            _scan(v, path + "[]", out)
    elif isinstance(value, str) and (_ID.search(value) or _CMD.search(value)):
        out.append((path, value[:80]))


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


def test_free_replies_carry_no_delete_ingredients(app_server):
    srv = app_server
    repo = srv.get("/api/repos")["repos"][0]["id"]
    row = srv.get(f"/api/search?repo={repo}&q=needle")["results"][0]
    sep = "\\" if "\\" in row["full_path"] else "/"
    folder = row["full_path"].rsplit(sep, 1)[0]
    ctx = {"repo": repo, "source": row["source"], "full_path": row["full_path"], "path": row["full_path"],
           "prefix": folder, "folder": folder, "q": "needle"}

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
        hits: list[tuple[str, str]] = []
        _scan(reply, "", hits)
        found += [(route, k, v) for k, v in hits]

    item = {"source": row["source"], "full_path": row["full_path"]}
    previews = [
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
    ]
    for route, body in previews:
        reply = _call(srv, "POST", route, body)
        if reply is None:
            continue
        called += 1
        hits = []
        _scan(reply, "", hits)
        found += [(route, k, v) for k, v in hits]

    assert called > 40, f"only {called} routes answered - the sweep is not reaching the app"
    new = sorted({(r, k) for r, k, _v in found if (r, k) not in ALLOWED})
    examples = {(r, k): v for r, k, v in found}
    assert not new, "Free replies carry ids or commands (ask Anshul before allowing any):\n" + "\n".join(
        f"  {r}  {k}  e.g. {examples[(r, k)]}" for r, k in new)
