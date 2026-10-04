"""App-facing entry point: finds the stored licence and verifies it."""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .model import NO_LICENCE, RANK, Licence, State
from .verify import load_public_keys, normalise, verify

LICENSE_FILE = "license.key"
SHARED_DIR = Path.home() / ".licensekit"
MAX_FILE_BYTES = 64 * 1024


class LicenseKit:
    def __init__(self, product, public_keys, banned_ids=(), app_major=None, app_dir=None,
                 env_prefix=None, shared_dir=SHARED_DIR, clock=None):
        self.product = product
        self.keys = load_public_keys(public_keys)
        self.banned = frozenset(banned_ids)
        self.app_major = app_major
        self.app_dir = Path(app_dir) if app_dir else None
        self.env_prefix = (env_prefix or product).upper()
        self.shared_dir = Path(shared_dir) if shared_dir else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(self, text, origin="pasted") -> Licence:
        return verify(text, product=self.product, keys=self.keys, banned=self.banned,
                      app_major=self.app_major, now=self._clock(), origin=origin)

    def load(self) -> Licence:
        """Best licence among every stored source; NONE when there is none. Never raises."""
        try:
            found = [self.verify(text, origin) for origin, text in self._candidates()]
            found = [lic for lic in found if lic.state is not State.NONE]
            if not found:
                return NO_LICENCE
            far = datetime.max.replace(tzinfo=timezone.utc)
            return max(found, key=lambda lic: (RANK[lic.state], lic.expires or far))
        except Exception:
            return NO_LICENCE

    def save(self, text) -> Path:
        """Store a licence in the app folder (atomic write)."""
        if self.app_dir is None:
            raise ValueError("no app_dir configured")
        self.app_dir.mkdir(parents=True, exist_ok=True)
        target = self.app_dir / LICENSE_FILE
        fd, tmp = tempfile.mkstemp(dir=self.app_dir, prefix=".license-")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(normalise(text) + "\n")
        os.replace(tmp, target)
        return target

    def clear(self) -> None:
        """Remove the licence stored in the app folder (other sources are not touched)."""
        if self.app_dir is not None:
            (self.app_dir / LICENSE_FILE).unlink(missing_ok=True)

    def _candidates(self):
        env_text = os.environ.get(f"{self.env_prefix}_LICENSE")
        if env_text:
            yield f"env {self.env_prefix}_LICENSE", env_text
        env_file = os.environ.get(f"{self.env_prefix}_LICENSE_FILE")
        if env_file:
            text = _read(Path(env_file))
            if text:
                yield f"env {self.env_prefix}_LICENSE_FILE", text
        for folder, label in ((self.app_dir, "app folder"), (self.shared_dir, "shared folder")):
            if folder is not None:
                text = _read(folder / LICENSE_FILE)
                if text:
                    yield label, text


def _read(path: Path) -> str:
    try:
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""
