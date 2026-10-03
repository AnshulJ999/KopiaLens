"""Demo copies: a repo's index with every name already faked.

The copy is a self-contained KopiaLens data folder (``<data>/demo/repo<N>[+<M>...]-<style>/``):
one plain (unencrypted) generation per source repo as ``repo1``, ``repo2``..., the kopia replies
its screens need (recorded once from each real repo, faked), its caches, and settings that keep it
off the local disk. All repos share one name key. Real names never
exist in it, so nothing that reads it can leak one. Sizes, counts, dates and ids stay real.

Names map by name alone (see ``fakenames``). Sibling collisions are resolved globally: a real
name that collides anywhere takes its re-rolled form everywhere, so a file reads the same in
Search, the Tree and every path.
"""

from __future__ import annotations

import collections
import copy
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from config import APP_VERSION, CACHEDIR, DATA_DIR, HERE

from ..index import store
from . import demoreplies, fakenames
from .applog import get_logger

log = get_logger("demo")

DEMO_DIR = "demo"
_MAX_ROUNDS = 30
_SEG_SPLIT = r"[\\/@:]"
_TEXT_SPLIT = r"[\\/@:\r\n\t\",]"
_KEPT_COLUMNS = {"ext", "repo"}
# What a finished demo folder holds; everything else the cache build wrote is removed.
_KEEP = {"cache", "demo.json", "repos.json", "settings.json", "data_version.json"}
# A demo with Backblaze also keeps its generated bzdata, the BZ index (and kopialens_bz.sqlite*).
BZ_KEEP = {"bzdata", "bz"}
_DOT_IGNORE_KEPT = {".kopiaignore", ".gitignore", ".nobackup", ".backupignore"}
# kopia's own words in its replies (cache report, stats); never data.
_KOPIA_WORDS = {"content", "contents", "indexes", "metadata", "own-writes", "blob-list",
                "server-contents", "blobs", "packs", "kopia", "kopia.exe", "repository.config"}
# Dates and long hex ids (kopia cache folders, run dates) also appear as generated values.
# Generated text that identifies nothing: long hex ids, dates, version numbers ("1.0"), and
# shorter hex codes mixing digits and letters ("433a5c"; a plain word like "facade" stays).
_GENERATED = re.compile(r"[0-9a-f]{12,}|\d{4}-\d{2}-\d{2}|v?\d+(\.\d+)+|"
                        r"(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,}")
_DEMO_SETTINGS = {"kopialens": {"onboarded": True, "auth_enabled": False, "index_mode": "off",
                                "disk_lookup": "off", "encryption_level": "off"}}
_SWEEP_SKIP = ("pick", "open", "browse", "download", "restart", "quit", "shutdown", "mount")


def _repo_ids(repos: str | list[str]) -> list[str]:
    """Accept "1", "1,2" or ["1", "2"]; return ["1", "2"]."""
    items = repos.split(",") if isinstance(repos, str) else repos
    return [r.strip() for r in items if r.strip()]


def copy_dir(repos: str | list[str], style: str) -> str:
    return os.path.join(DATA_DIR, DEMO_DIR, f"repo{'+'.join(_repo_ids(repos))}-{style}")


def _source_parts(source: str) -> tuple[str, str, str]:
    user, rest = source.split("@", 1)
    host, path = rest.split(":", 1)
    return user, host, path


def _sep_for(path: str) -> str:
    return "/" if path.startswith("/") else "\\"


def _join(parent: str, name: str, sep: str) -> str:
    if not parent:
        return sep + name
    return (parent if parent.endswith(sep) else parent + sep) + name


def _is_root_segment(seg: str) -> bool:
    return seg == "" or bool(re.fullmatch(r"[A-Za-z]:", seg))


class _Names:
    """real name -> fake name, per kind ('d' folder, 'f' file)."""

    def __init__(self, namer: fakenames.Namer, categories: dict[str, str]):
        self.namer = namer
        self.categories = categories
        self.attempt: dict[tuple[str, str], int] = {}
        self.fake: dict[tuple[str, str], str] = {}

    def get(self, kind: str, real: str) -> str:
        key = (kind, real)
        if key not in self.fake:
            self._make(key)
        return self.fake[key]

    def _make(self, key: tuple[str, str]) -> None:
        kind, real = key
        n = self.attempt.get(key, 0)
        if _is_root_segment(real):
            self.fake[key] = real
        elif kind == "d":
            self.fake[key] = self.namer.folder(real, self.categories.get(real.lower(), "Mixed"), n)
        else:
            self.fake[key] = self.namer.file(real, n)

    def bump(self, key: tuple[str, str]) -> None:
        self.attempt[key] = self.attempt.get(key, 0) + 1
        self._make(key)

    def kept(self, kind: str, real: str) -> bool:
        """Kept by design: a technical name, or (generic style) a name of common words only."""
        if _is_root_segment(real) or real.lower() in fakenames.TECHNICAL:
            return True
        if self.namer.style != "generic":
            return False
        stem = real if kind == "d" else fakenames.split_ext(real)[0]
        return fakenames.is_generic(stem)

    def table(self) -> pa.Table:
        keys = list(self.fake)
        return pa.table({"kind": [k for k, _ in keys], "real": [r for _, r in keys],
                         "fake": [self.fake[k] for k in keys]})


def _tables(gen) -> set[str]:
    return set(gen.manifest.get("artifacts", {}))


def _categories(cur, tables: set[str]) -> dict[str, str]:
    if not {"pathx", "anc", "dir"} <= tables:
        return {}
    counts: dict[str, dict[str, int]] = {}
    for name, ext, n in cur.execute("""
            SELECT d.name, lower(coalesce(p.ext, '')), count(*) FROM pathx p
            JOIN anc a ON a.dir_id = p.dir_id JOIN dir d ON d.dir_id = a.anc_id
            GROUP BY 1, 2""").fetchall():
        counts.setdefault(name.lower(), {})[ext] = n
    return {name: fakenames.category_of(c) for name, c in counts.items()}


