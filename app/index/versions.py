"""Index backups: older versions kept on disk, an optional copy in another folder, and switching to
the newest good copy when the current version is damaged.

A kept version is the manifest an index build replaced (``versions/g<N>.json``), so there is one
per index run: the later stages of a run publish further generations on top of the same build.
GC keeps every file a kept version names. The copy folder mirrors the current version's files
(names are generation-stamped and never reused, so only new files are copied).

"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time

from config import DATA_DIR

from ..core import filecrypto
from ..core.applog import get_logger
from ..core import atomicfile

log = get_logger("store")

MAX_KEEP = 3
_MAGIC = (b"PAR1", b"PARE")
_NOTICES = "index_notices.json"
_STATUS = "index_backup_status.json"


def _kl() -> dict:
    from ..settings import load_settings
    return load_settings().get("kopialens") or {}


def keep_count() -> int:
    try:
        n = int(_kl().get("index_keep_versions", 1))
    except (TypeError, ValueError):
        n = 1
    return max(0, min(MAX_KEEP, n))


def backup_dir() -> str:
    return str(_kl().get("index_backup_dir") or "").strip()


def _rels(manifest: dict | None) -> list[str]:
    return [r.replace("\\", "/") for rels in ((manifest or {}).get("artifacts") or {}).values()
            for r in rels]


def _public(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if not k.startswith("_")}


def _atomic_json(path: str, body: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
        fh.flush()
        os.fsync(fh.fileno())
    atomicfile.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# ---- kept versions -------------------------------------------------------------------------

def _vdir(root: str) -> str:
    return os.path.join(root, "versions")


def kept(root: str) -> list[dict]:
    """The kept manifests, newest first; each carries ``_file``."""
    try:
        names = os.listdir(_vdir(root))
    except OSError:
        return []
    out = []
    for fn in names:
        if not re.fullmatch(r"g\d+\.json", fn):
            continue
        man = _read_json(os.path.join(_vdir(root), fn))
        if man and man.get("artifacts"):
            man["_file"] = os.path.join(_vdir(root), fn)
            out.append(man)
    out.sort(key=lambda m: int(m.get("generation") or 0), reverse=True)
    return out


def keep(root: str, replaced: dict | None) -> None:
    """At an index build's publish: keep the manifest it replaces, then trim to the setting."""
    n = keep_count()
    bad = problems(root, replaced) if replaced and replaced.get("artifacts") and n > 0 else []
    if bad:
        # A damaged version is no backup; the older good ones stay.
        log.warning("index at %s: the replaced version %s is damaged, so it is not kept (%s)",
                    root, replaced.get("generation"), bad[0])
    elif replaced and replaced.get("artifacts") and n > 0:
        os.makedirs(_vdir(root), exist_ok=True)
        try:
            published = os.path.getmtime(os.path.join(root, "manifest.json"))
        except OSError:
            published = time.time()
        body = {**_public(replaced), "kept": {"at": time.time(), "published_at": published}}
        _atomic_json(os.path.join(_vdir(root), f"g{int(replaced.get('generation') or 0)}.json"), body)
    for man in kept(root)[n:]:
        try:
            os.remove(man["_file"])
        except OSError as exc:
            log.warning("could not remove kept index version %s: %s", man["_file"], exc)


def protected(root: str) -> set[str]:
    """Every pool file a kept version names (GC must leave them)."""
    return {rel for man in kept(root) for rel in _rels(man)}


def rewrite(manifest: dict, artifacts: dict, encrypted: bool) -> None:
    """A kept version after its files were re-encoded (encryption level change)."""
    body = {**_public(manifest), "artifacts": artifacts, "encrypted": encrypted}
    _atomic_json(manifest["_file"], body)


# ---- damage check + recovery ---------------------------------------------------------------

def problems(root: str, manifest: dict) -> list[str]:
    """Files a version names that are missing or not whole Parquet files. Empty = looks good."""
    bad = []
    for rel in _rels(manifest):
        p = os.path.join(root, rel)
        try:
            with open(p, "rb") as fh:
                head = fh.read(4)
                fh.seek(-4, os.SEEK_END)
                tail = fh.read(4)
            if head not in _MAGIC or tail not in _MAGIC:
                bad.append(f"{rel}: damaged")
        except OSError:
            bad.append(f"{rel}: missing")
    return bad


def _highest(root: str, current: dict | None) -> int:
    """The highest generation number this repo ever used: a recovered version is published above
    it, so no generation number (and nothing cached under one) is ever reused."""
    hi = int((current or {}).get("generation") or 0)
    for man in kept(root):
        hi = max(hi, int(man.get("generation") or 0))
    for _dirpath, _dirs, files in os.walk(os.path.join(root, "pool")):
        for fn in files:
            m = re.match(r"g(\d+)", fn)
            if m:
                hi = max(hi, int(m.group(1)))
    return hi


