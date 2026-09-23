"""The encryption switch (): rewrite local data between plaintext and encrypted.

Scope is KopiaLens's own files only - the kopia repository is never touched, and the rewrite is
a DuckDB re-encode (repo 1: 699 MB, 15 artifacts, 65M rows, 6.9 s measured).

One repo per call: every artifact file of the published generation is re-encoded to the other
state, verified (footer magic + row count), and only then swapped in by the usual atomic
manifest publication. A crash anywhere before that swap leaves the current generation exactly
as it was; the staged files are overwritten by the next attempt.

Side caches (churn, folder prewarm, policy prices, ranking, the table cache) sit outside a
generation and carry their own state in the Parquet footer magic, so they stay readable in
either state. They are rewritten in place, one file at a time; a file that cannot be rewritten
is dropped rather than left in the wrong state - a cache rebuilds itself, a leak does not.

The manifest's ``encrypted`` flag is the FACT readers follow. The setting is the INTENT, which
the caller flips before the rewrite starts, so the two disagree only while a switch is pending -
and that is exactly what ``pending()`` reports.
"""

from __future__ import annotations

import os
import re
import time

import duckdb

from config import CACHEDIR, DATA_DIR, REPOS, repo_kind

from ..core.applog import get_logger
from . import crypto, publish, store

log = get_logger("keyswitch")

_GEN_RE = re.compile(r"g(\d+)\.parquet$")


def repo_states() -> list[dict]:
    """Per kopia repo: what its data currently is, and whether that disagrees with the level.

    A repo with no generation yet is never pending - its first build follows the setting, so
    ``encrypted`` reports what that build will write.
    """
    from . import crypto
    want = crypto.level() != crypto.LEVEL_OFF
    out = []
    for repo in REPOS:
        if repo_kind(repo) == "backblaze":
            continue                    # the BZ store follows on its own item ()
        manifest = store.read_manifest(repo)
        if manifest is None:
            out.append({"repo": repo, "generation": None, "encrypted": want, "pending": False})
        else:
            encrypted = bool(manifest.get("encrypted"))
            out.append({"repo": repo, "generation": manifest.get("generation"),
                        "encrypted": encrypted, "pending": encrypted != want})
    return out


def pending() -> list[str]:
    """Repos whose published generation is not in the state the setting asks for."""
    return [r["repo"] for r in repo_states() if r["pending"]]


def bz_state() -> dict:
    """The Backblaze parquet store's own state line (): the level covers it at Everything."""
    from ..backblaze import pq as bz_pq
    from . import crypto
    manifest = bz_pq.read_manifest()
    if manifest is None:
        return {"repo": "bz", "generation": None, "encrypted": False, "pending": False,
                "exists": False}
    encrypted = bool(manifest.get("encrypted"))
    return {"repo": "bz", "generation": manifest.get("generation"), "encrypted": encrypted,
            "exists": True,
            "pending": encrypted != (crypto.level() == crypto.LEVEL_EVERYTHING)}


def _fwd(path: str) -> str:
    return path.replace("\\", "/").replace("'", "''")


def switch_repo(repo: str, on: bool, say=None) -> dict:
    """Rewrite one repo's published generation into ``on``. Returns a summary dict.

    Caller (the job) flips the setting first; this function leaves the manifest as the only
    source of truth for what is on disk. Raises PermissionError when the key is locked - both
    directions need it, one to write the new files and the other to read the old ones.
    """
    say = say or (lambda text: None)
    if not crypto.unlocked():
        raise crypto.StoreLocked("unlock KopiaLens before switching encryption")
    root = store.repo_dir(repo)
    with publish.WriterLock(repo):
        manifest = store.read_manifest(repo)
        if manifest is None:
            return {"repo": repo, "files": 0, "rows": 0, "mb": 0.0, "seconds": 0.0,
                    "note": "no generation yet - the next index writes it in the chosen state"}
        if bool(manifest.get("encrypted")) == on:
            return {"repo": repo, "files": 0, "rows": 0, "mb": 0.0, "seconds": 0.0,
                    "note": "already in the requested state"}
        started = time.time()
        out = _rewrite_generation(root, manifest, on, os.path.join(root, "_staging"),
                                  os.path.join(DATA_DIR, f"duck_tmp_repo{repo}"), say)
        new = dict(manifest)
        new["generation"] = out["generation"]
        new["artifacts"] = out["artifacts"]
        new["encrypted"] = on
        new["_moves"] = out["moves"]
        publish.publish(root, new)
        _drop_superseded(root, manifest.get("artifacts") or {}, out["artifacts"],
                         lambda: store.close_repo(repo), say)
        summary = {"repo": repo, "generation": out["generation"], "files": out["files"],
                   "rows": out["rows"], "mb": out["mb"], "encrypted": on,
                   "seconds": round(time.time() - started, 1)}
        log.info("repo=%s encryption switched to %s: %s", repo,
                 "encrypted" if on else "plain", summary)
        say(f"repo {repo}: {out['files']} file(s), {out['mb']} MB, {summary['seconds']}s")
        return summary


