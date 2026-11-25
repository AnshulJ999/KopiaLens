"""The kopia binary: one resolver, and no builder may hardcode a name.

The failure this pins: nothing ever checked which kopia ran. A KopiaUI user HAS kopia but not on
PATH, and every command started with the bare string ``kopia`` - so the app either failed with a
raw WinError or silently ran whatever PATH happened to hold.
"""

from __future__ import annotations

import pathlib
import re
import sys

from app.core import kopiabin, kopia

_APP = pathlib.Path(kopia.__file__).resolve().parents[1]
# A command list that STARTS with the literal binary name ("kopia" as a repo KIND is a different
# string entirely and stays allowed).
_BARE_PREFIX = re.compile(r"""\[\s*['"]kopia['"]\s*[,\]]""")


def test_no_module_builds_a_bare_kopia_command():
    """THE invariant, package-wide: only core/kopiabin.py may name the binary."""
    offenders = []
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP).as_posix()
        if rel.startswith("tests/") or rel == "core/kopiabin.py":
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _BARE_PREFIX.search(line):
                offenders.append(f"{rel}:{num}")
    assert not offenders, (
        f"these build a command starting with the bare name 'kopia': {offenders}. "
        "Use core.kopia._kopia_exe() / core.kopiabin.exe()."
    )


def _stub(tmp_path, name: str):
    path = tmp_path / name
    path.write_text("stub", encoding="utf-8")
    path.chmod(0o755)   # POSIX: the resolver takes executables only
    return path


def _isolate(monkeypatch, tmp_path, *, env=None, setting=None, which=None,
             locations=(), kopiaui=(), bundled=()):
    monkeypatch.setattr(kopiabin, "explicit_setting", lambda: setting or "")
    monkeypatch.setattr(kopiabin, "_usual_locations", lambda: list(locations))
    monkeypatch.setattr(kopiabin, "_kopiaui_bundles", lambda: list(kopiaui))
    monkeypatch.setattr(kopiabin, "_bundled_copies", lambda: list(bundled))
    monkeypatch.setattr(kopiabin.shutil, "which", lambda _name: which)
    if env:
        monkeypatch.setenv(kopiabin.ENV_OVERRIDE, env)
    else:
        monkeypatch.delenv(kopiabin.ENV_OVERRIDE, raising=False)
    kopiabin.invalidate()


def test_resolution_order(monkeypatch, tmp_path):
    """env -> setting -> BUNDLED -> PATH -> install locations -> KopiaUI bundle.

    The bundle comes before the machine's copies on purpose: a release pins the kopia it was tested
    with, so "works out of the box" does not depend on what a user happens to have installed."""
    ui = _stub(tmp_path, "ui-kopia")
    _isolate(monkeypatch, tmp_path, kopiaui=[str(ui)])
    assert kopiabin.resolve() == str(ui)
    assert kopiabin.source() == "KopiaUI"

    loc = _stub(tmp_path, "loc-kopia")
    _isolate(monkeypatch, tmp_path, kopiaui=[str(ui)], locations=[str(loc)])
    assert kopiabin.resolve() == str(loc)

    on_path = _stub(tmp_path, "path-kopia")
    _isolate(monkeypatch, tmp_path, which=str(on_path), locations=[str(loc)], kopiaui=[str(ui)])
    assert kopiabin.resolve() == str(on_path)
    assert kopiabin.source() == "PATH"

    bundled = _stub(tmp_path, "bundled-kopia")
    _isolate(monkeypatch, tmp_path, which=str(on_path), locations=[str(loc)], kopiaui=[str(ui)],
             bundled=[str(bundled)])
    assert kopiabin.resolve() == str(bundled)
    assert kopiabin.source() == "bundled"
    assert kopiabin.info(with_version=False)["bundled"] is True

    chosen = _stub(tmp_path, "set-kopia")
    _isolate(monkeypatch, tmp_path, setting=str(chosen), which=str(on_path), bundled=[str(bundled)])
    assert kopiabin.resolve() == str(chosen)
    assert kopiabin.source() == "setting"

    forced = _stub(tmp_path, "env-kopia")
    _isolate(monkeypatch, tmp_path, env=str(forced), setting=str(chosen), which=str(on_path),
             bundled=[str(bundled)])
    assert kopiabin.resolve() == str(forced)
    assert kopiabin.source() == "environment"


