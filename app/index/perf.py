"""Run-scoped performance record for the index worker.

Off unless the worker starts a run, so every hook is a cheap no-op in the app process and in
tests. A run records, per stage and sub-phase: wall time, kopia calls and kopia time, peak RSS of
this process and of its kopia children, and peak DuckDB disk spill. It also keeps the counts each
phase grows with (folders, entries, content ids, snapshots) and the size of what was written, so
first-index time and index size can be modelled from the snapshot listing alone.

The record holds counts and timings only, never paths or source names.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager

from ..core.applog import get_logger
from ..core import atomicfile

log = get_logger("perf")

_RUN: "Run | None" = None
_KEEP_RUNS = 30          # records kept per repo in the jsonl
_MAX_LEVELS = 300


def _kopia_now() -> tuple[int, float]:
    from ..core import kopia as _k
    return int(_k.KOPIA_CALLS), float(_k.KOPIA_TIME)


def _dir_bytes(path: str) -> int:
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                    elif e.is_dir(follow_symlinks=False):
                        total += _dir_bytes(e.path)
                except OSError:
                    pass
    except OSError:
        pass
    return total


class _Phase:
    __slots__ = ("name", "t0", "k0", "rss", "kids", "spill", "laps", "lap")

    def __init__(self, name: str) -> None:
        self.name = name
        self.t0 = time.perf_counter()
        self.k0 = _kopia_now()
        self.rss = self.kids = self.spill = 0
        self.laps: list[dict] = []
        self.lap: "_Phase | None" = None

    def peak(self, rss: int, kids: int, spill: int) -> None:
        self.rss = max(self.rss, rss)
        self.kids = max(self.kids, kids)
        self.spill = max(self.spill, spill)
        if self.lap is not None:
            self.lap.peak(rss, kids, spill)

    def close(self) -> dict:
        if self.lap is not None:
            self.laps.append(self.lap.close())
            self.lap = None
        calls, secs = _kopia_now()
        rec = {"name": self.name, "secs": round(time.perf_counter() - self.t0, 1),
               "kopia_calls": calls - self.k0[0], "kopia_secs": round(secs - self.k0[1], 1),
               "rss_mb": self.rss // 2**20, "kopia_rss_mb": self.kids // 2**20,
               "spill_mb": self.spill // 2**20}
        if self.laps:
            rec["laps"] = self.laps
        return rec


class Run:
    def __init__(self, repo: str, sample_every: float = 1.0) -> None:
        self.repo = repo
        self.started = time.time()
        self.phases: list[dict] = []
        self.levels: list[dict] = []
        self.counts: dict = {}
        self.inputs: dict = {}
        self.outputs: dict = {}
        self.estimate: dict | None = None     # first index only: size and time ranges
        self.spill_dirs: list[str] = []
        self.peak_rss = self.peak_kids = self.peak_spill = 0
        self._open: list[_Phase] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._every = sample_every
        self._thread = threading.Thread(target=self._loop, daemon=True, name="perf-sampler")

    def _sample(self) -> None:
        rss = kids = 0
        try:
            import psutil
            me = psutil.Process()
            rss = me.memory_info().rss
            for c in me.children(recursive=True):
                try:
                    kids += c.memory_info().rss
                except Exception:  # noqa: BLE001 - child exited between listing and reading
                    pass
        except Exception:  # noqa: BLE001 - instrumentation must never affect the run
            pass
        spill = sum(_dir_bytes(d) for d in self.spill_dirs)
        with self._lock:
            self.peak_rss = max(self.peak_rss, rss)
            self.peak_kids = max(self.peak_kids, kids)
            self.peak_spill = max(self.peak_spill, spill)
            for p in self._open:
                p.peak(rss, kids, spill)

    def _loop(self) -> None:
        while not self._stop.wait(self._every):
            self._sample()

    def start(self) -> "Run":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def start(repo: str, spill_dirs: list[str] | None = None) -> Run:
    global _RUN
    _RUN = Run(repo)
    _RUN.spill_dirs = list(spill_dirs or [])
    return _RUN.start()


def active() -> Run | None:
    return _RUN


@contextmanager
def phase(name: str):
    """A timed phase; nests. The innermost open phase owns any ``lap`` calls."""
    run = _RUN
    if run is None:
        yield
        return
    p = _Phase(name)
    run._sample()
    with run._lock:
        parent = run._open[-1] if run._open else None
        run._open.append(p)
    try:
        yield
    finally:
        run._sample()
        with run._lock:
            run._open.remove(p)
            rec = p.close()
            if parent is not None:
                if parent.lap is not None:
                    parent.lap.laps.append(rec)
                else:
                    parent.laps.append(rec)
            else:
                run.phases.append(rec)
        log.info("perf: %-28s %7.1fs  kopia %6.1fs/%-5d rss %5d MB  kopia rss %5d MB  spill %5d MB",
                 name, rec["secs"], rec["kopia_secs"], rec["kopia_calls"], rec["rss_mb"],
                 rec["kopia_rss_mb"], rec["spill_mb"])


def lap(name: str) -> None:
    """End the current sequential step inside the innermost phase and start the next one."""
    run = _RUN
    if run is None:
        return
    run._sample()
    with run._lock:
        if not run._open:
            return
        owner = run._open[-1]
        if owner.lap is not None:
            owner.laps.append(owner.lap.close())
        owner.lap = _Phase(name)


def count(**kw) -> None:
    if _RUN is not None:
        _RUN.counts.update({k: v for k, v in kw.items() if v is not None})
        if _RUN.estimate is not None and kw.get("tree_entries") is not None:
            from .estimate import from_tree       # the tree is read: its size is exact
            n, f = int(kw["tree_entries"]), int(kw.get("tree_folders") or 0)
            _RUN.estimate = from_tree([n, n], [f, f], exact=True)


def level(**kw) -> None:
    if _RUN is not None and len(_RUN.levels) < _MAX_LEVELS:
        _RUN.levels.append(kw)


def kopia_now() -> tuple[int, float]:
    return _kopia_now()


def inputs_from_listing(srcs: dict) -> dict:
    """What the snapshot listing says about a repo before anything is indexed.

    ``summ.dirs``/``summ.files`` count a snapshot's whole subtree, so the latest snapshot per source
    is a floor for distinct folders/files and the sum over all snapshots a ceiling.
    """
    out = {"sources": len(srcs), "snapshots": 0, "latest_files": 0, "latest_dirs": 0,
           "latest_bytes": 0, "all_files": 0, "all_dirs": 0, "max_snaps_per_source": 0}
    for snaps in srcs.values():
        out["snapshots"] += len(snaps)
        out["max_snaps_per_source"] = max(out["max_snaps_per_source"], len(snaps))
        latest = None
        for s in snaps:
            summ = (s.get("rootEntry") or {}).get("summ") or {}
            out["all_files"] += int(summ.get("files") or 0)
            out["all_dirs"] += int(summ.get("dirs") or 0)
            if latest is None or str(s.get("startTime") or "") > str(latest.get("startTime") or ""):
                latest = s
        summ = ((latest or {}).get("rootEntry") or {}).get("summ") or {}
        out["latest_files"] += int(summ.get("files") or 0)
        out["latest_dirs"] += int(summ.get("dirs") or 0)
        out["latest_bytes"] += int(summ.get("size") or 0)
    return out


def set_inputs(srcs: dict, first: bool = False) -> None:
    if _RUN is not None and not _RUN.inputs:
        _RUN.inputs = inputs_from_listing(srcs)
        if first:
            from .estimate import from_listing
            _RUN.estimate = from_listing(srcs)


def outputs_from_manifest(root: str, artifacts: dict) -> dict:
    """Bytes on disk per artifact kind of the published generation, plus the whole repo folder."""
    by_kind: dict[str, int] = {}
    for name, rels in (artifacts or {}).items():
        total = 0
        for rel in rels:
            try:
                total += os.path.getsize(os.path.join(root, rel))
            except OSError:
                pass
        by_kind[name] = total
    return {"artifact_bytes": by_kind, "generation_bytes": sum(by_kind.values()),
            "repo_dir_bytes": _dir_bytes(root)}


def set_outputs(root: str, artifacts: dict) -> None:
    if _RUN is not None:
        _RUN.outputs = outputs_from_manifest(root, artifacts)


def machine() -> dict:
    out: dict = {"cpus": os.cpu_count()}
    from ..core.host import ram_bytes
    if ram := ram_bytes():
        out["ram_gb"] = round(ram / 2**30, 1)
    try:
        from . import store
        out["duckdb_memory"], out["duckdb_threads"] = store.resolve_limits()
    except Exception:  # noqa: BLE001
        pass
    return out


def records_path(repo: str) -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, "runs", f"perf_repo{repo}.jsonl")


def finish(summary: dict) -> dict | None:
    """Stop sampling, append the record (trimmed to the last few runs) and return it."""
    global _RUN
    run, _RUN = _RUN, None
    if run is None:
        return None
    run.stop()
    run._sample()
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(run.started)),
           "repo": run.repo, "full": bool(summary.get("full")), "ok": bool(summary.get("ok")),
           "trigger": summary.get("trigger"), "elapsed": summary.get("elapsed"),
           "machine": machine(), "inputs": run.inputs, "counts": run.counts,
           "outputs": run.outputs, "peak_rss_mb": run.peak_rss // 2**20,
           "peak_kopia_rss_mb": run.peak_kids // 2**20, "peak_spill_mb": run.peak_spill // 2**20,
           "phases": run.phases, "levels": run.levels}
    try:
        path = records_path(run.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines: list[str] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        lines = (lines + [json.dumps(rec)])[-_KEEP_RUNS:]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        atomicfile.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - a record that fails to save must not fail the run
        log.warning("perf record for repo=%s not saved: %s", run.repo, exc)
    log.info("perf: peak rss %d MB, kopia rss %d MB, spill %d MB; counts %s; inputs %s; "
             "generation %s bytes", rec["peak_rss_mb"], rec["peak_kopia_rss_mb"],
             rec["peak_spill_mb"], run.counts, run.inputs,
             (run.outputs or {}).get("generation_bytes"))
    return rec
