"""Stage the Notes widget for a testing build (release.py --with-notes, the Docker build arg, a WSL
source run). The build then ships it as devnotes/ beside the server, where app/core/devnotes.py
serves it and sends every note to another KopiaLens. Normal builds never include it.

    python packaging/notes_bundle.py <folder>
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
MARKER = "BUNDLE.txt"   # app/core/devnotes.py serves the folder only when this is present


def stage(dest: str) -> str:
    missing = [p for p in FILES.values() if not os.path.isfile(p)]
    if missing:
        raise SystemExit("Notes widget files not found:\n  " + "\n  ".join(missing))
    os.makedirs(dest, exist_ok=True)
    for name, src in FILES.items():
        shutil.copy2(src, os.path.join(dest, name))
    with open(os.path.join(dest, MARKER), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("Notes widget for KopiaLens testing builds. Not for public releases.\n")
    return dest


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(stage(os.path.abspath(sys.argv[1])))
