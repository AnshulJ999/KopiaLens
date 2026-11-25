"""Stage the Notes widget (Settings > Experimental) for a build: release.py and build_server.py do
it themselves; the Docker build needs it staged into packaging/docker/devnotes first (its build
context has no widget sources), and a WSL source run stages it for itself. The widget's sources
live outside this repository: without them build_server.py builds without Notes, while release.py
requires them.

    python packaging/notes_bundle.py <folder> [--testing]

<folder>/web/ ships as devnotes/ beside the server (app/core/devnotes.py serves it);
<folder>/py/notes_server/ is the widget's note store, compiled into the server. --testing marks a
testing build, which starts with Experimental and Notes switched on (config.TESTING_BUILD).
"""
import os
import shutil
import sys

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDGET = os.path.join(os.path.dirname(os.path.dirname(KL_ROOT)), "Widgets", "notes")

FILES = {
    "notes-starter.js": os.path.join(WIDGET, "notes-starter.js"),
    "notes-widget.js": os.path.join(WIDGET, "notes-widget.js"),
    "notes-widget.css": os.path.join(WIDGET, "notes-widget.css"),
    "kopialens-notes.js": os.path.join(KL_ROOT, "devtools", "notes", "kopialens-notes.js"),
}
STORE = ("__init__.py", "fastapi_router.py", "notes_store.py", "notes_export.py")
MARKER = "BUNDLE.txt"     # app/core/devnotes.py serves the folder only when this is present
TESTING = "TESTING.txt"   # config.TESTING_BUILD


def _sources() -> list[str]:
    return [*FILES.values(), *(os.path.join(WIDGET, "notes_server", f) for f in STORE)]


def available() -> bool:
    """True when the widget's sources are present."""
    return all(os.path.isfile(p) for p in _sources())


def stage(dest: str, testing: bool = False) -> str:
    missing = [p for p in _sources() if not os.path.isfile(p)]
    if missing:
        raise SystemExit("Notes widget files not found (is Widgets/notes checked out?):\n  " + "\n  ".join(missing))
    web, store = os.path.join(dest, "web"), os.path.join(dest, "py", "notes_server")
    os.makedirs(web, exist_ok=True)
    os.makedirs(store, exist_ok=True)
    for name, src in FILES.items():
        shutil.copy2(src, os.path.join(web, name))
    for name in STORE:
        shutil.copy2(os.path.join(WIDGET, "notes_server", name), os.path.join(store, name))
    with open(os.path.join(web, MARKER), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("The Notes widget (Settings > Experimental).\n")
    marker = os.path.join(web, TESTING)
    if testing:
        with open(marker, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("Testing build: Experimental and Notes start switched on.\n")
    elif os.path.exists(marker):
        os.remove(marker)   # left by an earlier testing stage into the same folder
    return dest


def is_staged(folder: str) -> bool:
    return (os.path.isfile(os.path.join(folder, "web", MARKER))
            and os.path.isfile(os.path.join(folder, "py", "notes_server", "notes_store.py")))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--testing"]
    if len(args) != 1:
        raise SystemExit(__doc__)
    print(stage(os.path.abspath(args[0]), testing="--testing" in sys.argv[1:]))
