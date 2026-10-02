"""Simulated kopia runs for demo repos.

Every action a demo repo starts (delete, remove files, restore, maintenance, pin, snapshot,
policy writes) and every dry-run preview gets believable kopia-format output built from the
demo's own index. Nothing is changed anywhere; the last line says so.
"""

from __future__ import annotations

import time

from .applog import get_logger

log = get_logger("demosim")

DONE_NOTE = "Demo: nothing was changed."
_STREAM_SECONDS = 4.0
_TICK = 0.05


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000
    return f"{n:.1f} TB"


def _flag_values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def _positional(args: list[str], skip: int) -> list[str]:
    out, i = [], skip
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            i += 1 if "=" in a or a in ("--delete", "--commit", "--full", "--add", "--remove") else 2
            continue
        out.append(a)
        i += 1
    return out


class _Index:
    """Read-only lookups on the demo generation."""

    def __init__(self, repo: str):
        from ..index import store
        self.gen = store.current(repo)
        self.cur = self.gen.cursor()

    def close(self) -> None:
        self.gen.release()

    def snapshots(self, ids: list[str]) -> list[tuple]:
        if not ids:
            return []
        return self.cur.execute(
            "SELECT snapshot_id, source, start_time FROM isnaps WHERE list_contains(?, snapshot_id) "
            "ORDER BY start_time", [ids]).fetchall()

    def file_hits(self, oids: list[str], names: list[str], sources: list[str]) -> list[tuple]:
        """(snapshot_id, source, start_time, full_path) for every snapshot holding a target."""
        cids = [o[1:] if o[:1] in ("I", "Z") else o for o in oids]
        return self.cur.execute("""
            SELECT s.snapshot_id, sr.source, s.start_time, p.full_path FROM filev f
            JOIN pathx p ON p.path_id = f.path_id AND p.src_id = f.src_id
            JOIN snap s ON s.snap_id = f.snap_id JOIN src sr ON sr.src_id = f.src_id
            LEFT JOIN cid c ON c.cid_id = f.cid_id
            WHERE (list_contains(?, c.content_id) OR list_contains(?, p.name))
              AND (len(?) = 0 OR list_contains(?, sr.source))
            ORDER BY s.start_time, p.full_path""", [cids, names, sources, sources]).fetchall()

    def object_summary(self, oid: str) -> tuple[int, int, int]:
        """(files, folders, bytes) under a restore object; zeros when it is not in the index."""
        obj = oid.split("/", 1)[0]
        root = self.cur.execute("""
            SELECT count(*), coalesce(sum(f.size), 0) FROM isnaps i
            JOIN snap s ON s.snapshot_id = i.snapshot_id JOIN filev f ON f.snap_id = s.snap_id
            WHERE i.root_obj = ? AND i.snapshot_id = (SELECT snapshot_id FROM isnaps
                                                      WHERE root_obj = ? LIMIT 1)""",
                                [obj, obj]).fetchone()
        if root and root[0]:
            return int(root[0]), 0, int(root[1])
        bare = obj[1:] if obj[:1] in ("I", "k", "Z") else obj
        row = self.cur.execute("""
            SELECT t.etype, t.size, t.s_files, t.s_dirs, t.s_size FROM tent t
            JOIN cid c ON c.cid_id = coalesce(t.ind_cid, t.cid_id)
            WHERE c.content_id IN (?, ?) LIMIT 1""", [obj, bare]).fetchone()
        if not row:
            return 0, 0, 0
        etype, size, files, dirs, s_size = row
        if etype == 1:
            return int(files or 0), int(dirs or 0), int(s_size or 0)
        return 1, 0, int(size or 0)


