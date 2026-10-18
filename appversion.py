"""The KopiaLens version and build commit, from one place.

Version: KOPIALENS_RELEASE_VERSION (a release build's tag) > the highest kopialens/v* git tag (a source
checkout) > the VERSION file, which builds write and installed copies, Docker and the web demo read.
Commit: KOPIALENS_COMMIT (builds) > the COMMIT file a build ships > git's HEAD (a source checkout).
Git's own files are read directly; git itself is never run.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
COMPILED = "__compiled__" in globals()
TAG_PREFIX = "kopialens/"
# A pre-release part must be alpha/beta/rc: older tags carry descriptions ("v0.41-before-UI-refactor")
# whose numbers are not versions in this order.
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(-(?:alpha|beta|rc)(?:\.\d+)*)?$")


def normalize(tag: str) -> str | None:
    """"kopialens/v0.36" -> "0.36.0", "v1.1.0-beta.1" -> "1.1.0-beta.1"; None when not a version."""
    name = tag.strip().removeprefix("refs/tags/").removeprefix(TAG_PREFIX)
    m = _SEMVER.match(name)
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    return f"{int(major)}.{int(minor)}.{int(patch or 0)}{pre or ''}"


def order_key(version: str) -> tuple:
    """Semantic Versioning order: 1.0.0-beta.1 < 1.0.0-beta.2 < 1.0.0-rc.1 < 1.0.0."""
    core, _, pre = version.lstrip("vV").split("+")[0].partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in (core.split(".") + ["0", "0", "0"])[:3])
    if not pre:
        return nums, 1, ()
    return nums, 0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _git_dirs() -> tuple[str, str] | None:
    """(this checkout's git folder, the shared one holding refs) for a source checkout; a worktree's
    .git is a file naming its folder, whose commondir names the main one."""
    if COMPILED:
        return None
    folder = HERE
    for _ in range(4):
        dot = os.path.join(folder, ".git")
        if os.path.isdir(dot):
            return dot, dot
        if os.path.isfile(dot):
            line = _read(dot)
            if line.startswith("gitdir:"):
                own = os.path.normpath(os.path.join(folder, line[7:].strip()))
                common = _read(os.path.join(own, "commondir"))
                return own, os.path.normpath(os.path.join(own, common)) if common else own
        folder = os.path.dirname(folder)
    return None


def _refs(common: str, prefix: str) -> dict[str, str]:
    """Refs under ``prefix`` (e.g. refs/tags/kopialens/) -> object id, loose files over packed-refs."""
    out = {}
    for line in _read(os.path.join(common, "packed-refs")).splitlines():
        sha, _, name = line.partition(" ")
        if name.startswith(prefix) and not line.startswith(("#", "^")):
            out[name] = sha
    base = os.path.join(common, *prefix.rstrip("/").split("/"))
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            name = prefix + os.path.relpath(os.path.join(dirpath, f), base).replace(os.sep, "/")
            out[name] = _read(os.path.join(dirpath, f))
    return out


def tag_version() -> str | None:
    """The highest kopialens/v* tag in a source checkout, or None."""
    dirs = _git_dirs()
    if not dirs:
        return None
    found = [v for v in (normalize(n) for n in _refs(dirs[1], "refs/tags/" + TAG_PREFIX)) if v]
    return max(found, key=order_key) if found else None


def version() -> str:
    tagged = normalize(os.environ.get("KOPIALENS_RELEASE_VERSION", "") or "x")
    return tagged or tag_version() or _read(os.path.join(HERE, "VERSION")) or "0.0.0"


def stamp() -> str:
    """Writes version() into the VERSION file when it differs (builds ship that file); returns it."""
    v = version()
    if _read(os.path.join(HERE, "VERSION")) != v:
        with open(os.path.join(HERE, "VERSION"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(v + "\n")
    return v


def commit() -> str | None:
    """The short commit this copy was built from, or None when unknown."""
    sha = os.environ.get("KOPIALENS_COMMIT", "").strip() or _read(os.path.join(HERE, "COMMIT"))
    if not sha:
        dirs = _git_dirs()
        if dirs:
            head = _read(os.path.join(dirs[0], "HEAD"))
            if head.startswith("ref:"):
                ref = head[4:].strip()
                sha = _read(os.path.join(dirs[1], *ref.split("/"))) or _refs(dirs[1], ref).get(ref, "")
            else:
                sha = head
    return sha[:7] if re.fullmatch(r"[0-9a-f]{7,40}", sha or "") else None