def switch_bz(on: bool, say=None) -> dict:
    """Rewrite the Backblaze parquet generations between plaintext and encrypted ().

    The BZ store is covered at the Everything level and uses the same DuckDB footer key as the
    index. The slim SQLite (history/state tables) is NOT covered yet - see the report: its
    path-bearing tables are queried by SQL, so encrypting them needs a query-layer change.
    """
    from ..backblaze import pq as bz_pq
    say = say or (lambda text: None)
    if not crypto.unlocked():
        raise crypto.StoreLocked("unlock KopiaLens before switching encryption")
    lock = bz_pq.WriterLock()
    lock.__enter__()
    try:
        manifest = bz_pq.read_manifest()
        if manifest is None:
            return {"repo": "bz", "files": 0, "rows": 0, "mb": 0.0, "seconds": 0.0,
                    "note": "no generation yet - the next refresh writes it in the chosen state"}
        if bool(manifest.get("encrypted")) == on:
            return {"repo": "bz", "files": 0, "rows": 0, "mb": 0.0, "seconds": 0.0,
                    "note": "already in the requested state"}
        root = bz_pq.root()
        started = time.time()
        out = _rewrite_generation(root, manifest, on, bz_pq.staging_dir(),
                                  os.path.join(root, "_tmp"), say)
        drives = {drive: out["rel_map"].get(rel, rel)
                  for drive, rel in (manifest.get("drives") or {}).items()}
        bz_pq.publish(out["generation"], drives, out["moves"], encrypted=on)
        _drop_superseded(root, manifest.get("artifacts") or {}, out["artifacts"],
                         bz_pq.close_open_generation, say)
        summary = {"repo": "bz", "generation": out["generation"], "files": out["files"],
                   "rows": out["rows"], "mb": out["mb"], "encrypted": on,
                   "seconds": round(time.time() - started, 1)}
        log.info("backblaze encryption switched to %s: %s",
                 "encrypted" if on else "plain", summary)
        say(f"backblaze: {out['files']} file(s), {out['mb']} MB, {summary['seconds']}s")
        return summary
    finally:
        lock.__exit__(None, None, None)


