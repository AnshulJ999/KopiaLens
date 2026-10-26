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
        # The index keeps big files' ids with their "I" prefix; other forms come without one.
        cids = list(oids) + [o[1:] for o in oids if o[:1] in ("I", "Z")]
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

    def under(self, path: str) -> tuple[int, int]:
        """(files, bytes) in the latest snapshots at or under ``path``."""
        low = path.lower().rstrip("\\/")
        return self.cur.execute("""
            SELECT count(*), coalesce(sum(size), 0)::BIGINT FROM pathx WHERE in_latest
              AND (lower(full_path) = ? OR starts_with(lower(full_path), ? || '\\')
                   OR starts_with(lower(full_path), ? || '/'))""", [low, low, low]).fetchone()

    def storage(self) -> tuple[int, int]:
        """(stored bytes, snapshots) of the demo repo."""
        try:
            packed = self.cur.execute("SELECT coalesce(sum(packed), 0) FROM csize").fetchone()[0]
        except Exception:  # noqa: BLE001 - a copy without the size map: the files' own sizes
            packed = self.cur.execute("SELECT coalesce(sum(size), 0) FROM pathx").fetchone()[0]
        snaps = self.cur.execute("SELECT count(*) FROM snap").fetchone()[0]
        return int(packed), int(snaps)


def path_known(repo: str, path: str) -> bool:
    """A path a demo can snapshot: one of its sources, or inside one."""
    from .paths import source_root
    from .sources import sources_index
    low = path.lower().rstrip("\\/")
    for src in sources_index(repo):
        root = source_root(src).lower().rstrip("\\/")
        if low == root or low.startswith(root + "\\") or low.startswith(root + "/"):
            return True
    return False


def _kopia_bytes(n: float) -> str:
    """kopia's own byte format: "72 B", "8.5 KB", "1 KB", "2.4 GB"."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        n /= 1000
    return ""


_PACK = 20_000_000      # kopia fills data packs to about 20 MB


def blob_stats_lines(stored: int, snapshots: int) -> list[str]:
    """``kopia blob stats`` output for a repo storing ``stored`` bytes: data packs of about 20 MB
    plus the small index, manifest and log blobs (shape taken from a real repository)."""
    packs = max(1, -(-stored // _PACK))
    small = [(4, 18), (20, 430), (max(10, snapshots), 2_000), (packs // 14 + 1, 70_000), (1, 320_000),
             (max(1, packs // 36), 6_000_000)]
    blobs = [size for n, size in small for _ in range(n)] + [stored // packs] * packs
    out = [f"Count: {len(blobs)}", f"Total: {_kopia_bytes(sum(blobs))}",
           f"Average: {_kopia_bytes(sum(blobs) // len(blobs))}", "Histogram:", ""]
    low = 0
    for high in (10 ** e for e in range(1, 9)):
        hits = [b for b in blobs if low <= b < high]
        out.append(f"{len(hits):9d} between {_kopia_bytes(low)} and {_kopia_bytes(high)} (total {_kopia_bytes(sum(hits))})")
        low = high
    return out


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
            files, size = idx.under(path)
            say(f"Snapshotting {path} ...", "stderr")
            say(f" * 0 hashing, {files} hashed ({_kopia_bytes(size)}), 0 cached (0 B), uploaded 0 B, "
                "estimating...", "stderr")
            say("Created snapshot.", "stderr")
        elif head[:2] == ["snapshot", "estimate"]:
            path = (_positional(args, 2) or [""])[0]
            files, size = idx.under(path)
            say("Analyzing . ...", "stderr")
            say(f"Snapshot includes {files} file(s), total size {_kopia_bytes(size)}")
            say("Snapshot excludes no files.")
            say("Snapshot excludes no directories.")
        elif head[:2] == ["snapshot", "verify"]:
            stored, snapshots = idx.storage()
            pct = next((float(a.split("=", 1)[1]) for a in args if a.startswith("--verify-files-percent=")), 0.0)
            blobs = max(1, stored // _PACK) + snapshots * 2
            objects = max(snapshots * 40, blobs * 3)
            say("Listing blobs...", "stderr")
            for n in range(10000, blobs, 10000):
                say(f"  {n} blobs...", "stderr")
            say(f"Listed {blobs} blobs.", "stderr")
            for i in range(3):
                say(f"Processed {objects * i // 3} objects ({_kopia_bytes(stored * i / 3)}). "
                    f"Read {int(objects * pct / 100) * i // 3} files ({_kopia_bytes(stored * pct / 100 * i / 3)}).", "stderr")
            say(f"Finished processing {objects} objects ({_kopia_bytes(stored)}). "
                f"Read {int(objects * pct / 100)} files ({_kopia_bytes(stored * pct / 100)}).", "stderr")
        elif head[:2] == ["blob", "stats"]:
            for line in blob_stats_lines(*idx.storage()):
                say(line)
        elif head[:1] == ["policy"]:
            say(f"Policy {head[1] if len(head) > 1 else 'write'}: done.")
        else:
            say(f"kopia {' '.join(head)}: done.")
    finally:
        idx.close()
    from config import DEMO_SHOW
    if DEMO_SHOW is None or DEMO_SHOW["job_note"]:
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
