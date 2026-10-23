"""klens: the KopiaLens command line, for scripts and AI agents.

A thin client over the KopiaLens API (standard library only, so this one file also runs on any
machine with Python 3). JSON on stdout by default, ``--table`` for a readable table; progress and
waiting messages go to stderr.

    klens login                      save a token for this KopiaLens
    klens status                     server, repositories, backups
    klens search 1 "*.iso" --largest
    klens plan 1 --prefix "D:/Old"   preview a removal (prints a plan id)
    klens dry-run 1 <plan>           ask kopia what it would remove
    klens run 1 <plan> --yes         remove it (waits for your approval in KopiaLens)

Exit codes: 0 done, 1 failed, 2 usage, 3 refused or denied, 4 left waiting (--no-wait).
"""

from __future__ import annotations

import argparse
import getpass
import json
import ipaddress
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "dev"     # set from the app's version in builds
API_VERSION = 1         # the /health "api" number this client speaks
DEFAULT_URL = "http://127.0.0.1:7123"
APPROVAL_HEADER = "X-KopiaLens-Approval"
_CRED_SERVICE = "KopiaLens CLI"


class Failed(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------- saved login

def _config_dir() -> str:
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "KopiaLens")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "kopialens")


def _config_path() -> str:
    return os.path.join(_config_dir(), "cli.json")


def _load_config() -> dict:
    try:
        with open(_config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(data: dict) -> None:
    os.makedirs(_config_dir(), exist_ok=True)
    path = _config_path()
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    if os.name != "nt":
        os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)


def _win_cred():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    return ctypes, CREDENTIAL, adv


def _store_token(url: str, token: str | None) -> str:
    """Save (or with None, forget) the token for ``url``. Returns where it went."""
    target = f"{_CRED_SERVICE}:{url}"
    if os.name == "nt":
        ctypes, CREDENTIAL, adv = _win_cred()
        if token is None:
            adv.CredDeleteW(target, 1, 0)
            return "Windows Credential Manager"
        blob = token.encode("utf-8")
        cred = CREDENTIAL(Type=1, TargetName=target, UserName="token", Persist=2,
                          CredentialBlobSize=len(blob),
                          CredentialBlob=ctypes.cast(ctypes.create_string_buffer(blob, len(blob)),
                                                     ctypes.POINTER(ctypes.c_char)))
        if not adv.CredWriteW(ctypes.byref(cred), 0):
            raise Failed(f"Couldn't save the token in Windows Credential Manager (error {ctypes.get_last_error()}).")
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        cmd = (f'delete-generic-password -s "{_CRED_SERVICE}" -a "{url}"\n' if token is None else
               f'add-generic-password -U -s "{_CRED_SERVICE}" -a "{url}" -w "{token}"\n')
        subprocess.run(["security", "-i"], input=cmd, text=True, capture_output=True)
        return "the macOS Keychain"
    cfg = _load_config()
    tokens = cfg.setdefault("tokens", {})
    if token is None:
        tokens.pop(url, None)
    else:
        tokens[url] = token
    _save_config(cfg)
    return _config_path()


def _saved_token(url: str) -> str | None:
    if os.name == "nt":
        ctypes, CREDENTIAL, adv = _win_cred()
        ptr = ctypes.POINTER(CREDENTIAL)()
        if not adv.CredReadW(f"{_CRED_SERVICE}:{url}", 1, 0, ctypes.byref(ptr)):
            return None
        try:
            c = ptr.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
        finally:
            adv.CredFree(ptr)
    if sys.platform == "darwin":
        out = subprocess.run(["security", "find-generic-password", "-s", _CRED_SERVICE, "-a", url, "-w"],
                             capture_output=True, text=True)
        return out.stdout.strip() or None if out.returncode == 0 else None
    return (_load_config().get("tokens") or {}).get(url)


# ---------------------------------------------------------------------------- talking to KopiaLens