_SIBLINGS = {  # (tree, folder, kind, name) per table: tree 0 = paths, 1 = the Tree
    "dir": "SELECT 0, parent_id, 'd', name FROM dir WHERE parent_id IS NOT NULL",
    "pathx": "SELECT 0, dir_id, 'f', name FROM pathx",
    "tent": "SELECT 1, dir_id, CASE WHEN etype = 1 THEN 'd' ELSE 'f' END, name FROM tent",
}


def _resolve_collisions(cur, names: _Names, tables: set[str]) -> int:
    """Re-roll colliding names until every folder's children are unique (case-insensitive).

    Works on integer keys (name id, fake-name id) so a repo with millions of paths fits the
    store's memory limit; members are fetched only for the folders that actually collide."""
    parts = [sql for t, sql in _SIBLINGS.items() if t in tables]
    if not parts:
        return 0
    for kind, real in cur.execute(
            "SELECT DISTINCT kind, real FROM (" + " UNION ALL ".join(
                f"SELECT k AS kind, n AS real FROM ({p}) s(t, g, k, n)" for p in parts) + ")").fetchall():
        names.get(kind, real)
    keys = list(names.fake)
    cur.register("_demo_nid", pa.table({"kind": [k for k, _ in keys], "real": [r for _, r in keys],
                                        "nid": list(range(len(keys)))}))
    cur.execute("CREATE OR REPLACE TEMP TABLE _demo_sib AS SELECT DISTINCT s.t, s.g, n.nid FROM ("
                + " UNION ALL ".join(f"SELECT * FROM ({p}) x(t, g, k, n)" for p in parts)
                + ") s JOIN _demo_nid n ON n.kind = s.k AND n.real = s.n")
    cur.unregister("_demo_nid")
    bumped = 0
    for _ in range(_MAX_ROUNDS):
        fid: dict[str, int] = {}
        fids = [fid.setdefault(names.fake[k].lower(), len(fid)) for k in keys]
        uses = collections.Counter(fids)
        # Only a fake name that two real names share can collide anywhere.
        shared = [i for i, f in enumerate(fids) if uses[f] > 1]
        if not shared:
            return bumped
        cur.register("_demo_fid", pa.table({"nid": shared, "fid": [fids[i] for i in shared]}))
        cur.execute("""CREATE OR REPLACE TEMP TABLE _demo_j AS
                       SELECT s.t, s.g, f.fid, s.nid FROM _demo_sib s JOIN _demo_fid f USING (nid)""")
        cur.unregister("_demo_fid")
        # Sorted rows streamed in batches: a list per colliding folder does not fit the memory
        # limit on a large repo (millions of folders collide in the generic style).
        reader = cur.execute("""
            SELECT j.t, j.g, j.fid, j.nid FROM _demo_j j
            JOIN (SELECT t, g, fid FROM _demo_j GROUP BY ALL HAVING min(nid) <> max(nid)) c
              USING (t, g, fid) ORDER BY j.t, j.g, j.fid""").to_arrow_reader(200_000)
        losers: set[tuple[str, str]] = set()

        def settle(members: set[int]) -> None:
            ranked = sorted((keys[m] for m in members),
                            key=lambda k: names.namer._pick(k[0], k[1], "order", n=1 << 62))
            losers.update(ranked[1:])

        group, members = None, set()
        for batch in reader:
            cols = batch.to_pydict()
            for t, g, f, n in zip(cols["t"], cols["g"], cols["fid"], cols["nid"]):
                if (t, g, f) != group:
                    if len(members) > 1:
                        settle(members)
                    group, members = (t, g, f), set()
                members.add(n)
        if len(members) > 1:
            settle(members)
        if not losers:
            return bumped
        for key in losers:
            names.bump(key)
        bumped += len(losers)
    raise RuntimeError("demo copy: sibling name collisions did not settle")


def _fake_path(path: str, sep: str, names: _Names, last_is_file: bool) -> str:
    segs = path.split(sep)
    out = []
    for i, seg in enumerate(segs):
        if _is_root_segment(seg):
            out.append(seg)
        else:
            out.append(names.get("f" if last_is_file and i == len(segs) - 1 else "d", seg))
    return sep.join(out)


def _fake_source(source: str, names: _Names) -> str:
    user, host, path = _source_parts(source)
    return (f"{names.namer.user(user)}@{names.namer.host(host)}:"
            f"{_fake_path(path, _sep_for(path), names, False)}")


_GLOB_SPLIT = re.compile(r"([*?]|\[[^\]]*\])")
_EXT_LIKE = re.compile(r"\.[A-Za-z0-9]{1,6}")


