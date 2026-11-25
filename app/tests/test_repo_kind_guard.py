"""No kopia command may be built for a repo that is not a kopia repo.

The Backblaze repo (`bz`) is a read-only local SQLite index and carries NO kopia CLI args. That is
what makes this dangerous rather than merely wrong: `repo="bz"` does not fail, it produces a
perfectly valid command against the DEFAULT kopia repository. On `snapshot delete` or `restore`,
that is the wrong repository.

The first attempt at this guarded only `_base_command` and looked complete. It was not: NINE
builders concatenated `REPOS[repo]["args"]` themselves, and they were every destructive path.
Hence the source-level invariant below - it catches the next builder someone adds, which is the
actual failure mode. Testing only the nine known ones would have passed on the broken version too.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from app.core import kopia

_APP = pathlib.Path(kopia.__file__).resolve().parents[1]
# Any subscript access to an "args" key, so the loop form (`for repo, cfg in REPOS.items(): cfg["args"]`)
# is caught as well as the direct one. The old pattern only matched `REPOS[repo]["args"]`, and only
# inside core/kopia.py - api/excluded.py built a bare Backblaze command in the loop form and was
# invisible to both (found 2026-09-07).
_ARGS_READ = re.compile(r"""\[\s*['"]args['"]\s*\]""")


def test_no_module_reads_raw_repo_args():
    """THE test, package-wide. Only _repo_args may touch a repo record's raw CLI args."""
    offenders = []
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP).as_posix()
        if rel.startswith("tests/") or rel == "core/kopia.py":
            continue
        for num, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _ARGS_READ.search(line):
                offenders.append(f"{rel}:{num}")
    assert not offenders, (
        f"these read a repo record's raw args directly: {offenders}. "
        "Route them through core.kopia._repo_args(repo), which rejects a non-kopia repo."
    )


def test_no_builder_bypasses_the_guard():
    """The same invariant inside core/kopia.py, where the one permitted use lives."""
    src = inspect.getsource(kopia)
    uses = [m for m in re.finditer(r'REPOS\[[^\]]+\]\["args"\]', src)]
    # Exactly one legitimate use: inside _repo_args itself, after validation.
    assert len(uses) == 1, (
        f"{len(uses)} places read REPOS[...]['args'] directly; only _repo_args may. "
        "Route the new builder through _repo_args(repo)."
    )
    guard = inspect.getsource(kopia._repo_args)
    assert 'REPOS[repo]["args"]' in guard, "the one permitted use should be the guard's own"


def test_repo_args_rejects_a_non_kopia_repo():
    with pytest.raises(ValueError, match="not a kopia repo"):
        kopia._repo_args("bz")


def test_repo_args_rejects_an_unknown_repo():
    with pytest.raises(ValueError, match="unknown repo"):
        kopia._repo_args("nope")


def test_repo_args_allows_a_real_kopia_repo():
    assert isinstance(kopia._repo_args("1"), list)


# The destructive builders, called directly - belt and braces alongside the source invariant.
DESTRUCTIVE = [
    ("snapshot_delete_commands", lambda: kopia.snapshot_delete_commands("bz", ["abc"])),
    ("remove_files_command", lambda: kopia.remove_files_command("bz", object_ids=["abc"])),
    ("remove_files_chunked_commands", lambda: kopia.remove_files_chunked_commands("bz", ["abc"])),
    ("restore_command_full", lambda: kopia.restore_command_full("bz", "abc", "C:/tmp")),
    ("maintenance_run_command", lambda: kopia.maintenance_run_command("bz")),
    ("snapshot_pin_command", lambda: kopia.snapshot_pin_command("bz", ["abc"])),
    ("snapshot_diff_command", lambda: kopia.snapshot_diff_command("bz", "a", "b")),
    ("policy_set_command", lambda: kopia.policy_set_command("bz", target="u@h:p", add_ignore=["x"])),
    ("policy_delete_command", lambda: kopia.policy_delete_command("bz", target="u@h:p")),
    ("policy_import_command", lambda: kopia.policy_import_command("bz", "b.json")),
]


@pytest.mark.parametrize("name,call", DESTRUCTIVE, ids=[n for n, _ in DESTRUCTIVE])
def test_destructive_builders_refuse_a_non_kopia_repo(name, call):
    with pytest.raises(ValueError):
        call()