def _loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Client:
    def __init__(self, url: str, token: str | None, approval: str | None = None, wait: bool = True,
                 insecure: bool = False, fallback: bool = False):
        self.url = url.rstrip("/")
        self.token = token
        self.approval = approval
        self.wait = wait
        self.insecure = insecure
        self.fallback = fallback    # the default address: try HTTPS when plain HTTP doesn't answer

    def _context(self):
        # KopiaLens's own certificate on this computer can't be checked against a public authority.
        if self.url.startswith("https://") and (self.insecure or _loopback(self.url)):
            return ssl._create_unverified_context()
        return None

    def _send(self, method: str, path: str, query: dict | None, body, approval: str | None):
        q = {k: v for k, v in (query or {}).items() if v is not None and v is not False}
        url = self.url + path + ("?" + urllib.parse.urlencode(q, doseq=True) if q else "")
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if approval:
            req.add_header(APPROVAL_HEADER, approval)
        try:
            with urllib.request.urlopen(req, timeout=600, context=self._context()) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), raw
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            if self.fallback and self.url.startswith("http://"):
                self.fallback = False
                self.url = "https://" + self.url[len("http://"):]
                return self._send(method, path, query, body, approval)
            reason = getattr(exc, "reason", exc)
            raise Failed(f"Couldn't reach KopiaLens at {self.url} ({reason}). Is it running?") from exc

    def call(self, method: str, path: str, query: dict | None = None, body=None):
        status, headers, raw = self._send(method, path, query, body, self.approval)
        headers = {k.lower(): v for k, v in headers.items()}
        if status == 403 and headers.get("x-kopialens-needs") == "approval" and APPROVAL_HEADER.lower() in headers \
                and not self.approval:
            aid = headers[APPROVAL_HEADER.lower()]
            self._await_approval(aid, _detail(raw))
            status, headers, raw = self._send(method, path, query, body, aid)
            headers = {k.lower(): v for k, v in headers.items()}
        try:
            out = json.loads(raw) if raw else {}
        except ValueError:
            out = raw.decode("utf-8", "replace")     # a docs page or a download: printed as it is
        if status >= 400:
            needs = headers.get("x-kopialens-needs")
            code = 3 if status in (401, 403) else 1
            raise Failed(_detail(raw) or f"KopiaLens answered {status}.", code) if not needs else \
                Failed(f"{_detail(raw)} ({needs})", code)
        return out

    def _await_approval(self, aid: str, message: str) -> None:
        if not self.wait:
            print(json.dumps({"approval": aid, "status": "waiting", "detail": message}, indent=2))
            raise Failed(f"Left waiting for approval {aid}. Run the same command with --approval {aid} once it's approved.", 4)
        print(f"Waiting for approval in KopiaLens (request {aid}). Approve or deny it in Settings > API tokens.",
              file=sys.stderr)
        while True:
            status, _h, raw = self._send("GET", f"/api/approvals/{aid}", None, None, None)
            rec = json.loads(raw) if status == 200 and raw else {}
            state = rec.get("status")
            if state == "approved":
                print("Approved.", file=sys.stderr)
                return
            if status != 200 or state not in ("waiting",):
                raise Failed(f"The request was {state or 'dropped'}.", 3)
            time.sleep(2)


def _detail(raw: bytes) -> str:
    try:
        d = json.loads(raw).get("detail")
        return d if isinstance(d, str) else json.dumps(d)
    except (ValueError, AttributeError):
        return raw.decode("utf-8", "replace")[:300] if raw else ""


# ---------------------------------------------------------------------------- output

_LIST_KEYS = ("rows", "results", "repos", "sources", "snapshots", "jobs", "approvals", "files", "items",
              "records", "entries", "children", "lines")


def _rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in _LIST_KEYS:
            if isinstance(data.get(k), list):
                return data[k]
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return None


def _print(data, table: bool) -> None:
    if isinstance(data, str):
        print(data)
        return
    rows = _rows(data) if table else None
    if not rows or not isinstance(rows[0], dict):
        print(json.dumps(data, indent=2, default=str))
        return
    cols = [k for k, v in rows[0].items() if not isinstance(v, (dict, list))][:8]
    cells = [[_cell(r.get(c)) for c in cols] for r in rows]
    width = [min(48, max([len(c)] + [len(row[i]) for row in cells])) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(width[i]) for i, c in enumerate(cols)))
    for row in cells:
        print("  ".join(v[:width[i]].ljust(width[i]) for i, v in enumerate(row)))