def _rewrite_generation(root: str, manifest: dict, on: bool, staging: str, tmp: str,
                        say) -> dict:
    """Re-encode every artifact file of one generation, verified, into ``on``.

    Shared by the index store and the Backblaze store (same manifest shape). The caller holds
    that store's writer lock and does the publication; this function only produces staged files
    and a new artifact list, and never touches the published generation - a crash leaves it
    exactly as it was, and the next attempt overwrites the staged files.
    """
    current = bool(manifest.get("encrypted"))
    gen = int(manifest.get("generation") or 0) + 1
    os.makedirs(staging, exist_ok=True)
    limit, threads = store.resolve_limits()
    con = duckdb.connect()
    # The session database follows the READ side: while converting plaintext -> encrypted the
    # data being read (and spilled) is plaintext either way, so encrypting the session protects
    # nothing until the new files exist.
    session = crypto.apply(con, tmp, limit, threads, encrypted=current)
    try:
        # Both directions need the key registered: reading the current state names it, and
        # writing the target state names it. Only one of the two is encrypted, but the key must
        # be on the connection for whichever one is.
        crypto.register_key(con, encrypted=True)
        artifacts_out: dict[str, list[str]] = {}
        moves: list[tuple[str, str]] = []
        rel_map: dict[str, str] = {}
        total_rows = total_bytes = 0
        for name, rels in (manifest.get("artifacts") or {}).items():
            out_rels = []
            for index, rel in enumerate(rels):
                src = os.path.join(root, rel)
                new_rel = _GEN_RE.sub(f"g{gen}.parquet", rel)
                if new_rel == rel:
                    # Never let an unexpected name overwrite the file it read from.
                    raise RuntimeError(f"cannot version the artifact path {rel!r}")
                staged = os.path.join(staging, f"switch_{name}_{index}_g{gen}.parquet")
                rows = con.execute("SELECT count(*) FROM read_parquet(%s)"
                                   % crypto.parquet_read(_fwd(src), encrypted=current)).fetchone()[0]
                con.execute("COPY (SELECT * FROM read_parquet(%s)) TO '%s' (%s)"
                            % (crypto.parquet_read(_fwd(src), encrypted=current), _fwd(staged),
                               crypto.copy_options(encrypted=on)))
                if crypto.file_is_encrypted(staged) != on:
                    raise RuntimeError(f"{rel}: the rewrite produced a file in the wrong state")
                back = con.execute("SELECT count(*) FROM read_parquet(%s)"
                                   % crypto.parquet_read(_fwd(staged), encrypted=on)).fetchone()[0]
                if back != rows:
                    raise RuntimeError(f"{rel}: the rewrite changed the row count {rows} -> {back}")
                total_rows += rows
                total_bytes += os.path.getsize(staged)
                moves.append((staged, new_rel))
                rel_map[rel] = new_rel
                out_rels.append(new_rel)
            artifacts_out[name] = out_rels
            say(f"{name}: {len(rels)} file(s) rewritten")
    finally:
        try:
            con.close()
        finally:
            if session is not None:
                session.cleanup()
    return {"generation": gen, "artifacts": artifacts_out, "moves": moves, "rel_map": rel_map,
            "files": len(moves), "rows": total_rows, "mb": round(total_bytes / 1e6, 1)}


def apply_level(level: str, say=None) -> dict:
    """Move the whole install to ``level``: the index generations, the file layer, Backblaze.

    Every part is the same verified rewrite as the per-repo switch, so a failure or a crash
    leaves each file readable and the caller can simply run it again. A repo whose index pass is
    running (or that another write job holds) is DEFERRED and reported rather than forced.
    """
    from ..core import filecrypto, nightly
    say = say or (lambda text: None)
    level = (level or "").lower()
    if level not in crypto.LEVELS:
        raise ValueError(f"unknown encryption level {level!r}")
    if level != crypto.LEVEL_OFF and not crypto.configured():
        raise RuntimeError("set up encryption before turning it on")
    if crypto.configured() and not crypto.unlocked():
        raise crypto.StoreLocked("unlock KopiaLens before changing the encryption level")
    want_index = level != crypto.LEVEL_OFF
    out: dict = {"level": level, "repos": {}, "files": {}, "backblaze": {}, "deferred": []}
    for state in repo_states():
        repo = state["repo"]
        if not state.get("generation"):
            out["repos"][repo] = {"note": "no generation yet - the next index writes it in "
                                         "the chosen state"}
            continue
        if state["encrypted"] == want_index:
            out["repos"][repo] = {"note": "already in the requested state"}
            continue
        if nightly.is_running(repo):
            out["deferred"].append(repo)
            say(f"repo {repo}: an index pass is running - deferred to the next attempt")
            continue
        try:
            out["repos"][repo] = switch_repo(repo, want_index, say=say)
        except RuntimeError as exc:      # another writer holds the repo's lock
            out["deferred"].append(repo)
            say(f"repo {repo}: another write is in progress - deferred ({exc})")
    out["files"] = filecrypto.switch_files(level == crypto.LEVEL_EVERYTHING, say=say)
    bz = bz_state()
    if bz["exists"]:
        want_bz = level == crypto.LEVEL_EVERYTHING
        if bz["encrypted"] != want_bz:
            try:
                out["backblaze"] = switch_bz(want_bz, say=say)
            except RuntimeError as exc:
                out["deferred"].append("bz")
                say(f"backblaze: another write is in progress - deferred ({exc})")
        else:
            out["backblaze"] = {"note": "already in the requested state"}
    else:
        out["backblaze"] = {"note": "no Backblaze index yet"}
    log.info("encryption level %s applied: %s", level, out.get("deferred") or "nothing deferred")
    return out


