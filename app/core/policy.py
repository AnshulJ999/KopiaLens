"""Read + cache each source's kopia policy (retention, schedule, ignore rules, compression).

``compute_next_snapshots`` already ran ``policy show`` for the scheduling block and threw the
rest away; this module keeps the whole policy so the UI can answer "what backs this folder up,
how often, how long is it kept, and is it excluded". One subprocess per source, cached to disk.

Read-only: ``policy show`` never mutates. Policy EDITS are not done here (command-generation only,
elsewhere) - this module never writes to kopia.
"""

from __future__ import annotations

import json
import os
import threading
import time

from config import CACHEDIR

from .applog import get_logger
from .kopia import kopia

log = get_logger("policy")

POLICY_TTL = 24 * 3600          # per-source policies are refreshed nightly; this is the read-side floor
_PATH_TTL = 900.0               # effective-policy-for-a-folder lookups (one subprocess each)
_PATH_CACHE: dict[tuple, tuple[float, dict]] = {}
_PATH_CACHE_MAX = 300
_PATH_LOCK = threading.Lock()


def _cache_path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"policies_repo{repo}.json")


def load_policies(repo: str) -> dict:
    """Cached ``{"ts": float, "policies": {source_key: raw_policy}}``; empty dict when never built."""
    try:
        with open(_cache_path(repo), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_policies(repo: str, policies: dict) -> None:
    os.makedirs(CACHEDIR, exist_ok=True)
    tmp = _cache_path(repo) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "policies": policies}, fh)
        os.replace(tmp, _cache_path(repo))
    except Exception as exc:  # noqa: BLE001 - a cache write must never fail the caller
        log.warning("repo=%s policy cache write failed: %s", repo, exc)


def show_policy(repo: str, target: str) -> dict:
    """One ``kopia policy show <target> --json``. ``target`` is a source key (``user@host:path``)
    or the same user@host with any absolute path - kopia resolves the EFFECTIVE (inherited) policy
    for a path that has no policy of its own."""
    res = kopia(repo, ["policy", "show", target, "--json"])
    out = (res.stdout or "").strip()
    return json.loads(out) if out else {}


def refresh_policies(repo: str, force: bool = False) -> dict:
    """Re-read every source's policy (one subprocess each) and cache it. Skips entirely when the
    cache is younger than POLICY_TTL unless ``force``. Runs in the nightly worker."""
    from .sources import sources_index  # local import: avoids a circular dependency

    cached = load_policies(repo)
    if not force and cached.get("policies") and time.time() - float(cached.get("ts") or 0) < POLICY_TTL:
        return cached["policies"]
    # Start from what we already know and MERGE. Building a fresh dict would mean one transient
    # subprocess failure silently erased that source's policy for the next 24 hours - the panel
    # would then show "policy not loaded" for a source that has a perfectly good policy.
    policies: dict = dict(cached.get("policies") or {})
    live = set(sources_index(repo))
    failed = 0
    for key in sorted(live):
        try:
            policies[key] = show_policy(repo, key)
        except Exception as exc:  # noqa: BLE001 - one bad source must not lose the rest
            failed += 1
            log.warning("policy show failed for %s (keeping previous): %s", key, exc)
    for gone in [k for k in policies if k not in live]:
        policies.pop(gone)      # source no longer exists; do not keep its policy forever
    if policies:
        _write_policies(repo, policies)
    (log.warning if failed else log.debug)("policies repo=%s: %d known, %d failed this pass",
                                           repo, len(policies), failed)
    return policies


def _user_host(source_key: str) -> str:
    """A source key like ``Anshul@anshul-desktop:C:`` + drive root -> ``Anshul@anshul-desktop``.
    The path itself contains a
    colon on Windows, so split on the FIRST one only (user@host never contains a colon)."""
    return source_key.split(":", 1)[0]