def _cell(v) -> str:
    return "" if v is None else str(v)


def _wait_job(cli: Client, job: dict) -> dict:
    """Follow a job to its end, printing its output lines to stderr."""
    jid, cursor = job.get("job_id"), 0
    while True:
        st = cli.call("GET", "/api/jobs/status", {"job_id": jid, "after": cursor})
        for line in st.get("lines") or []:
            print(line, file=sys.stderr)
        cursor = st.get("cursor", cursor)
        if st.get("state") != "running":
            st.pop("lines", None)
            return st
        time.sleep(1)


# ---------------------------------------------------------------------------- commands

def _cmd(args, cli: Client):
    c = args.command
    if c == "status":
        health = cli.call("GET", "/health")
        return {"url": cli.url, "version": health.get("version"), "api": health.get("api"),
                "repos": cli.call("GET", "/api/repos"), "backups": cli.call("GET", "/api/engine/status")}
    if c == "repos":
        return cli.call("GET", "/api/repos")
    if c == "sources":
        return cli.call("GET", "/api/sources", {"repo": args.repo})
    if c == "search":
        return cli.call("GET", "/api/search", {"repo": args.repo, "q": args.query, "largest": args.largest,
                                              "source": args.source, "ext": args.ext, "limit": args.limit,
                                              "deleted_only": args.deleted, "over_gb": args.over_gb})
    if c == "file":
        return cli.call("GET", "/api/search/detail", {"repo": args.repo, "source": args.source,
                                                     "full_path": args.path})
    if c == "folder":
        return cli.call("GET", "/api/folder/summary", {"repo": args.repo, "prefix": args.prefix,
                                                      "source": args.source})
    if c == "ls":
        return cli.call("GET", "/api/folder/contents", {"repo": args.repo, "prefix": args.prefix,
                                                       "source": args.source, "limit": args.limit})
    if c == "snapshots":
        return cli.call("GET", "/api/snapshots", {"repo": args.repo, "source": args.source})
    if c == "index":
        if args.build:
            return cli.call("POST", "/api/index/build", {"repo": args.repo, "full": args.full})
        return cli.call("GET", "/api/index/status", {"repo": args.repo})
    if c == "snapshot":
        return cli.call("POST", "/api/engine/snapshot", body={"repo": args.repo, "source": args.source})
    if c == "plan":
        body = {"repo": args.repo, "query": args.query or "", "prefix": args.prefix, "source": args.source,
                "ext": args.ext, "deleted_only": args.deleted, "over_gb": args.over_gb or 0,
                "everywhere": args.everywhere, "all_versions": args.all_versions}
        plan = cli.call("POST", "/api/actions/remove-plan", body=body)
        if not plan.get("plan_token"):
            raise Failed("Removing files needs KopiaLens Pro, so there's no plan to run.", 3)
        keep = ("plan_token", "file_count", "item_count", "logical", "physical", "total_file_count",
                "sources_matched", "everywhere", "filename_extra_count", "content_extra_count", "preview")
        return {k: plan.get(k) for k in keep}
    if c in ("dry-run", "run"):
        if c == "run" and not args.yes:
            raise Failed("This removes data from your backups. Run it again with --yes.", 2)
        job = cli.call("POST", "/api/jobs/start", body={
            "kind": "remove_files", "repo": args.repo,
            "params": {"form": "plan", "plan_token": args.plan, "dry": c == "dry-run"}})
        return job if args.no_follow else _wait_job(cli, job)
    if c == "delete-snapshots":
        if not args.dry_run and not args.yes:
            raise Failed("This deletes snapshots. Run it again with --yes, or try --dry-run first.", 2)
        job = cli.call("POST", "/api/jobs/start", body={
            "kind": "snapshot_delete", "repo": args.repo,
            "params": {"snapshot_ids": args.ids, "dry": args.dry_run}})
        return job if args.no_follow else _wait_job(cli, job)
    if c == "jobs":
        return cli.call("GET", "/api/jobs/list")
    if c == "job":
        st = cli.call("GET", "/api/jobs/status", {"job_id": args.job_id})
        return _wait_job(cli, st) if args.wait else st
    if c == "logs":
        return cli.call("GET", "/api/logs/app", {"limit": args.limit, "level": args.level})
    if c == "docs":
        if args.page:
            page = args.page if args.page.endswith(".md") else args.page + ".md"
            return cli.call("GET", "/api/help/docs/file", {"path": page})
        d = cli.call("GET", "/api/help/docs")
        pages = d.get("pages") or {}
        return [{"page": slug, "title": (pages.get(slug) or {}).get("title", slug), "group": g["title"]}
                for g in d.get("groups") or [] for slug in g.get("pages") or []]
    if c == "approval":
        return cli.call("GET", f"/api/approvals/{urllib.parse.quote(args.id)}")
    if c == "api":
        body = json.loads(args.data) if args.data else None
        query = dict(q.split("=", 1) for q in args.query or [])
        return cli.call(args.method.upper(), args.path if args.path.startswith("/") else "/" + args.path,
                        query, body)
    raise Failed(f"unknown command: {c}", 2)


