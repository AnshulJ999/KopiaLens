"""RO kopia subprocess runner + ALLOWED allowlist; plus ls/restore/mount.
PORT from kopiatoolkit.py: kopia(), _check_ro(), ALLOWED (already includes the show command).
ADD: ls() to browse a dir, restore(obj, dest, opts) (writes to disk, dialog-driven), mount(obj) (whole snapshot).
NEVER add a repo-destructive command here -- snapshot delete is command-generation in api/actions.py. TODO."""