class _Faker:
    """Fakes the names inside kopia's own replies with the copy's name map."""

    def __init__(self, names: _Names, src_map: dict[str, str]):
        self.names = names
        self.namer = names.namer
        self.src_map = src_map
        # Whole real strings (source keys, paths, user@host, user, host) -> fake, longest first:
        # the safety net for any field the structured fakers below do not know about.
        pairs: dict[str, str] = {}
        for real, fake in src_map.items():
            ru, rh, rp = _source_parts(real)
            fu, fh, fp = _source_parts(fake)
            pairs.update({real: fake, f"{ru}@{rh}": f"{fu}@{fh}", rp: fp, ru: fu, rh: fh})
        pairs = {k: v for k, v in pairs.items() if len(k) >= 3 and not _is_root_segment(k)}
        self._pairs = pairs
        self._re = (re.compile("|".join(re.escape(k) for k in sorted(pairs, key=len, reverse=True)),
                               re.IGNORECASE) if pairs else None)
        self._lower = {k.lower(): v for k, v in pairs.items()}

    def text(self, s: str) -> str:
        return self._re.sub(lambda m: self._lower[m.group(0).lower()], s) if self._re else s

    def scrub(self, obj):
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {self.text(k): self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.scrub(v) for v in obj]
        return obj

    def target(self, key: str) -> str:
        """A source key or policy target (``user@host:path``, ``user@host``, ``@host``)."""
        if key in self.src_map:
            return self.src_map[key]
        if key.startswith("(") or "@" not in key:
            return key
        user_host, sep, path = key.partition(":")
        user, _, host = user_host.rpartition("@")
        out = f"{self.namer.user(user) if user else ''}@{self.namer.host(host) if host else ''}"
        return out + (":" + _fake_path(path, _sep_for(path), self.names, False) if sep else "")

    def _glob(self, seg: str, kind: str) -> str:
        out = []
        for tok in _GLOB_SPLIT.split(seg):
            if not tok or _GLOB_SPLIT.fullmatch(tok) or _EXT_LIKE.fullmatch(tok) \
                    or not re.search(r"[A-Za-z]", tok) or fakenames.is_generic(tok.strip(".-_ ")):
                out.append(tok)
            else:
                out.append(self.names.get(kind, tok))
        return "".join(out)

    def rule(self, rule: str) -> str | None:
        """One ignore rule: every literal path piece takes the same fake name as in the index."""
        s = rule.strip()
        if not s or s.startswith("#"):
            return None
        neg = s.startswith("!")
        body = s[1:] if neg else s
        parts = body.split("/")
        out = []
        for i, seg in enumerate(parts):
            if seg in ("", "*", "**") or seg.lower() in fakenames.TECHNICAL:
                out.append(seg)
                continue
            last = i == len(parts) - 1
            kind = "f" if last and not body.endswith("/") and "." in seg.strip("*.") else "d"
            out.append(self._glob(seg, kind) if _GLOB_SPLIT.search(seg) else self.names.get(kind, seg))
        return ("!" if neg else "") + "/".join(out)

    def rules_text(self, text: str) -> str:
        return "\n".join(r for r in (self.rule(x) for x in text.splitlines()) if r) + "\n"

    def policy(self, pol: dict) -> dict:
        pol = copy.deepcopy(pol) if isinstance(pol, dict) else {}
        pol.pop("actions", None)    # script paths and commands
        files = pol.get("files")
        if isinstance(files, dict):
            if isinstance(files.get("ignore"), list):
                files["ignore"] = [r for r in (self.rule(x) for x in files["ignore"]) if r]
            if isinstance(files.get("ignoreDotFiles"), list):
                files["ignoreDotFiles"] = [n if n.lower() in _DOT_IGNORE_KEPT else self.names.get("f", n)
                                           for n in files["ignoreDotFiles"]]
        return self.scrub(pol)

    def policy_export(self, data: dict) -> dict:
        return {self.target(k): self.policy(v) for k, v in (data or {}).items()}

    def snapshots(self, snaps: list) -> list:
        out = []
        for snap in snaps or []:
            snap = copy.deepcopy(snap)
            src = snap.get("source") or {}
            fake = self.target(f"{src.get('userName', '')}@{src.get('host', '')}:{src.get('path', '')}")
            user, host, path = _source_parts(fake)
            snap["source"] = {"host": host, "userName": user, "path": path}
            snap["description"] = ""
            snap.pop("tags", None)
            root = snap.get("rootEntry")
            if isinstance(root, dict):
                if root.get("name") and not _is_root_segment(root["name"]):
                    root["name"] = self.names.get("d", root["name"])
                if isinstance(root.get("summ"), dict):
                    root["summ"].pop("errors", None)
            out.append(self.scrub(snap))
        return out

    def _hex(self, value: str) -> str:
        digest = hmac.new(self.namer._key, value.encode(), hashlib.sha256).hexdigest()
        return (digest * 4)[:len(value)]

    def repo_status(self, st: dict) -> dict:
        st = copy.deepcopy(st) if isinstance(st, dict) else {}
        st["configFile"] = demoreplies.DISPLAY_CONFIG
        storage = st.get("storage")
        if isinstance(storage, dict):
            cfg = storage.get("config") if isinstance(storage.get("config"), dict) else {}
            fake: dict = {}
            for k, v in cfg.items():
                if not isinstance(v, str):
                    fake[k] = v
                elif k == "path":
                    fake[k] = r"D:\Backups\Kopia" if re.match(r"[A-Za-z]:|\\\\", v) else "/srv/backups/kopia"
                elif k in ("bucket", "container", "share"):
                    fake[k] = "demo-backups"
            storage["config"] = fake
        opts = st.get("clientOptions")
        if isinstance(opts, dict):
            if opts.get("hostname"):
                opts["hostname"] = self.namer.host(opts["hostname"])
            if opts.get("username"):
                opts["username"] = self.namer.user(opts["username"])
            if "description" in opts:
                opts["description"] = "Demo repository"
        for k in ("uniqueIDHex", "uniqueID"):
            if isinstance(st.get(k), str):
                st[k] = self._hex(st[k])
        for k in ("cacheDirectory", "cacheDir"):
            st.pop(k, None)
        return self.scrub(st)

    def maintenance(self, info: dict) -> dict:
        info = copy.deepcopy(info) if isinstance(info, dict) else {}

        def drop_errors(obj):
            if isinstance(obj, dict):
                obj.pop("error", None)
                for v in obj.values():
                    drop_errors(v)
            elif isinstance(obj, list):
                for v in obj:
                    drop_errors(v)
        drop_errors(info)
        return self.scrub(info)

    def cache_info(self, text: str) -> str:
        text = re.sub(r"[A-Za-z]:\\Users\\[^\\\r\n]+\\", r"C:\\Users\\demo\\", text)
        text = re.sub(r"/home/[^/\r\n]+/", "/home/demo/", text)
        return self.text(text)