def _login(args, url: str) -> dict:
    token = args.token_value
    if not token:
        token = (sys.stdin.readline() if not sys.stdin.isatty() else getpass.getpass("API token: ")).strip()
    if not token.startswith("kl_"):
        raise Failed("That isn't a KopiaLens API token (they start with kl_). Create one in Settings > API tokens.", 2)
    Client(url, token).call("GET", "/api/repos")
    where = _store_token(url, token)
    cfg = _load_config()
    cfg["url"] = url
    _save_config(cfg)
    return {"ok": True, "url": url, "saved_in": where}


def _check_version(cli: Client) -> None:
    try:
        health = cli.call("GET", "/health")
    except Failed:
        return
    api = health.get("api")
    if api != API_VERSION:
        print(f"Note: this klens speaks API {API_VERSION}; KopiaLens {health.get('version')} at {cli.url} "
              f"reports {api if api is not None else 'no API version'}. Some commands may not work.",
              file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="klens", description="The KopiaLens command line.")
    p.add_argument("--url", help=f"KopiaLens address (default: saved, KOPIALENS_URL, or {DEFAULT_URL})")
    p.add_argument("--token", help="API token (default: KOPIALENS_TOKEN, or the one saved by login)")
    p.add_argument("--table", action="store_true", help="print lists as a table instead of JSON")
    p.add_argument("--approval", help="send this approved request id with the request")
    p.add_argument("--no-wait", action="store_true", help="don't wait when a request needs approval")
    p.add_argument("--insecure", action="store_true",
                   help="accept any HTTPS certificate (KopiaLens's own one on this computer is always accepted)")
    sub = p.add_subparsers(dest="command", metavar="command")

    def cmd(name, text, repo=False):
        s = sub.add_parser(name, help=text, description=text)
        if repo:
            s.add_argument("repo", help="repository number, as in KopiaLens")
        return s

    s = cmd("login", "save an API token for this KopiaLens")
    s.add_argument("token_value", nargs="?", help="the token (asked for when left out)")
    cmd("logout", "forget the saved token")
    cmd("version", "this klens and the KopiaLens it talks to")
    cmd("status", "server, repositories and backups")
    cmd("repos", "the connected repositories")
    cmd("sources", "the backed-up folders of a repository", repo=True)
    s = cmd("search", "search the files in a repository's backups", repo=True)
    s.add_argument("query", nargs="?", help="a name or pattern, for example *.iso")
    s.add_argument("--largest", action="store_true", help="largest files first")
    s.add_argument("--source", help="only this backed-up folder")
    s.add_argument("--ext", help="only these extensions, for example iso,zip")
    s.add_argument("--over-gb", type=float, help="only files over this size")
    s.add_argument("--deleted", action="store_true", help="only files no longer on disk")
    s.add_argument("--limit", type=int, default=100)
    s = cmd("file", "one file: sizes, versions, sharing and what removing it frees", repo=True)
    s.add_argument("source", help="the backed-up folder the file is in")
    s.add_argument("path", help="the file's full path")
    for name, text in (("folder", "a folder's totals"), ("ls", "a folder's contents")):
        s = cmd(name, text, repo=True)
        s.add_argument("prefix", nargs="?", help="the folder path")
        s.add_argument("--source", help="the backed-up folder")
        if name == "ls":
            s.add_argument("--limit", type=int, default=500)
    s = cmd("snapshots", "a repository's snapshots", repo=True)
    s.add_argument("--source", help="only this backed-up folder")
    s = cmd("index", "index status; --build starts an index run", repo=True)
    s.add_argument("--build", action="store_true", help="start an index run")
    s.add_argument("--full", action="store_true", help="with --build: rebuild from scratch")
    s = cmd("snapshot", "take a snapshot now (when KopiaLens runs the backups)", repo=True)
    s.add_argument("source", help="the backed-up folder")
    s = cmd("plan", "preview removing files from the backups (prints a plan id)", repo=True)
    s.add_argument("--query", help="a name or pattern")
    s.add_argument("--prefix", help="a folder path")
    s.add_argument("--source", help="only this backed-up folder")
    s.add_argument("--ext", help="only these extensions")
    s.add_argument("--over-gb", type=float, help="only files over this size")
    s.add_argument("--deleted", action="store_true", help="only files no longer on disk")
    s.add_argument("--everywhere", action="store_true", help="every backed-up folder, not only the matched ones")
    s.add_argument("--all-versions", action="store_true", help="every version, not only the current one")
    for name, text in (("dry-run", "ask kopia what a plan would remove (changes nothing)"),
                       ("run", "remove a plan's files from the backups")):
        s = cmd(name, text, repo=True)
        s.add_argument("plan", help="the plan id from klens plan")
        s.add_argument("--no-follow", action="store_true", help="start the job and return")
        if name == "run":
            s.add_argument("--yes", action="store_true", help="confirm the removal")
    s = cmd("delete-snapshots", "delete snapshots by id", repo=True)
    s.add_argument("ids", nargs="+", help="snapshot ids")
    s.add_argument("--dry-run", action="store_true", help="show what would be deleted")
    s.add_argument("--yes", action="store_true", help="confirm the deletion")
    s.add_argument("--no-follow", action="store_true", help="start the job and return")
    cmd("jobs", "recent jobs")
    s = cmd("job", "one job's status")
    s.add_argument("job_id")
    s.add_argument("--wait", action="store_true", help="follow it to the end")
    s = cmd("logs", "the KopiaLens log")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--level", help="only this level and above, for example warning")
    s = cmd("docs", "the KopiaLens docs: the list of pages, or one page")
    s.add_argument("page", nargs="?", help="a page, for example cli or api")
    s = cmd("approval", "the status of an approval request")
    s.add_argument("id")
    s = cmd("api", "call any API route, for example: klens api GET /api/repos")
    s.add_argument("method")
    s.add_argument("path")
    s.add_argument("--data", help="JSON body")
    s.add_argument("--query", action="append", help="key=value (repeatable)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    cfg = _load_config()
    url = (args.url or os.environ.get("KOPIALENS_URL") or cfg.get("url") or DEFAULT_URL).rstrip("/")
    fallback = url == DEFAULT_URL and not args.url
    try:
        if args.command == "login":
            _print(_login(args, url), False)
            return 0
        if args.command == "logout":
            _print({"ok": True, "forgotten_from": _store_token(url, None)}, False)
            return 0
        token = args.token or os.environ.get("KOPIALENS_TOKEN") or _saved_token(url)
        cli = Client(url, token, args.approval, wait=not args.no_wait, insecure=args.insecure, fallback=fallback)
        if args.command == "version":
            health = cli.call("GET", "/health")
            _print({"klens": __version__, "api": API_VERSION, "kopialens": health.get("version"),
                    "kopialens_api": health.get("api"), "url": url}, False)
            return 0
        _check_version(cli)
        out = _cmd(args, cli)
        _print(out, args.table)
        if isinstance(out, dict) and out.get("state") in ("failed", "stopped"):
            return 1
        return 0
    except Failed as exc:
        print(f"klens: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