def _drop_superseded(root: str, old: dict, new: dict, close_open, say) -> int:
    """Take the superseded copy off disk now instead of waiting for the GC's one-hour grace.

    Only files the new generation does not name, and only once this process holds no generation
    for the repo - a reader mid-query must not lose the file under it (on POSIX that delete
    succeeds and the read then fails). A busy repo keeps the old files and the normal GC
    collects them, which is what the pool is designed to do.
    """
    for _ in range(25):
        if close_open():
            break
        time.sleep(0.2)
    else:
        say("the previous copy is still in use - it goes with the next index pass")
        return 0
    keep = {rel for rels in new.values() for rel in rels}
    gone = 0
    for rel in [rel for rels in old.values() for rel in rels]:
        if rel in keep:
            continue
        try:
            os.remove(os.path.join(root, rel))
            gone += 1
        except OSError:
            pass                    # held open outside this process: the next GC gets it
    if gone:
        say(f"removed {gone} superseded file(s)")
    return gone


def cache_files() -> list[str]:
    """Every side-cache Parquet file: the data folder's per-repo caches and ``data/cache/``."""
    out = []
    for base in (DATA_DIR, CACHEDIR):
        try:
            for fn in os.listdir(base):
                if fn.endswith(".parquet"):
                    out.append(os.path.join(base, fn))
        except OSError:
            pass
    return out


def switch_caches(on: bool, say=None) -> dict:
    """Rewrite every side cache into ``on``. Returns ``{files, dropped, seconds}``.

    Verification is the same shape as an artifact: footer magic plus a row count. A file that
    cannot be rewritten is REMOVED - it is a derived cache, it rebuilds on the next use, and
    leaving a plaintext file behind on an encrypted install is the failure being fixed.
    """
    say = say or (lambda text: None)
    if not crypto.unlocked():
        raise PermissionError("unlock KopiaLens before switching encryption")
    started = time.time()
    done = dropped = 0
    limit, threads = store.resolve_limits()
    tmp_dir = os.path.join(DATA_DIR, "duck_tmp_keyswitch")
    con = duckdb.connect()
    session = crypto.apply(con, tmp_dir, limit, threads, encrypted=on)
    try:
        crypto.register_key(con, encrypted=True)
        for path in cache_files():
            current = crypto.file_is_encrypted(path)
            if current == on:
                continue
            tmp = path + f".{os.getpid()}.switch.tmp"
            try:
                rows = con.execute("SELECT count(*) FROM read_parquet(%s)"
                                   % crypto.parquet_read(_fwd(path), encrypted=current)).fetchone()[0]
                con.execute("COPY (SELECT * FROM read_parquet(%s)) TO '%s' (%s)"
                            % (crypto.parquet_read(_fwd(path), encrypted=current), _fwd(tmp),
                               crypto.copy_options(encrypted=on)))
                if crypto.file_is_encrypted(tmp) != on:
                    raise RuntimeError("the rewrite produced a file in the wrong state")
                back = con.execute("SELECT count(*) FROM read_parquet(%s)"
                                   % crypto.parquet_read(_fwd(tmp), encrypted=on)).fetchone()[0]
                if back != rows:
                    raise RuntimeError(f"row count changed {rows} -> {back}")
                os.replace(tmp, path)
                done += 1
            except Exception as exc:  # noqa: BLE001 - one cache must not stop the rest
                log.warning("cache %s could not be rewritten (%s) - dropping it so it rebuilds "
                            "in the right state", os.path.basename(path), exc)
                for stale in (tmp, path):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
                dropped += 1
    finally:
        try:
            con.close()
        finally:
            if session is not None:
                session.cleanup()
    out = {"files": done, "dropped": dropped, "seconds": round(time.time() - started, 1)}
    if done or dropped:
        say(f"caches: {done} rewritten, {dropped} dropped")
        log.info("side caches switched to %s: %s", "encrypted" if on else "plain", out)
    return out