def _record(repo: str, fk: _Faker, cur, tables: set[str]) -> dict:
    """Run the read commands the screens need against the real repo; store faked replies."""
    from . import kopia as K
    from .rulelayers import declared_names
    out: dict[str, dict] = {}

    def put(args: list[str], rc: int, text: str) -> None:
        out[demoreplies.key(args)] = {"rc": rc, "out": text, "err": "" if rc == 0 else demoreplies.UNAVAILABLE}

    def run(args: list[str]):
        res = K.kopia(repo, args)
        if res.returncode != 0:
            log.warning("demo record: kopia %s failed (rc=%s)", " ".join(args[:2]), res.returncode)
        return res

    res = run(["snapshot", "list", "--json"])
    snaps = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else []
    put(["snapshot", "list", "--json"], res.returncode, json.dumps(fk.snapshots(snaps)))

    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        res = run(["policy", "export", "--to-file", tmp, "--overwrite"])
        with open(tmp, encoding="utf-8") as fh:
            exported = json.load(fh) if res.returncode == 0 else {}
    except (OSError, ValueError):
        exported = {}
    finally:
        os.remove(tmp)
    put(["policy", "export"], 0 if exported else 1, json.dumps(fk.policy_export(exported)))

    targets = sorted({f"{s['source'].get('userName', '')}@{s['source'].get('host', '')}:"
                      f"{s['source'].get('path', '')}" for s in snaps if isinstance(s.get("source"), dict)}
                     | {k for k in exported if not k.startswith("(")})
    for target in targets:
        res = run(["policy", "show", target, "--json"])
        body = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else {}
        put(["policy", "show", fk.target(target), "--json"], res.returncode, json.dumps(fk.policy(body)))
    res = run(["policy", "show", "--global", "--json"])
    body = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else {}
    put(["policy", "show", "--global", "--json"], res.returncode, json.dumps(fk.policy(body)))

    res = run(["repository", "status", "--json"])
    body = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else {}
    put(["repository", "status", "--json"], res.returncode, json.dumps(fk.repo_status(body)))
    res = run(["maintenance", "info", "--json"])
    body = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else {}
    put(["maintenance", "info", "--json"], res.returncode, json.dumps(fk.maintenance(body)))
    # Not `blob stats`: it lists every blob in the repository (many minutes on a big one), and
    # its screen is on-demand only. A demo answers it "not available".
    res = run(["content", "stats"])
    put(["content", "stats"], res.returncode, res.stdout if res.returncode == 0 else "")
    res = run(["cache", "info"])
    put(["cache", "info"], res.returncode, fk.cache_info(res.stdout) if res.returncode == 0 else "")

    # Ignore files: their rules are read through `kopia show <content id>`.
    names = [n.lower() for n in declared_names(repo, exported)]
    if names and {"pathx", "cid"} <= tables:
        ph = ",".join("?" * len(names))
        ids = [r[0] for r in cur.execute(
            f"SELECT DISTINCT c.content_id FROM pathx p JOIN cid c ON c.cid_id = p.cid_id "
            f"WHERE lower(p.name) IN ({ph})", names).fetchall() if r[0]]
        for cid in ids:
            res = run(["show", cid])
            put(["show", cid], res.returncode, fk.rules_text(res.stdout) if res.returncode == 0 else "")
    return out


def _write(cur, sql: str, out_path: str) -> int:
    reader = cur.execute(sql).to_arrow_reader(200_000)
    rows = 0
    with pq.ParquetWriter(out_path, reader.schema) as writer:
        for batch in reader:
            writer.write_batch(batch)
            rows += batch.num_rows
    return rows