def test_inventory_lists_every_copy_with_its_version(monkeypatch, tmp_path):
    """The data for the Settings row and the one-time "a different kopia was found" notice: every
    copy present, with version, which one is in use, and which is newest."""
    bundled = _stub(tmp_path, "bundled-kopia")
    other = _stub(tmp_path, "other-kopia")
    _isolate(monkeypatch, tmp_path, which=str(other), bundled=[str(bundled)])

    class _Res:
        def __init__(self, out):
            self.returncode = 0
            self.stdout = out
            self.stderr = ""

    monkeypatch.setattr(kopiabin.subprocess, "run",
                        lambda cmd, **kw: _Res("0.24.1 build: abc from: kopia/kopia"
                                               if cmd[0] == str(other) else
                                               "0.23.1 build: def from: kopia/kopia"))
    found = kopiabin.inventory()
    assert len(found) == 2
    by_path = {f["path"]: f for f in found}
    assert by_path[str(bundled)]["in_use"] is True and by_path[str(bundled)]["version"] == "0.23.1"
    assert by_path[str(other)]["in_use"] is False and by_path[str(other)]["newest"] is True


def test_a_candidate_that_does_not_exist_is_skipped(monkeypatch, tmp_path):
    real = _stub(tmp_path, "real-kopia")
    _isolate(monkeypatch, tmp_path, setting=str(tmp_path / "gone-kopia"),
             kopiaui=[str(real)])
    assert kopiabin.resolve() == str(real)


def test_exe_falls_back_to_the_bare_name(monkeypatch, tmp_path):
    """With nothing found the command still reads `kopia` - the failure stays where it was, now
    with a message instead of a raw OSError."""
    _isolate(monkeypatch, tmp_path)
    assert kopiabin.resolve() is None
    assert kopiabin.exe() == "kopia"
    info = kopiabin.info(with_version=False)
    assert info["available"] is False and info["detail"]


def test_check_validates_one_path(monkeypatch, tmp_path):
    assert kopiabin.check("")["ok"] is False
    assert kopiabin.check(str(tmp_path / "nope"))["ok"] is False
    # a real, runnable program that does NOT report a version is refused (python prints "Python ..")
    assert kopiabin.check(sys.executable)["ok"] is False

    fake = _stub(tmp_path, "kopia-copy")

    class _Res:
        returncode = 0
        stdout = "0.23.1 build: 72ec08fd8edb86c67ed27099bf1b955e1f308ffa from: kopia/kopia"
        stderr = ""

    monkeypatch.setattr(kopiabin.subprocess, "run", lambda *a, **k: _Res())
    ok = kopiabin.check(str(fake))
    assert ok["ok"] is True and ok["version"] == "0.23.1"


def test_base_command_uses_the_resolved_binary(monkeypatch):
    fake = r"C:\Fake Kopia\kopia.exe"
    monkeypatch.setattr(kopiabin, "resolve", lambda force=False: fake)
    assert kopia._base_command("1", ["snapshot", "list"])[0] == fake
    assert kopia._kopia_exe() == fake


def test_probe_and_connect_builders_use_the_resolved_binary(monkeypatch):
    fake = "/opt/kopia/bin/kopia"
    monkeypatch.setattr(kopiabin, "resolve", lambda force=False: fake)
    from app.core import repoconnect
    args = repoconnect.build_connect_args("filesystem", {"path": "/backups"}, "/tmp/r.config", "pw")
    assert args[0] == fake
    assert args[:6] == [fake, "--config-file", "/tmp/r.config", "repository", "connect",
                        "filesystem"]


def test_missing_binary_fails_with_a_readable_message(monkeypatch):
    monkeypatch.setattr(kopiabin, "resolve", lambda force=False: r"C:\definitely\not\here\kopia.exe")
    res = kopia.kopia("1", ["snapshot", "list"])
    assert res.returncode == 1
    assert "kopia was not found" in res.stderr