def effective_for_path(repo: str, source_key: str, path: str) -> dict:
    """Effective policy for an arbitrary folder under a source (kopia resolves inheritance).
    One subprocess, cached ~15 min - the folder detail panel calls this per folder opened."""
    target = f"{_user_host(source_key)}:{path}"
    ck = (repo, target.lower())
    now = time.time()
    with _PATH_LOCK:
        hit = _PATH_CACHE.get(ck)
        if hit is not None and now - hit[0] < _PATH_TTL:
            return hit[1]
    try:
        pol = show_policy(repo, target)
    except Exception as exc:  # noqa: BLE001
        log.warning("effective policy failed for %s: %s", target, exc)
        pol = {}
    with _PATH_LOCK:
        if len(_PATH_CACHE) >= _PATH_CACHE_MAX:
            for stale in sorted(_PATH_CACHE, key=lambda k: _PATH_CACHE[k][0])[:_PATH_CACHE_MAX // 4]:
                _PATH_CACHE.pop(stale, None)
        _PATH_CACHE[ck] = (now, pol)
    return pol


def _fmt_interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        n = seconds // 86400
        return f"every {n}d" if n > 1 else "daily"
    if seconds % 3600 == 0:
        return f"every {seconds // 3600}h"
    if seconds % 60 == 0:
        return f"every {seconds // 60}m"
    return f"every {seconds}s"


def describe_schedule(sched: dict) -> str:
    """Human one-liner for a scheduling policy block, e.g. ``every 6h + 22:00``."""
    if not sched:
        return "inherited"
    if sched.get("manual"):
        return "manual only"
    parts: list[str] = []
    iv = sched.get("intervalSeconds")
    if iv:
        parts.append(_fmt_interval(int(iv)))
    for tod in sched.get("timeOfDay") or []:
        parts.append(f"{int(tod.get('hour', 0)):02d}:{int(tod.get('min', 0)):02d}")
    for expr in sched.get("cron") or []:
        clean = str(expr).split("#", 1)[0].strip()
        if clean:
            parts.append(f"cron {clean}")
    return " + ".join(parts) if parts else "no schedule"


_RETENTION_LABELS = (
    ("keepLatest", "latest"), ("keepHourly", "h"), ("keepDaily", "d"),
    ("keepWeekly", "w"), ("keepMonthly", "m"), ("keepAnnual", "y"),
)


def describe_retention(ret: dict) -> str:
    """``200 latest / 360h / 360d / 120w / 48m / 5y`` - the six fixed kopia count buckets."""
    if not ret:
        return "inherited"
    parts = []
    for field, label in _RETENTION_LABELS:
        val = ret.get(field)
        if val:
            parts.append(f"{int(val)} {label}" if label == "latest" else f"{int(val)}{label}")
    return " / ".join(parts) if parts else "no retention"


def summarize(pol: dict) -> dict:
    """Compact, UI-ready view of a raw policy."""
    files = pol.get("files") or {}
    return {
        "schedule": describe_schedule(pol.get("scheduling") or {}),
        "retention": describe_retention(pol.get("retention") or {}),
        "ignore": list(files.get("ignore") or []),
        "ignore_dot_files": list(files.get("ignoreDotFiles") or []),
        "ignore_cache_dirs": bool(files.get("ignoreCacheDirs")),
        "max_file_size": files.get("maxFileSize"),
        "compression": ((pol.get("compression") or {}).get("compressorName") or "none"),
        "ignore_identical": bool((pol.get("retention") or {}).get("ignoreIdenticalSnapshots")),
    }


def source_summaries(repo: str) -> dict[str, dict]:
    """``{source_key: summarize(policy)}`` from cache; refreshes when the cache is stale/missing."""
    cached = load_policies(repo)
    policies = cached.get("policies") or {}
    if not policies or time.time() - float(cached.get("ts") or 0) >= POLICY_TTL:
        policies = refresh_policies(repo) or policies
    return {key: summarize(pol) for key, pol in policies.items()}


# ---- defined policies (the export view) ------------------------------------------------
# The per-source cache above holds EFFECTIVE policies (`policy show` - what kopia applies,
# inherited values filled in). The Policies tab also needs the DEFINED policies - which target
# sets which field itself. One `policy export` returns all of them (global included) as
# {target: policy}; a target that defines nothing at all is absent from it.

_GLOBAL_TARGET = "(global)"
_DEFINED_TTL = 24 * 3600


def _defined_cache_path(repo: str) -> str:
    return os.path.join(CACHEDIR, f"policies_defined_repo{repo}.json")


def load_defined_policies(repo: str) -> dict:
    """Cached ``{"ts": float, "policies": {target: raw_policy}}`` from the last successful
    export; empty dict when never built. Targets are source keys or ``(global)``."""
    try:
        with open(_defined_cache_path(repo), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def refresh_defined_policies(repo: str, force: bool = False) -> tuple[dict, bool]:
    """(defined policies, fresh) - re-exports every defined policy when the cache is older than
    ``_DEFINED_TTL``. A FAILED export keeps serving the previous cache and reports ``fresh=False``
    - stale definitions beat no definitions (the tab marks itself stale instead of going blank)."""
    import tempfile

    cached = load_defined_policies(repo)
    if not force and cached.get("policies") and \
            time.time() - float(cached.get("ts") or 0) < _DEFINED_TTL:
        return cached, True
    os.makedirs(CACHEDIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=CACHEDIR)
    os.close(fd)
    try:
        res = kopia(repo, ["policy", "export", "--to-file", tmp, "--overwrite"])
        if res.returncode != 0:
            log.warning("policy export failed repo=%s rc=%d: %s", repo, res.returncode,
                        (res.stderr or "").strip()[:300])
            return cached, False
        with open(tmp, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("policy export produced %s, expected an object" % type(data).__name__)
        _write_defined(repo, data)
        return {"ts": time.time(), "policies": data}, True
    except Exception as exc:  # noqa: BLE001 - a failed export must not blank the tab
        log.warning("policy export/verify failed repo=%s (keeping previous): %s", repo, exc)
        return cached, False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _write_defined(repo: str, policies: dict) -> None:
    os.makedirs(CACHEDIR, exist_ok=True)
    tmp = _defined_cache_path(repo) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "policies": policies}, fh)
        os.replace(tmp, _defined_cache_path(repo))
    except Exception as exc:  # noqa: BLE001 - a cache write must never fail the caller
        log.warning("repo=%s defined-policy cache write failed: %s", repo, exc)


def refresh_one_policy(repo: str, target: str) -> None:
    """Re-read ONE source's effective policy into the cache after a write (a full
    refresh_policies would spend one subprocess per source for a one-target change)."""
    cached = load_policies(repo)
    policies = dict(cached.get("policies") or {})
    try:
        policies[target] = show_policy(repo, target)
    except Exception as exc:  # noqa: BLE001 - keep the old value, the next nightly fixes it
        log.warning("policy refresh failed for %s (keeping previous): %s", target, exc)
        return
    _write_policies(repo, policies)


def invalidate_after_write(repo: str, targets: list[str] | None = None,
                           ignore_only: bool = False) -> None:
    """Post-write cache refresh: the effective policies of every source the write actually
    reaches, plus the defined-policies export and the pricing cache.

    ``targets`` = the policy targets the write touched. A write reaches a source when the source
    IS a written target, or its rules are owned by one (an ancestor policy), or the global
    policy was written and the source inherits it. Origins are resolved against the NEW export -
    the write may have changed who owns whom (a Global write turns a no-rules source into a
    global-inheriting one). With no targets (a mirror restore) everything is refreshed.

    ``ignore_only`` (, add/remove-ignore writes only): the effective policies of reached
    sources change in exactly one field - ``files.ignore`` - and the NEW export already carries
    every defined rule, so the cache is patched from the export with ZERO extra subprocesses.
    A Global write that used to fire dozens of ``policy show`` calls inside one save request now
    fires one export. Anything else (whole-policy deletion, export failure, a source with no
    cached effective policy) falls back to the per-source refresh: correctness first, and kopia's
    other ten field groups are never re-merged here on purpose.
    """
    from ..index import policypricing  # local import: index layer, keeps policy import-light
    from .sources import sources_index

    defined, export_fresh = refresh_defined_policies(repo, force=True)  # FIRST: ownership from the new state
    if targets is None:
        refresh_policies(repo, force=True)
    elif not (ignore_only and export_fresh) or not _patch_ignore_only(repo, targets, defined):
        written = set(targets)
        defined_policies = load_defined_policies(repo).get("policies") or {}
        for key in sources_index(repo):
            origin, owner = effective_ignore_origin(repo, key, defined_policies)
            # reached when the source IS a written target, or the written target owns its rules
            # (an ancestor policy or - via owner == (global) - the global policy)
            if key in written or (owner is not None and owner in written):
                refresh_one_policy(repo, key)
    policypricing.invalidate(repo)


def _patch_ignore_only(repo: str, targets: list[str], defined: dict) -> bool:
    """ Fast path: rewrite ``files.ignore`` on the cached EFFECTIVE policies of every
    source the write reached, computed from the fresh export with the same ownership walk the
    per-source fallback uses. Returns False (caller falls back) when the cache is missing an
    entry or the patch fails - a silently half-updated cache would feed pricing and targeting
    the wrong rules."""
    from .sources import sources_index

    try:
        cached = load_policies(repo)
        policies = dict(cached.get("policies") or {})
        pols = defined.get("policies") or {}   # the cache wrapper -> the bare {target: policy} dict
        global_rules = list(((pols.get(_GLOBAL_TARGET) or {}).get("files") or {}).get("ignore") or [])
        for key in sources_index(repo):
            origin, owner = effective_ignore_origin(repo, key, pols)
            if key not in targets and not (owner is not None and owner in targets):
                continue
            if origin == "own":
                rules = list(((pols.get(key) or {}).get("files") or {}).get("ignore") or [])
            elif origin == "inherited":
                rules = list(((pols.get(owner or "") or {}).get("files") or {}).get("ignore") or [])
            elif origin == "global":
                rules = global_rules
            else:
                rules = []
            pol = policies.get(key)
            if pol is None:
                return False  # never cached - let the per-source refresh fetch it properly
            files = dict(pol.get("files") or {})
            if files.get("ignore") != rules:
                files["ignore"] = rules
                pol = {**pol, "files": files}
                policies[key] = pol
        _write_policies(repo, policies)
        return True
    except Exception as exc:  # noqa: BLE001 - fall back, never fail the save
        log.warning("repo=%s ignore-only cache patch failed (falling back): %s", repo, exc)
        return False


def global_ignores(repo: str, defined: dict | None = None) -> list[str]:
    """The global policy's own ignore rules ([] when the global policy defines none)."""
    defined = defined if defined is not None else load_defined_policies(repo).get("policies") or {}
    pol = defined.get(_GLOBAL_TARGET) or {}
    return list((pol.get("files") or {}).get("ignore") or [])


def effective_ignore_origin(repo: str, source_key: str, defined: dict | None = None) -> tuple[str, str | None]:
    """Which policy's ignore rules actually apply to ``source_key``: (origin, owner_target).
    kopia resolves per-field from the nearest policy on the path chain that DEFINES rules -
    a source's own list REPLACES the global one, it never concatenates (verified in kopia's
    policy_merge.go mergeStringList and against this install's data)."""
    defined = defined if defined is not None else load_defined_policies(repo).get("policies") or {}

    def _rules(target: str) -> list[str] | None:
        pol = defined.get(target)
        if pol is None:
            return None
        val = (pol.get("files") or {}).get("ignore")
        return list(val) if val else None

    own = _rules(source_key)
    if own:
        return "own", source_key
    # Walk the path ancestors (same user@host), longest first - kopia's path-chain inheritance.
    # A source key's path part carries the SOURCE machine's separator; detect it from the text.
    # The TOP of the walk must be spelled the way kopia spells policy targets: a Windows drive
    # root is "G:\" (never bare "G:" - that key matches nothing and made drive-root-owned
    # sources read as global-inherited), and the POSIX root is "/".
    user_host, sep, path = source_key.partition(":")
    if sep and path:
        sep_char = "\\" if "\\" in path else "/"
        prefixes: list[str] = []
        cur = path.rstrip(sep_char)
        if sep_char in cur:
            while True:
                parent = cur.rsplit(sep_char, 1)[0] if sep_char in cur else ""
                if not parent or parent == sep_char:
                    break
                if not (len(parent) == 2 and parent[1] == ":"):  # bare "G:" is never a target
                    prefixes.append(f"{user_host}:{parent}")
                cur = parent
            if sep_char == "\\":
                body = path.rstrip(sep_char)
                if len(body) >= 2 and body[1] == ":":
                    prefixes.append(f"{user_host}:{body[:2]}{sep_char}")
            else:
                prefixes.append(f"{user_host}:/")
        for prefix in sorted(set(prefixes), key=len, reverse=True):
            got = _rules(prefix)
            if got:
                return "inherited", prefix
    if global_ignores(repo, defined):
        return "global", _GLOBAL_TARGET
    return "none", None