def recover(repo: str, reason: str) -> dict | None:
    """Make the newest good kept version (else the copy folder's) current, and tell the user.

    The caller holds the repo's writer lock. The damaged manifest is kept beside it. Returns what
    was done, or None when nothing usable exists (the caller then rebuilds from kopia)."""
    from . import publish, rankpaths, store
    root = store.repo_dir(repo)
    current = _read_json(store.manifest_path(repo))
    cands = [("kept", m) for m in kept(root)
             if not current or m.get("generation") != current.get("generation")]
    copy = _backup_manifest(repo)
    if copy:
        cands.append(("copy", copy))
    for where, man in cands:
        if where == "copy":
            try:
                _copy_back(repo, man)
            except OSError as exc:
                log.warning("repo=%s copying the index back from %s failed: %s", repo, _dest(repo), exc)
                continue
        bad = problems(root, man)
        if bad:
            log.warning("repo=%s the %s index version %s is not usable either: %s",
                        repo, where, man.get("generation"), bad[:3])
            continue
        new = {k: v for k, v in _public(man).items() if k not in ("kept", "backup")}
        new["generation"] = _highest(root, current) + 1
        mp = store.manifest_path(repo)
        if os.path.exists(mp):
            try:
                atomicfile.replace(mp, os.path.join(root, f"manifest.damaged-{int(time.time())}.json"))
            except OSError:
                pass
        publish.write_manifest(root, new)
        rankpaths.forget(repo)      # its ids may come from the damaged chain
        when = (man.get("kept") or {}).get("published_at") or (man.get("backup") or {}).get("at")
        info = {"repo": repo, "from": where, "generation": new["generation"],
                "version_at": when, "reason": str(reason)[:300]}
        log.error("repo=%s the index was damaged (%s) - switched to the %s from %s; the next "
                  "index run brings it up to date", repo, reason,
                  "kept version" if where == "kept" else "folder copy",
                  time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "?")
        _add_notice(info)
        try:
            from ..core import audit
            audit.audit("executed", "index_recovered", repo=repo, summary=info)
        except Exception:  # noqa: BLE001 - the audit line is best-effort
            pass
        return info
    log.error("repo=%s the index is damaged (%s) and no kept copy is usable - it is rebuilt "
              "from kopia", repo, reason)
    return None


# ---- notices -------------------------------------------------------------------------------

def _notices_path() -> str:
    return os.path.join(DATA_DIR, _NOTICES)


def _read_notices() -> list[dict]:
    try:
        text = filecrypto.read_text(_notices_path()) if os.path.exists(_notices_path()) else None
        data = json.loads(text) if text else []
        return data if isinstance(data, list) else []
    except ValueError:
        return []


def _add_notice(info: dict) -> None:
    items = _read_notices()
    items.append({**info, "id": secrets.token_hex(6), "at": time.time(), "kind": "recovered"})
    filecrypto.write_text(_notices_path(), json.dumps(items[-20:]))
    alert_pending()


def notices() -> list[dict]:
    return [n for n in _read_notices() if not n.get("dismissed")]


def dismiss(notice_id: str) -> None:
    items = _read_notices()
    for n in items:
        if n.get("id") == notice_id:
            n["dismissed"] = True
    filecrypto.write_text(_notices_path(), json.dumps(items))


def alert_pending() -> None:
    """Tray toast + webhook for notices not yet alerted. Only the app process alerts: a recovery
    inside the index worker is alerted when the app sees the run finish."""
    from . import store
    if store._in_worker:
        return
    items = _read_notices()
    fresh = [n for n in items if not n.get("alerted") and not n.get("dismissed")]
    if not fresh:
        return
    from ..core import engine
    for n in fresh:
        engine.alert(n.get("repo"), "warn", "Index switched to a backup copy",
                     "The current index was damaged, so KopiaLens switched to the last good copy. "
                     "The next index run brings it up to date.")
        n["alerted"] = True
    filecrypto.write_text(_notices_path(), json.dumps(items))


# ---- copy folder ---------------------------------------------------------------------------

def _dest(repo: str) -> str | None:
    d = backup_dir()
    return os.path.join(d, f"repo{repo}") if d else None


def _backup_manifest(repo: str) -> dict | None:
    d = _dest(repo)
    if not d:
        return None
    man = _read_json(os.path.join(d, "manifest.json"))
    return man if man and man.get("artifacts") else None


def _copy_back(repo: str, man: dict) -> None:
    from . import store
    root, src_root = store.repo_dir(repo), _dest(repo)
    for rel in _rels(man):
        dst = os.path.join(root, rel)
        if os.path.exists(dst) and not problems(root, {"artifacts": {"x": [rel]}}):
            continue
        _copy(os.path.join(src_root, rel), dst)