def make_copy(repos: str | list[str], style: str, finish: bool = True) -> dict:
    """Write a demo copy of one or more repos (demo ids 1, 2, ... in the given order).
    Returns counts only, never names."""
    ids = _repo_ids(repos)
    namer = fakenames.Namer(fakenames.load_key(DATA_DIR), style)
    target = copy_dir(ids, style)
    building = target + ".building"
    if os.path.isdir(building):
        shutil.rmtree(building)
    os.makedirs(building)
    records, forbidden, per_repo = [], set(), {}
    allowed: set[str] = set()   # every repo's fake names and extensions
    identity: set[str] = set()  # user and computer names: never allowed
    for n, repo in enumerate(ids, 1):
        demo_id = str(n)
        if len(ids) > 1:
            _step(f"repo {repo} as demo repo {demo_id}")
        part = _copy_repo(repo, demo_id, building, namer)
        records.append(_demo_record(part.pop("replies"), demo_id,
                                    "Demo" if len(ids) == 1 else f"Demo {demo_id}"))
        forbidden |= part.pop("forbidden")
        allowed |= part.pop("allowed")
        identity |= part.pop("identity")
        per_repo[repo] = part
    # One repo's fake name or extension may equal another repo's real name.
    forbidden -= allowed - identity

    from .demobz import BZ_RECORD
    _dump(os.path.join(building, "repos.json"), {"version": 1, "cache_dir": None,
                                                 "repos": records + [BZ_RECORD]})
    from .dataupgrade import DATA_VERSION
    _dump(os.path.join(building, "data_version.json"),
          {"version": DATA_VERSION, "app": DATA_VERSION, "at": _dt.datetime.now().timestamp()})
    _dump(os.path.join(building, "settings.json"), _DEMO_SETTINGS)
    _dump(os.path.join(building, "demo.json"),
          {"style": style, "source_repos": ids, "app_version": APP_VERSION, "backblaze": True,
           "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})

    _step("building caches and checking screens")
    finished = _finish(building, forbidden) if finish else {}
    prune(building, [r["id"] for r in records])

    _step("checking files for real names")
    leaks = leak_check(building, forbidden, demoreplies.machine_paths())
    if leaks["leaked"] or finished.get("leaked"):
        raise RuntimeError(f"demo copy failed its leak check: {leaks['leaked']} in files "
                           f"({leaks['files']}), {finished.get('leaked', 0)} in screens "
                           f"({finished.get('routes')})")
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.replace(building, target)
    totals = {k: sum(p[k] for p in per_repo.values())
              for k in ("renamed", "re_rolled", "kept_by_design", "recorded")}
    result = {"path": target, "rows": per_repo[ids[0]]["rows"] if len(ids) == 1 else
              {r: p["rows"] for r, p in per_repo.items()},
              **totals, "checked_names": len(forbidden),
              "caches": finished.get("stages"), "cache_errors": finished.get("errors"),
              "screens_checked": finished.get("swept"), "screens_failed": finished.get("failed")}
    log.info("demo copy of repos %s (%s): %s names, %s re-rolled", ",".join(ids), style,
             totals["renamed"], totals["re_rolled"])
    return result


def _copy_repo(repo: str, demo_id: str, building: str, namer: fakenames.Namer) -> dict:
    """One repo's faked index, manifest, recorded replies and size map as ``repo<demo_id>``."""
    pool = os.path.join(building, f"repo{demo_id}", "pool")
    os.makedirs(pool)
    gen = store.current(repo)
    try:
        cur = gen.cursor()
        cur.execute("SET threads = 4")   # the index worker's cap; more threads multiply hash memory
        # A one-off CLI run on the developer's machine: a quarter of RAM, never below the store's tier.
        import psutil
        cur.execute(f"SET memory_limit = '{max(store.memory_limit_bytes(), psutil.virtual_memory().total // 4)}B'")
        tables = _tables(gen)
        names = _Names(namer, _categories(cur, tables))
        _step("resolving name collisions")
        bumped = _resolve_collisions(cur, names, tables)

        sources = dict(cur.execute("SELECT src_id, source FROM src").fetchall()) if "src" in tables else {}
        src_map = {s: _fake_source(s, names) for s in sources.values()}
        extra = list(gen.manifest.get("sources", {}))
        for t in ("isnaps", "sanalysis"):
            if t in tables:
                extra += [r[0] for r in cur.execute(f"SELECT DISTINCT source FROM {t}").fetchall()]
        for s in extra:
            src_map.setdefault(s, _fake_source(s, names))
        cur.register("_demo_src", pa.table({"real": list(src_map), "fake": list(src_map.values())}))

        if "dir" in tables:
            dirs = cur.execute("SELECT dir_id, src_id, dir_path, name, parent_id FROM dir "
                               "ORDER BY depth, dir_id").fetchall()
            fake_dir: dict[int, str] = {}
            rows = {"dir_id": [], "dir_path": [], "name": [], "sep": []}
            for dir_id, src_id, dir_path, name, parent_id in dirs:
                sep = _sep_for(_source_parts(sources[src_id])[2]) if src_id in sources else "\\"
                if parent_id is None or parent_id not in fake_dir:
                    fpath = _fake_path(dir_path, sep, names, False)
                    fname = fpath.split(sep)[-1] if name else name
                else:
                    fname = names.get("d", name)
                    fpath = _join(fake_dir[parent_id], fname, sep)
                fake_dir[dir_id] = fpath
                rows["dir_id"].append(dir_id)
                rows["dir_path"].append(fpath)
                rows["name"].append(fname)
                rows["sep"].append(sep)
            cur.register("_demo_dir", pa.table(rows))

        cur.register("_demo_names", names.table())
        written: dict[str, int] = {}
        selects = {
            "src": "SELECT t.src_id, m.fake AS source FROM src t JOIN _demo_src m ON m.real = t.source",
            "isnaps": f"SELECT t.* REPLACE ('{demo_id}' AS repo, m.fake AS source) FROM isnaps t "
                      "JOIN _demo_src m ON m.real = t.source",
            "sanalysis": f"SELECT t.* REPLACE ('{demo_id}' AS repo, m.fake AS source) FROM sanalysis t "
                         "JOIN _demo_src m ON m.real = t.source",
            "dir": "SELECT t.* REPLACE (f.dir_path AS dir_path, f.name AS name) FROM dir t "
                   "JOIN _demo_dir f USING (dir_id) ORDER BY t.dir_id",
            # ext is re-derived from the fake name with the indexer's rule: the real column
            # keeps anything after the last dot (a computer name in "file.Some-PC", say).
            "pathx": """SELECT t.* REPLACE (n.fake AS name,
                            CASE WHEN contains(n.fake, '.')
                                 THEN lower(list_extract(string_split(n.fake, '.'), -1)) ELSE '' END AS ext,
                            CASE
                            WHEN f.dir_path = '' THEN f.sep || n.fake
                            WHEN right(f.dir_path, 1) = f.sep THEN f.dir_path || n.fake
                            ELSE f.dir_path || f.sep || n.fake END AS full_path)
                        FROM pathx t JOIN _demo_dir f USING (dir_id)
                        JOIN _demo_names n ON n.kind = 'f' AND n.real = t.name
                        ORDER BY t.src_id, t.path_id""",
            "tent": """SELECT * FROM (SELECT t.* REPLACE (n.fake AS name,
                            (min(t.pos) OVER (PARTITION BY t.dir_id)
                             + row_number() OVER (PARTITION BY t.dir_id ORDER BY n.fake) - 1) AS pos,
                            NULL::VARCHAR AS s_errors, NULL::VARCHAR AS extra)
                       FROM tent t JOIN _demo_names n
                         ON n.kind = (CASE WHEN t.etype = 1 THEN 'd' ELSE 'f' END) AND n.real = t.name)
                       ORDER BY dir_id, pos""",
        }
        for name in sorted(tables):
            sql = selects.get(name, f"SELECT * FROM {name}")
            _step(f"writing {name}")
            written[name] = _write(cur, sql, os.path.join(pool, f"{name}.parquet"))
        real_rows = {name: cur.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                     for name in tables}
        _step("recording kopia replies")
        replies = _record(repo, _Faker(names, src_map), cur, tables)
        exts = ({r[0] for r in cur.execute("SELECT DISTINCT ext FROM read_parquet(?)",
                                             [os.path.join(pool, "pathx.parquet")]).fetchall() if r[0]}
                if "pathx" in tables else set())
    finally:
        gen.release()

    lost = {n: real_rows[n] - written[n] for n in tables if real_rows[n] != written[n]}
    if lost:
        raise RuntimeError(f"demo copy dropped rows: {lost}")

    m = gen.manifest
    manifest = {
        "generation": m.get("generation"), "repo": demo_id, "schema_version": m.get("schema_version"),
        "counters": m.get("counters", {}), "stats": m.get("stats", {}), "encrypted": False,
        "sources": {src_map[k]: dict(v, key=src_map[k]) if isinstance(v, dict) and "key" in v else v
                    for k, v in m.get("sources", {}).items()},
        "tree_incomplete": [src_map[s] for s in m.get("tree_incomplete", []) if s in src_map],
        "tree_forced_for": [src_map[s] for s in m.get("tree_forced_for", []) if s in src_map],
        "artifacts": {name: [f"pool/{name}.parquet"] for name in sorted(tables)},
    }
    _dump(os.path.join(building, f"repo{demo_id}", "manifest.json"), manifest)
    _dump(demoreplies.replies_path(demo_id, building), replies)
    sizemap = os.path.join(CACHEDIR, f"sizemap_repo{repo}.pkl")
    if os.path.isfile(sizemap):   # content ids and sizes only
        os.makedirs(os.path.join(building, "cache"), exist_ok=True)
        shutil.copyfile(sizemap, os.path.join(building, "cache", f"sizemap_repo{demo_id}.pkl"))
    return {"rows": written, "renamed": len(names.fake), "re_rolled": bumped,
            "kept_by_design": sum(1 for k in names.fake if names.kept(*k)), "recorded": len(replies),
            "replies": replies, "forbidden": forbidden_names(names, src_map, exts),
            "allowed": {f.lower() for f in names.fake.values()} | exts, "identity": _identity(src_map)}


def prune(folder: str, repo_ids: list[str]) -> None:
    """Remove what the cache build wrote that a finished demo does not need. Kept: the fixed
    files, the Backblaze data, and every file that belongs to one of the demo's repos (stage
    outputs such as ignore_files_repo<N>.parquet live next to the index); temp folders go."""
    from .repos import _belongs_to
    for entry in os.listdir(folder):
        own = any(_belongs_to(entry, r) for r in repo_ids) and not entry.startswith("duck_tmp")
        if own or entry in _KEEP | BZ_KEEP or entry.startswith("kopialens_bz.sqlite"):
            continue
        path = os.path.join(folder, entry)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)


def _demo_record(replies: dict, demo_id: str, name: str) -> dict:
    """The demo's repos.json record, with storage facts from the faked repository status."""
    from .repos import scrub_storage_config
    rec = {"id": demo_id, "name": name, "short_name": name, "kind": "kopia", "demo": True}
    raw = replies.get(demoreplies.key(["repository", "status", "--json"]), {}).get("out") or "{}"
    status = json.loads(raw)
    storage, volume = status.get("storage") or {}, status.get("volume") or {}
    if status.get("uniqueIDHex"):
        rec["unique_id"] = status["uniqueIDHex"]
    if storage:
        rec["storage"] = {"type": storage.get("type") or "",
                          "config": scrub_storage_config(storage.get("config") or {})}
    if volume:
        rec["capacity"] = {"capacity": volume.get("capacity"), "available": volume.get("available")}
    rec["status_at"] = _dt.datetime.now().timestamp()
    return rec


def _app_vocabulary() -> set[str]:
    """Words the app's own screens and code use. A real folder that shares one is not a leak
    signal when it shows up in app text (user and computer names always are)."""
    words: set[str] = set()
    for base, exts in ((os.path.join(HERE, "frontend"), (".html", ".js")), (os.path.join(HERE, "app"), (".py",))):
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("tests", "vendor", "__pycache__")]
            for f in files:
                if f.endswith(exts):
                    with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                        text = fh.read().lower()
                        # Plain words, and joined ones whole ("own-writes", "sizemap_repo").
                        words.update(re.findall(r"[a-z][a-z0-9]{2,}", text))
                        words.update(re.findall(r"[a-z][a-z0-9._-]{2,}[a-z0-9]", text))
    # The connect form's field labels and flags are app text too, phrases included.
    from .repoconnect import provider_list
    words.update(seg for text in _json_strings(json.dumps(provider_list())) for seg in _segments(text))
    return words


