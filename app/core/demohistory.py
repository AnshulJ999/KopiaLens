"""An invented task history for demo copies and sample data: what the Tasks tab and the audit log
show. Scheduled backups come from each repo's own snapshot list (times, file counts, errors); the
rest is a mix of the jobs KopiaLens runs, with paths and ids from the index and log text from the
demo's pretend engine. Runs in a process whose data folder is the demo's.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import random
import re

from config import DATA_DIR

from . import demoreplies, demosim, filecrypto
from .applog import JOB_LOG_DIR
from .engine import ENGINE_LOG_DIR
from .jobs import AUDIT_ACTION, KINDS

DAYS = 14
_HOUR = 3600.0
_DAY = 86400.0
_MAX_BACKUPS = 400          # per repo: the newest ones
_ACTOR = "admin"
HISTORY_FILES = ("engine_tasks.jsonl", "jobs_history.jsonl", "audit.jsonl")

# (hours before now, kind, repo slot, options): the newest day is mixed on purpose, so the Tasks
# tab's first screen shows more than backups. Repo slot 0 = the first repo, 1 = the second...
PLAN = [
    (0.7, "restore", 0, {"what": "folder"}),
    (1.6, "snapshot_verify", 0, {"percent": 5}),
    (2.4, "policy_write", 0, {"what": "exclude"}),
    (5.2, "snapshot_delete", 0, {"count": 3}),
    (9.5, "maintenance", 0, {"full": True}),
    (13.0, "snapshot_estimate", 0, {}),
    (20.5, "remove_files", 0, {}),
    (27.0, "snapshot_pin", 0, {}),
    (40.0, "maintenance", 1, {"full": True}),
    (52.0, "prune_versions", 0, {}),
    (70.0, "restore", 0, {"what": "file"}),
    (95.0, "blob_stats", 0, {}),
    (118.0, "snapshot_verify", 1, {"percent": 0}),
    (140.0, "policy_write", 0, {"what": "retention"}),
    (165.0, "snapshot_delete", 0, {"count": 2, "dry": True}),
    (190.0, "maintenance", 2, {"full": True}),
    (215.0, "remove_files", 0, {"dry": True}),
    (240.0, "restore", 0, {"what": "file", "fail": True}),
    (262.0, "snapshot_pin", 0, {"remove": True}),
    (290.0, "snapshot_estimate", 0, {}),
    (312.0, "maintenance", 0, {"full": True}),
]
_ENGINE_FULL_EVERY = 24.0   # hours between the engine's full maintenance runs
_ENGINE_QUICK_EVERY = 4.0   # its quick runs (routine: shown for the last two days only)


def _iso(t: float) -> str:
    return _dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


# A readable folder name: short, with a letter beyond hex (skips cache ids like "-1624ec9b").
_WORDY = re.compile(r"^(?=.{1,30}$).*[g-zG-Z]")


def _clock(t: float) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _snapshots(repo: str) -> list[dict]:
    try:
        with open(demoreplies.replies_path(repo), encoding="utf-8") as fh:
            rec = json.load(fh).get(demoreplies.key(["snapshot", "list", "--json"])) or {}
        return json.loads(rec.get("out") or "[]")
    except (OSError, ValueError):
        return []


def _ts(text: str) -> float:
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _src(s: dict) -> str:
    src = s.get("source") or {}
    return f"{src.get('userName')}@{src.get('host')}:{src.get('path')}"


def _counter(value: int, units: str = "", level: str = "") -> dict:
    return {"value": int(value), "units": units, "level": level}


def _backup_counters(stats: dict, took: float, rng: random.Random) -> dict:
    total = int(stats.get("totalSize") or 0)
    files = int(stats.get("fileCount") or 0)
    hashed = int(stats.get("nonCachedFiles") or 0)
    # What a backup reads is the changed files, at disk speed: never the whole source in seconds.
    hashed_bytes = int(min(total, hashed * rng.lognormvariate(math.log(1_500_000), 1.2),
                           max(took, 1.0) * rng.uniform(60e6, 220e6)))
    return {
        "Cached Bytes": _counter(total - hashed_bytes, "bytes"), "Cached Files": _counter(files - min(hashed, files)),
        "Errors": _counter(stats.get("errorCount") or 0, level="error"),
        "Excluded Directories": _counter(stats.get("excludedDirCount") or 0),
        "Excluded Files": _counter(stats.get("excludedFileCount") or 0),
        "Hashed Bytes": _counter(hashed_bytes, "bytes"), "Hashed Files": _counter(hashed),
        "Processed Bytes": _counter(total, "bytes"), "Processed Files": _counter(files),
        "Uploaded Bytes": _counter(int(hashed_bytes * rng.uniform(0.03, 0.4)), "bytes"),
    }


class _Writer:
    def __init__(self):
        self.engine: list[dict] = []
        self.jobs: list[dict] = []
        self.audit: list[tuple[float, dict]] = []
        os.makedirs(JOB_LOG_DIR, exist_ok=True)
        os.makedirs(ENGINE_LOG_DIR, exist_ok=True)

    def log(self, path: str, lines: list[str]) -> str:
        filecrypto.write_lines(path, lines)
        return path

    def add_audit(self, t: float, event: str, action: str, repo: str | None, summary: dict,
                  rc: int | None = None, duration: float | None = None, log_file: str | None = None,
                  actor: str = _ACTOR) -> None:
        self.audit.append((t, {"ts": _dt.datetime.fromtimestamp(t).isoformat(timespec="seconds"),
                               "event": event, "action": action, "repo": repo, "summary": summary, "rc": rc,
                               "duration_s": round(duration, 1) if duration is not None else None,
                               "log_file": log_file, "actor": actor}))

    def flush(self) -> None:
        self.engine.sort(key=lambda r: r["start"])
        self.jobs.sort(key=lambda r: r["created"])
        self.audit.sort(key=lambda a: a[0])
        for name, rows in zip(HISTORY_FILES, (self.engine, self.jobs, [a for _, a in self.audit])):
            filecrypto.write_lines(os.path.join(DATA_DIR, name), [json.dumps(r, ensure_ascii=False) for r in rows])


class _Repo:
    """What one repo's invented jobs draw on: its snapshots, big files and folders."""

    def __init__(self, repo: str, snaps: list[dict], rng: random.Random):
        from ..index import store
        self.repo, self.snaps, self.rng = repo, snaps, rng
        gen = store.current(repo)
        try:
            cur = gen.cursor()
            self.files = cur.execute("""
                SELECT p.full_path, p.size, s.source, c.content_id FROM pathx p
                JOIN src s USING (src_id) JOIN cid c ON c.cid_id = p.cid_id
                WHERE p.in_latest AND p.size > 20000000 ORDER BY p.size DESC LIMIT 60""").fetchall()
            rows = cur.execute("""
                SELECT p.src_id, d.dir_path, sum(p.size)::BIGINT, count(*) FROM pathx p JOIN dir d USING (dir_id)
                WHERE p.in_latest GROUP BY 1, 2""").fetchall()
            srcs = dict(cur.execute("SELECT src_id, source FROM src").fetchall())
        finally:
            gen.release()
        self.folders: dict[str, list[tuple[str, int, int]]] = {}
        for src_id, path, size, n in rows:
            self.folders.setdefault(srcs.get(src_id, ""), []).append((path, int(size), int(n)))
        for v in self.folders.values():
            v.sort(key=lambda r: -r[1])

    def file(self) -> tuple[str, int, str, str | None]:
        return self.rng.choice(self.files[:30]) if self.files else ("", 0, "", None)

    def folder(self) -> tuple[str, int, int]:
        pool = [f for v in self.folders.values() for f in v[1:12] if 50 <= f[2] <= 20000
                and _WORDY.search(os.path.basename(f[0].rstrip("\\")))]
        return self.rng.choice(pool) if pool else ("", 0, 0)

    def old_snaps(self, n: int) -> list[dict]:
        older = [s for s in self.snaps[:-40]] or self.snaps
        start = self.rng.randrange(max(1, len(older) - n))
        return older[start:start + n]