def _lines(repo: str, args: list[str]) -> tuple[list[tuple[str, str]], int]:
    """(stream, line) pairs and the exit code for one kopia command."""
    out: list[tuple[str, str]] = []

    def say(text: str, stream: str = "stdout") -> None:
        out.append((stream, text))

    head = args[:3]
    idx = _Index(repo)
    try:
        if head[:2] == ["snapshot", "delete"]:
            ids = _positional(args, 2)
            really = "--delete" in args
            for sid, source, start in idx.snapshots(ids):
                verb = "Deleting" if really else "Would delete"
                say(f"{verb} snapshot {sid} of {source} at {start:%Y-%m-%d %H:%M:%S}")
            if not really:
                say("Pass --delete to actually delete the snapshots.")
        elif head == ["snapshot", "fix", "remove-files"]:
            commit = "--commit" in args
            hits = idx.file_hits(_flag_values(args, "--object-id"), _flag_values(args, "--filename"),
                                 _flag_values(args, "--source"))
            snaps: dict[str, tuple] = {}
            for sid, source, start, path in hits:
                if sid not in snaps:
                    snaps[sid] = (source, start)
                    say(f"Processing snapshot {source} at {start:%Y-%m-%d %H:%M:%S}", "stderr")
                say(f"  will remove file {path}", "stderr")
            n = len(snaps)
            if commit:
                say(f"Fixed and committed {n} snapshots.", "stderr")
            else:
                say(f"Fixed {n} snapshots, but snapshot manifests were not updated. "
                    "Pass --commit to update snapshots.", "stderr")
        elif head[:2] == ["maintenance", "run"]:
            mode = "full" if "--full" in args else "quick"
            for step in (f"Running {mode} maintenance...", "Looking for active contents...",
                         "Looking for unreferenced contents...", "Rewriting contents from short packs...",
                         "Looking for unreferenced blobs...", "Compacting indexes...",
                         f"Finished {mode} maintenance."):
                say(step, "stderr")
        elif head[:1] == ["restore"]:
            pos = _positional(args, 1)
            target = pos[1] if len(pos) > 1 else ""
            files, dirs, size = idx.object_summary(pos[0] if pos else "")
            say(f"Restoring to local filesystem ({target}) with parallelism=8...", "stderr")
            for i in range(1, 5):
                say(f"Processed {files * i // 4} ({_fmt_bytes(size * i / 4)}) of {files} "
                    f"({_fmt_bytes(size)}).", "stderr")
            say(f"Restored {files} files, {dirs} directories and 0 symbolic links "
                f"({_fmt_bytes(size)}).", "stderr")
        elif head[:2] == ["snapshot", "pin"]:
            ids = _positional(args, 2)
            ids = ids[:-1] if ids else ids          # the last positional is the pin name
            word = "Removed pin from" if "--remove" in args else "Pinned"
            for sid, source, _start in idx.snapshots(ids):
                say(f"{word} snapshot {sid} of {source}")
        elif head[:2] == ["snapshot", "create"]:
            path = (_positional(args, 2) or [""])[0]
            say(f"Snapshotting {path} ...", "stderr")
            say(" * 0 hashing, 0 hashed (0 B), 0 cached (0 B), uploaded 0 B, estimating...", "stderr")
            say("Created snapshot.", "stderr")
        elif head[:1] == ["policy"]:
            say(f"Policy {head[1] if len(head) > 1 else 'write'}: done.")
        else:
            say(f"kopia {' '.join(head)}: done.")
    finally:
        idx.close()
    say(DONE_NOTE, "stderr")
    return out, 0


def run(repo: str, args: list[str]) -> tuple[int, str, str]:
    """A synchronous simulated run: (returncode, stdout, stderr)."""
    lines, rc = _lines(repo, args)
    log.info("demo simulated: kopia %s", " ".join(args[:3]))
    return (rc, "\n".join(t for s, t in lines if s == "stdout"),
            "\n".join(t for s, t in lines if s == "stderr"))


def stream(repo: str, args: list[str], result: dict):
    """The job runner's form: ``(stream, line)`` pairs paced over a few seconds."""
    lines, rc = _lines(repo, args)
    log.info("demo simulated job: kopia %s", " ".join(args[:3]))
    pause = min(_TICK * 4, _STREAM_SECONDS / max(1, len(lines)))
    for item in lines:
        yield item
        time.sleep(pause)
    result["returncode"] = rc