def _step(text: str) -> None:
    log.info("demo copy: %s", text)
    print(f"{_dt.datetime.now():%H:%M:%S} {text}", flush=True)


def _dump(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def forbidden_names(names: _Names, real_sources, exts: set[str] = frozenset()) -> set[str]:
    """Every real name that must never appear in a copy, lowercased. A name equal to a file
    extension in the data, or a standard ignore-file name, stays real by design."""
    forbidden = {real.lower() for (kind, real) in names.fake if not names.kept(kind, real)}
    for s in real_sources:
        path = _source_parts(s)[2]
        forbidden.update(seg.lower() for seg in path.replace("\\", "/").split("/")
                         if not _is_root_segment(seg) and not names.kept("d", seg))
    # Names under 3 characters, all digits, dates or hex ids identify nothing and collide with
    # drive letters, extensions, run dates and kopia ids.
    forbidden = {f for f in forbidden if len(f) >= 3 and not f.isdigit() and not _GENERATED.fullmatch(f)}
    forbidden -= set(exts) | _DOT_IGNORE_KEPT | _KOPIA_WORDS | _app_vocabulary()
    # A fake name that equals a real name elsewhere comes from the word lists, not from the data.
    forbidden -= {fake.lower() for fake in names.fake.values()}
    return forbidden | _identity(real_sources)


def _identity(real_sources) -> set[str]:
    """The user and computer names in the real sources, lowercased."""
    out: set[str] = set()
    for s in real_sources:
        user, host, _path = _source_parts(s)
        out.update({user.lower(), host.lower()})
    return {i for i in out if len(i) >= 3}


def _segments(text: str, split: str = _TEXT_SPLIT):
    return re.split(split, text.lower())


def _json_strings(value: str):
    """Strings inside a JSON payload, or the value itself when it is not JSON."""
    if value[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            yield from _strings(parsed)
            return
    yield value


def leak_check(copy_root: str, forbidden: set[str], machine: list[str] = ()) -> dict:
    """Count real names found as whole path segments in any file of the copy, and this PC's own
    folders (``machine``, lowercased) anywhere outside the faked index. Counts only."""
    leaked, files = 0, []
    con = duckdb.connect()
    con.register("_forbidden", pa.table({"seg": sorted(forbidden)}))
    for dirpath, _dirs, fnames in os.walk(copy_root):
        for fname in fnames:
            if fname == "demo.json":   # the copy's own fixed bookkeeping
                continue
            path = os.path.join(dirpath, fname)
            hits = 0
            if fname.endswith(".parquet"):
                fwd = path.replace("\\", "/").replace("'", "''")
                schema = pq.read_schema(path)
                for col in schema.names:
                    if col in _KEPT_COLUMNS or not pa.types.is_string(schema.field(col).type):
                        continue
                    if os.path.basename(dirpath) == "pool":
                        hits += con.execute(f"""
                            SELECT count(*) FROM (SELECT unnest(regexp_split_to_array(lower("{col}"),
                                '{_SEG_SPLIT}')) AS seg FROM read_parquet('{fwd}'))
                            WHERE seg IN (SELECT seg FROM _forbidden)""").fetchone()[0]
                    else:   # side caches: one JSON payload per row
                        for (value,) in con.execute(f'SELECT "{col}" FROM read_parquet(\'{fwd}\')').fetchall():
                            hits += sum(1 for text in _json_strings(value or "")
                                        for seg in _segments(text) if seg in forbidden)
                            hits += sum((value or "").lower().count(m) for m in machine)
            else:
                with open(path, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
                strings = list(_json_strings(text)) if fname.endswith(".json") else [text]
                hits = sum(1 for t in strings for seg in _segments(t) if seg in forbidden)
                hits += sum(text.lower().count(m) for m in machine)
            if hits:
                leaked += hits
                files.append(os.path.relpath(path, copy_root))
    con.close()
    return {"leaked": leaked, "files": files, "checked": len(forbidden)}


_FIELD_NAME = re.compile(r"[a-z][A-Za-z0-9_]*")


def _strings(obj):
    """Every string in a JSON value, nested JSON text included. Lowercase identifier keys are
    field names, not data."""
    if isinstance(obj, str):
        yield from _json_strings(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if not _FIELD_NAME.fullmatch(k):
                yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _hash(salt: str, text: str) -> str:
    return hashlib.sha256((salt + text).encode()).hexdigest()[:20]


def _child_env(folder: str) -> dict:
    env = dict(os.environ, KOPIALENS_DATA_DIR=folder, PYTHONIOENCODING="utf-8")
    for k in ("KOPIALENS_CACHE_DIR", "KOPIALENS_CONFIG_DIR", "KOPIALENS_REPOS_FILE"):
        env.pop(k, None)
    return env


def _finish(folder: str, forbidden: set[str]) -> dict:
    """Build the copy's caches and sweep its screens, in a process whose data folder IS the copy.
    The child gets salted hashes of the forbidden names, never the names."""
    salt = secrets.token_hex(8)
    check = folder + ".check"
    _dump(check, sorted(_hash(salt, f) for f in forbidden))
    env = _child_env(folder)
    env.update(KOPIALENS_DEMO_CHECK=check, KOPIALENS_DEMO_SALT=salt)
    try:
        res = subprocess.run([sys.executable, "-m", "app.core.demo", "--finish"], cwd=HERE, env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=3600)
    finally:
        os.remove(check)
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.startswith("{")]
    if res.returncode != 0 or not lines:
        raise RuntimeError(f"demo cache build failed (exit {res.returncode}): {(res.stderr or '')[-600:]}")
    return json.loads(lines[-1])


def _finish_child() -> None:
    """Runs inside the copy: every index stage after "index" for each repo, then a sweep of
    every screen, Backblaze included."""
    from config import kopia_repos

    from ..index import worker
    stages, errors = set(), {}
    for repo in kopia_repos():
        summary = worker.run(repo, skip_index=True)
        stages |= set(summary.get("stages") or {})
        if summary.get("errors"):
            errors[repo] = summary["errors"]
    try:
        _demo_backblaze(kopia_repos())
        stages.add("backblaze")
    except Exception as exc:  # noqa: BLE001 - the copy is still a full kopia demo without it
        errors["bz"] = str(exc)[:300]
    sweep = _sweep(os.environ["KOPIALENS_DEMO_SALT"], os.environ["KOPIALENS_DEMO_CHECK"], kopia_repos())
    print(json.dumps({"stages": sorted(stages), "errors": errors or None, **sweep}))


def _demo_backblaze(repos: list[str]) -> None:
    """Fake Backblaze data from the copy's own latest files (already faked), then indexed."""
    from . import demobz
    files, user, host = [], "demo", "DEMO-PC"
    for repo in repos:
        gen = store.current(repo)
        try:
            cur = gen.cursor()
            files += cur.execute("SELECT full_path, size::BIGINT, epoch(last_seen) FROM pathx "
                                 "WHERE in_latest").fetchall()
            row = cur.execute("SELECT source FROM src LIMIT 1").fetchone()
        finally:
            gen.release()
        if row:
            user, host, _path = _source_parts(row[0])
    root = os.path.join(DATA_DIR, "bzdata")
    demobz.write(root, files, user=user, host=host)
    out = demobz.index(root)
    if not out.get("ok", True):
        raise RuntimeError(f"Backblaze index: {out.get('reason')}")


def _sweep(salt: str, check_path: str, repos: list[str]) -> dict:
    """GET every API route that needs no input, for each repo, and count forbidden names in the
    replies."""
    from fastapi.testclient import TestClient

    from .. import main as app_main
    with open(check_path, encoding="utf-8") as fh:
        hashes = set(json.load(fh))
    client = TestClient(app_main.app, base_url="http://127.0.0.1")
    calls: list[tuple[str, dict | None, str]] = []
    for path, ops in app_main.app.openapi()["paths"].items():
        get = ops.get("get")
        if not get or not path.startswith("/api/") or "{" in path or any(w in path for w in _SWEEP_SKIP):
            continue
        params = {p["name"]: p for p in get.get("parameters", []) if p.get("in") == "query"}
        if any(p.get("required") and name != "repo" for name, p in params.items()):
            continue
        if "repo" in params:
            calls += [(path, {"repo": r}, "") for r in repos]
        else:
            calls.append((path, None, ""))
    for r in repos:
        calls += [(path, {"repo": r, **params}, " (with input)") for path, params in _sample_calls(r)]
    machine = demoreplies.machine_paths()
    swept, leaked, routes, failed = 0, 0, [], []
    for path, params, note in calls:
        try:
            resp = client.get(path, params=params)
        except Exception:  # noqa: BLE001 - a route that crashes leaks nothing; it is counted
            continue
        swept += 1
        if resp.status_code >= 400:
            failed.append(f"{path} {resp.status_code}{note}")
        hits = sum(1 for text in _json_strings(resp.text) for seg in _segments(text)
                   if _hash(salt, seg) in hashes)
        low = resp.text.lower()
        hits += sum(low.count(m) for m in machine)
        if hits:
            leaked += hits
            routes.append(path)
    return {"swept": swept, "leaked": leaked, "routes": routes, "failed": failed}


def _sample_calls(repo: str) -> list[tuple[str, dict]]:
    """The main screens that need input, called with a real source, folder, file and snapshots
    from the copy itself."""
    gen = store.current(repo)
    try:
        cur = gen.cursor()
        row = cur.execute("""SELECT sr.source, d.dir_path, p.full_path FROM pathx p
                             JOIN dir d ON d.dir_id = p.dir_id JOIN src sr ON sr.src_id = p.src_id
                             WHERE d.depth >= 2 ORDER BY p.versions DESC, p.size DESC LIMIT 1""").fetchone()
        snaps = [r[0] for r in cur.execute(
            "SELECT snapshot_id FROM isnaps WHERE source = ? ORDER BY start_time DESC LIMIT 2",
            [row[0]]).fetchall()] if row else []
    finally:
        gen.release()
    if not row or not snaps:
        return []
    source, folder, file = row
    calls = [("/api/snapshots", {"source": source}),
             ("/api/tree", {"source": source, "prefix": folder, "snapshot_id": snaps[0]}),
             ("/api/folder/contents", {"source": source, "prefix": folder}),
             ("/api/folder/summary", {"source": source, "prefix": folder}),
             ("/api/folder/policy", {"source": source, "prefix": folder}),
             ("/api/policies/detail", {"target": source}),
             ("/api/search/detail", {"source": source, "full_path": file}),
             ("/api/search/versions", {"source": source, "full_path": file}),
             ("/api/policies/path-status", {"source": source, "path": file, "is_folder": "false"})]
    if len(snaps) == 2:
        calls.append(("/api/snapshots/diff", {"id1": snaps[1], "id2": snaps[0]}))
    return calls


def _copies() -> list[str]:
    base = os.path.join(DATA_DIR, DEMO_DIR)
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base)
                  if os.path.isfile(os.path.join(base, d, "demo.json")))


def open_copy(name: str, port: int) -> None:
    """Run KopiaLens on a demo copy until closed (a separate instance on its own port)."""
    import webbrowser
    folder = os.path.join(DATA_DIR, DEMO_DIR, name)
    env = _child_env(folder)
    env.update(KOPIALENS_PORT=str(port), KOPIALENS_TRAY_PORT=str(port + 1),
               KOPIALENS_PROFILE="desktop")
    url = f"http://127.0.0.1:{port}"
    print(f"Demo '{name}' at {url} - close this window to stop it.")
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=HERE, env=env)
    webbrowser.open(url)
    proc.wait()