def _engine(w: _Writer, repo: str, snaps: list[dict], info: _Repo | None, now: float, days: int,
            rng: random.Random) -> None:
    """Scheduled backups and the engine's maintenance, one server run per day."""
    since = now - days * _DAY
    recent = [s for s in snaps if since <= _ts(s["startTime"]) <= now][-_MAX_BACKUPS:]
    numbers: dict[int, int] = {}

    def ident(t: float) -> tuple[str, int]:
        tag = int(t // _DAY * _DAY)
        numbers[tag] = numbers.get(tag, 0) + 1
        if numbers[tag] == 1 and (len(numbers) == 1 or rng.random() < 0.25):   # the engine restarts now and then
            w.add_audit(tag, "executed", "engine-start", repo, {"pid": rng.randint(2000, 60000)}, actor="system")
        return f"{tag}", numbers[tag]

    for s in recent:
        start, end = _ts(s["startTime"]), _ts(s["endTime"])
        tag, n = ident(start)
        src = _src(s)
        root = (s.get("source") or {}).get("path") or ""
        lines = [f"{_clock(start)} uploading {src}", f"{_clock(start)} starting upload of {src}",
                 f"{_clock(start)} uploading\tsource={src} previousManifests=1 parallel=8"]
        for path, size, files in (info.folders.get(src, []) if info else [])[:14]:
            rel = path[len(root):].strip("\\/").replace("\\", "/") if path.lower().startswith(root.lower()) else ""
            if rel:
                lines.append(f"{_clock(start)} snapshotted directory\tpath={rel} dur={rng.uniform(0.5, 9):.4f}ms "
                             f"size={size} files={files} dirs=1 errors=0")
        lines.append(f"{_clock(end)} created snapshot {s.get('id')}")
        path = w.log(os.path.join(ENGINE_LOG_DIR, f"task_repo{repo}_{tag}_{n}.log"), lines)
        w.engine.append({"id": f"engine:{repo}:{tag}.{n}", "repo": repo, "origin": "engine", "kind": "Snapshot",
                         "description": f"{src} at {_iso(start)}", "source": src, "status": "success",
                         "start": start, "end": end, "error": "", "progress": "",
                         "counters": _backup_counters(s.get("stats") or {}, end - start, rng), "log_path": path})
    t = since + rng.uniform(1, 6) * _HOUR
    while t < now - _HOUR:
        _maint(w, repo, t, True, ident, rng)
        t += _ENGINE_FULL_EVERY * _HOUR + rng.uniform(-40, 40) * 60
    t = now - 2 * _DAY
    while t < now - 0.5 * _HOUR:
        _maint(w, repo, t, False, ident, rng)
        t += _ENGINE_QUICK_EVERY * _HOUR + rng.uniform(-20, 20) * 60


def _maint(w: _Writer, repo: str, t: float, full: bool, ident, rng: random.Random) -> None:
    tag, n = ident(t)
    took = rng.uniform(20, 140) if full else rng.uniform(0.05, 0.4)
    mode = "full" if full else "quick"
    steps = [f"due for {mode} maintenance cycle", f"Running {mode} maintenance..."]
    steps += (["Looking for active contents...", "Looking for unreferenced contents...",
               "Rewriting contents from short packs...", "Looking for unreferenced blobs...",
               "Compacting indexes..."] if full else ["running quick epoch maintenance only",
                                                      "Advancing epoch markers..."])
    steps.append(f"Finished {mode} maintenance.")
    path = w.log(os.path.join(ENGINE_LOG_DIR, f"task_repo{repo}_{tag}_{n}.log"),
                 [f"{_clock(t + took * i / len(steps))} {x}" for i, x in enumerate(steps)])
    w.engine.append({"id": f"engine:{repo}:{tag}.{n}", "repo": repo, "origin": "engine", "kind": "Maintenance",
                     "description": "Periodic maintenance", "source": "", "status": "success",
                     "start": t, "end": t + took, "error": "", "progress": "", "counters": {}, "log_path": path})


def _sim(repo: str, args: list[str]) -> list[str]:
    lines, _rc = demosim._lines(repo, args)
    return [text for _stream, text in lines if text != demosim.DONE_NOTE]


def _job(w: _Writer, info: _Repo, t: float, kind: str, opts: dict, rng: random.Random) -> None:
    repo = info.repo
    dry = bool(opts.get("dry"))
    detail: dict = {}
    label, error, lines, took = kind, None, [], rng.uniform(1.5, 12)
    if kind == "restore":
        if opts.get("what") == "folder":
            path, size, files = info.folder()
        else:
            path, size, _src_, _cid = info.file()
            files = 1
        target = "D:\\Restored" if opts.get("what") == "folder" else "C:\\Users\\Public\\Restored"
        label, detail = os.path.basename(path.rstrip("\\")) or path, {"target": target, "items": 1}
        lines = [f"Restoring to local filesystem ({target}) with parallelism=8..."]
        if opts.get("fail"):
            error = f"exit 1: error restoring: open {target}\\{label}: Access is denied."
            lines.append(f"ERROR {error[8:]}")
        else:
            for i in range(1, 5):
                lines.append(f"Processed {files * i // 4} ({demosim._fmt_bytes(size * i / 4)}) of {files} "
                             f"({demosim._fmt_bytes(size)}).")
            lines.append(f"Restored {files} files, {max(1, files // 40) if files > 1 else 0} directories and "
                         f"0 symbolic links ({demosim._fmt_bytes(size)}).")
        took = 4 + size / 180e6
    elif kind == "snapshot_verify":
        pct = float(opts.get("percent") or 0)
        lines = _sim(repo, ["snapshot", "verify"] + ([f"--verify-files-percent={pct:g}"] if pct else []))
        label, detail = "all sources" + (f", {pct:g}% of files" if pct else ""), {"sources": 0, "percent": pct}
        took = rng.uniform(300, 1500) if pct else rng.uniform(60, 240)
    elif kind == "policy_write":
        path, _size, _files = info.folder()
        if opts.get("what") == "retention":
            label = "Global policy: retention"
            lines = ["Saved a copy of the policies before writing.", "Setting policy for (global)",
                     " - setting \"number of daily backups to keep\" to 14.", " - setting \"number of weekly backups to keep\" to 8.",
                     "Policy set: done."]
            detail = {"writes": 1}
        else:
            label = f"Exclude {os.path.basename(path)}"
            lines = ["Saved a copy of the policies before writing.", f"Setting policy for {os.path.dirname(path)}",
                     f" - adding {os.path.basename(path)}/ to \"ignored files\"", "Policy set: done."]
            detail = {"writes": 1}
        w.add_audit(t - 1, "executed", "policy-backup", repo, {"trigger": "pre-write",
                    "file": _dt.datetime.fromtimestamp(t).strftime("%Y%m%d_%H%M%S") + "_pre-write.json"})
        took = rng.uniform(1, 4)
    elif kind == "snapshot_delete":
        snaps = info.old_snaps(int(opts.get("count") or 1))
        ids = [s["id"] for s in snaps]
        lines = _sim(repo, ["snapshot", "delete", *ids] + ([] if dry else ["--delete"]))
        label, detail = f"{len(ids)} snapshot(s)" + (" - dry run" if dry else ""), {"snapshots": len(ids)}
        w.add_audit(t - 30, "generated", "snapshot-delete-commands", repo, {"ids": len(ids)})
        took = rng.uniform(2, 8)
    elif kind == "maintenance":
        lines = _sim(repo, ["maintenance", "run"] + (["--full"] if opts.get("full") else []))
        label, detail = ("full" if opts.get("full") else "quick") + " maintenance, safety full", {
            "full": bool(opts.get("full")), "safety": "full"}
        took = rng.uniform(60, 400)
    elif kind == "snapshot_estimate":
        path, _size, _files = info.folder()
        lines = _sim(repo, ["snapshot", "estimate", path])
        label, detail = path, {"path": path}
        took = rng.uniform(2, 15)
    elif kind == "remove_files":
        path, _size, source, cid = info.file()
        lines = _sim(repo, ["snapshot", "fix", "remove-files", "--object-id", cid or "", "--source", source]
                     + ([] if dry else ["--commit"]))
        n = sum(1 for x in lines if x.startswith("Processing snapshot"))
        label = f"{os.path.basename(path)} · remove 1 file from {max(1, n)} snapshots" + (" - dry run" if dry else "")
        detail = {"items": 1}
        took = rng.uniform(3, 20)
    elif kind == "snapshot_pin":
        snaps = info.old_snaps(1)
        ids = [s["id"] for s in snaps]
        lines = _sim(repo, ["snapshot", "pin", *ids, "keep"] + (["--remove"] if opts.get("remove") else []))
        label, detail = "keep", {"snapshots": len(ids), "remove": bool(opts.get("remove"))}
        took = rng.uniform(0.5, 2)
    elif kind == "prune_versions":
        path, _size, _src_, _cid = info.file()
        label, detail = "3 versions from 3 snapshots", {"versions": 3, "path": path, "snapshots": 3}
        lines = [f"Processing snapshot of {path} ({i})" for i in (1, 2, 3)] + ["Fixed and committed 3 snapshots."]
        took = rng.uniform(3, 10)
    elif kind == "blob_stats":
        lines = _sim(repo, ["blob", "stats"])
        label = "blob stats"
        took = rng.uniform(20, 90)
    job_id = "%012x" % rng.getrandbits(48)
    stamp = _dt.datetime.fromtimestamp(t).strftime("%Y%m%d_%H%M%S")
    log_path = w.log(os.path.join(JOB_LOG_DIR, f"job_{kind}_repo{repo}_{stamp}_{job_id}.log"),
                     [f"=== job {job_id} {kind} repo={repo} execute 1 command(s) ===", "", "--- command 1/1 ---"] + lines)
    detail["dry"] = dry
    state = "failed" if error else "done"
    w.jobs.append({"job_id": job_id, "kind": kind, "kind_label": KINDS[kind]["label"], "repo": repo,
                   "label": label, "dry": dry, "mutating": KINDS[kind]["mutating"] and not dry, "state": state,
                   "rc": 1 if error else 0, "error": error, "detail": detail, "created": t, "started": t,
                   "finished": t + took, "elapsed": round(took, 1), "total": 1, "done": 1, "errors": 1 if error else 0,
                   "log_path": log_path, "counters": {}})
    w.add_audit(t + took, "generated" if dry else "executed", AUDIT_ACTION.get(kind, kind), repo,
                {**detail, "batches": 1, "errors": 1 if error else 0, "stopped": False}, rc=1 if error else 0,
                duration=took, log_file=log_path)


def write(repos: list[str], now: float, seed: int = 1, days: int = DAYS) -> dict:
    """Write engine_tasks.jsonl, jobs_history.jsonl, audit.jsonl and their logs into this data
    folder, for ``repos`` (kopia repo ids, the main one first) as of ``now``."""
    rng = random.Random(seed)
    w = _Writer()
    infos: list[_Repo] = []
    for repo in repos:
        snaps = sorted(_snapshots(repo), key=lambda s: s.get("startTime") or "")
        if not snaps:
            continue
        info = _Repo(repo, snaps, rng)
        infos.append(info)
        _engine(w, repo, snaps, info, now, days, rng)
    for hours, kind, slot, opts in PLAN:
        if hours > days * 24 or not infos:
            continue
        info = infos[slot] if slot < len(infos) else infos[0]
        if kind in ("remove_files", "prune_versions") and not info.files:
            continue
        _job(w, info, now - hours * _HOUR, kind, opts, rng)
    w.flush()
    return {"engine_tasks": len(w.engine), "jobs": len(w.jobs), "audit": len(w.audit)}