def _copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = f"{dst}.{secrets.token_hex(4)}.part"
    shutil.copyfile(src, tmp)
    atomicfile.replace(tmp, dst)


def _unique_id(repo: str) -> str:
    try:
        import config
        return config.repo_record(repo).get("unique_id") or ""
    except Exception:  # noqa: BLE001
        return ""


def _copy_lock(repo: str):
    """One copy per repo at a time, across the app and the index worker."""
    from . import publish, store
    return publish.WriterLock(repo, os.path.join(store.repo_dir(repo), ".copying"))


def copying(repo: str) -> bool:
    return _copy_lock(repo).held()


def sync_backup(repo: str) -> dict:
    """Bring the copy folder up to date with the current version. Records and returns a status."""
    from . import publish, store
    d = _dest(repo)
    if not d:
        return {"repo": repo, "skipped": True}
    if not os.path.isdir(store.repo_dir(repo)):
        return _sync_backup(repo, d)        # not indexed yet: nothing to copy or lock
    lock = _copy_lock(repo)
    try:
        lock.__enter__()
    except publish.WriterBusy:
        log.info("repo=%s index copy already running", repo)
        return {"repo": repo, "busy": True}
    try:
        return _sync_backup(repo, d)
    finally:
        lock.__exit__(None, None, None)


def _sync_backup(repo: str, d: str) -> dict:
    from . import store
    status: dict = {"at": time.time(), "path": d}
    try:
        if os.path.realpath(d).lower().startswith(os.path.realpath(DATA_DIR).lower() + os.sep):
            raise ValueError("the copy folder is inside KopiaLens's own data folder - pick another drive")
        root = store.repo_dir(repo)
        man = store.read_manifest(repo)
        if man is None:
            status.update(ok=True, note="not indexed yet")
            return status
        bad = problems(root, man)
        if bad:
            raise RuntimeError(f"the current index failed its check, so it was not copied ({bad[0]})")
        os.makedirs(d, exist_ok=True)
        uid = _unique_id(repo)
        there = (_read_json(os.path.join(d, "manifest.json")) or {}).get("backup") or {}
        if uid and there.get("unique_id") and there["unique_id"] != uid:
            raise ValueError("this folder holds another repository's index copy - pick another folder")
        rels = sorted(set(_rels(man)))
        total = copied = 0
        for rel in rels:
            src, dst = os.path.join(root, rel), os.path.join(d, rel)
            size = os.path.getsize(src)
            total += size
            if os.path.exists(dst) and os.path.getsize(dst) == size:
                continue
            _copy(src, dst)
            copied += 1
        _atomic_json(os.path.join(d, "manifest.json"),
                     {**_public(man), "backup": {"at": time.time(), "repo": repo, "unique_id": uid}})
        names = set(rels)
        for dirpath, _dirs, files in os.walk(os.path.join(d, "pool")):
            for fn in files:
                rel = os.path.relpath(os.path.join(dirpath, fn), d).replace("\\", "/")
                if rel not in names and (fn.endswith(".parquet") or fn.endswith(".part")):
                    try:
                        os.remove(os.path.join(dirpath, fn))
                    except OSError:
                        pass
        status.update(ok=True, files=len(rels), bytes=total, copied=copied,
                      generation=man.get("generation"), encrypted=bool(man.get("encrypted")))
        log.info("repo=%s index copied to %s: %d file(s), %d new, %.0f MB", repo, d, len(rels),
                 copied, total / 1e6)
    except Exception as exc:  # noqa: BLE001 - a failed copy is reported, never fatal
        status.update(ok=False, error=str(exc)[:300])
        log.warning("repo=%s index copy to %s failed: %s", repo, d, exc)
    finally:
        _write_status(repo, status)
    return status


def _status_path() -> str:
    return os.path.join(DATA_DIR, _STATUS)


def _write_status(repo: str, status: dict) -> None:
    data = _read_json(_status_path()) or {}
    data[str(repo)] = status
    try:
        _atomic_json(_status_path(), data)
    except OSError as exc:
        log.warning("could not record the index copy status: %s", exc)


def report(repo: str) -> dict:
    """Kept versions (with the disk space only each one holds) and the copy folder's status."""
    from . import store
    root = store.repo_dir(repo)
    seen = set(_rels(store.read_manifest(repo) if os.path.exists(store.manifest_path(repo)) else None))
    versions = []
    for man in kept(root):
        own = [r for r in _rels(man) if r not in seen]
        seen.update(_rels(man))
        size = 0
        for rel in own:
            try:
                size += os.path.getsize(os.path.join(root, rel))
            except OSError:
                pass
        versions.append({"generation": man.get("generation"),
                         "published_at": (man.get("kept") or {}).get("published_at"),
                         "bytes": size})
    return {"repo": repo, "versions": versions, "copying": copying(repo),
            "copy": (_read_json(_status_path()) or {}).get(str(repo))}