def _choose(prompt: str, options: list[str]) -> str | None:
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    raw = input(f"{prompt} (number, Enter to cancel): ").strip()
    return options[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(options) else None


def menu() -> None:
    from config import REPOS
    print("KopiaLens demo copies\n")
    action = _choose("What would you like to do", ["Open a demo copy", "Make a demo copy",
                                                   "Make sample data (invented, no repo needed)"])
    if action and action.startswith("Make sample data"):
        from . import sampledata
        print("Working - this generates a made-up repository and indexes it (under a minute).")
        r = sampledata.build()
        print(f"Done: {r['files']} files in {r['snapshots']} snapshots, {r['bytes'] / 1e6:.0f} MB, "
              f"{r['swept']} screens checked.\n{r['path']}\nOpen it as the demo copy 'sample'.")
    if action == "Open a demo copy":
        copies = _copies()
        if not copies:
            print("No demo copies yet - make one first.")
        elif (name := _choose("Which copy", copies)):
            open_copy(name, 7323)
    elif action == "Make a demo copy":
        real = [r for r, c in REPOS.items() if c.get("kind", "kopia") == "kopia" and not c.get("demo")]
        options = {f"{r} - {REPOS[r]['name']}": r for r in real}
        if len(real) > 1:
            options[f"{' + '.join(real)} - all of them in one demo"] = ",".join(real)
        repo = _choose("Which repository", list(options))
        style = _choose("Which name style", list(fakenames.STYLES)) if repo else None
        if repo and style:
            print("Working - this reads the index and kopia once, then builds the demo's caches.")
            r = make_copy(options[repo], style)
            print(f"Done: {r['renamed']} names faked, {r['screens_checked']} screens checked, "
                  f"no real names found.\n{r['path']}")
    input("\nPress Enter to close.")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Demo copies of a repo's index.")
    ap.add_argument("--repo", help='a repo id, or several in one demo: "1,2"')
    ap.add_argument("--style", choices=fakenames.STYLES, default="generic")
    ap.add_argument("--open", metavar="COPY", help="run KopiaLens on a demo copy")
    ap.add_argument("--port", type=int, default=7323)
    ap.add_argument("--finish", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.finish:
        _finish_child()
    elif args.open:
        open_copy(args.open, args.port)
    elif args.repo:
        r = make_copy(args.repo, args.style)
        print(json.dumps({k: v for k, v in r.items() if k != "path"}, indent=1))
        print("written to", r["path"])
    else:
        menu()


if __name__ == "__main__":
    main()
