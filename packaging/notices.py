"""Write THIRD-PARTY-NOTICES.txt: the licences of everything a KopiaLens build ships.

    python packaging/notices.py <out file> [--python <exe>] [--kopia-license <file>] [--es-license <file>]
        [--cargo <Cargo.toml>]... [--cargo-metadata <json>]... [--target <rust triple>] [--parts <json>]
    python packaging/notices.py merge <out file> <parts json>...

`--parts` also saves the notices as data; `merge` joins several platforms' parts into one file
(the release's own THIRD-PARTY-NOTICES.txt, covering every platform).

Python packages are read from the interpreter that compiles the server (the build venv): every
package requirements.txt pulls in, plus Python itself and Nuitka's runtime. Rust crates come from
`cargo metadata` (normal dependencies for the target; build tools and proc-macros are not shipped).
Identical licence texts are printed once, with every component that uses them.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(KL_ROOT, "frontend", "vendor")
RULE = "=" * 78
LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|notice|unlicense)", re.I)

# Runs inside the build interpreter: the requirements closure with each package's licence files.
_PY_PROBE = r'''
import json, os, re, sys, sysconfig
from importlib import metadata
try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
NAME = re.compile(r"^(licen[cs]e|copying|notice)", re.I)
def norm(n): return re.sub(r"[-_.]+", "-", n).lower()
todo = []
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        line = line.split("#")[0].strip()
        if line:
            r = Requirement(line)
            todo.append((r.name, set(r.extras)))
todo.append(("nuitka", set()))
seen, out = {}, []
while todo:
    name, extras = todo.pop()
    key = norm(name)
    if key in seen and extras <= seen[key]:
        continue
    seen[key] = seen.get(key, set()) | extras
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        continue
    for req in dist.requires or []:
        r = Requirement(req)
        if r.marker is None or any(r.marker.evaluate({"extra": e}) for e in (seen[key] or {""})):
            todo.append((r.name, set(r.extras)))
    meta = dist.metadata
    lic = meta.get("License-Expression") or ""
    if not lic:
        short = (meta.get("License") or "").strip()
        lic = short if short and "\n" not in short and len(short) < 80 else ""
    if not lic:
        lic = "; ".join(c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::"))
    texts = []
    for f in dist.files or []:
        if NAME.match(os.path.basename(str(f))) and ".dist-info" in str(f):
            try:
                texts.append(dist.locate_file(f).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    out.append({"name": meta["Name"], "version": dist.version, "license": lic,
                "url": meta.get("Home-page") or "", "texts": texts})
py_license = ""
for p in (os.path.join(sys.base_prefix, "LICENSE.txt"), os.path.join(sysconfig.get_path("stdlib"), "LICENSE.txt")):
    if os.path.isfile(p):
        py_license = open(p, encoding="utf-8", errors="replace").read()
        break
uniq = {norm(p["name"]): p for p in out}
print(json.dumps({"python": sys.version.split()[0], "python_license": py_license,
                  "packages": sorted(uniq.values(), key=lambda p: p["name"].lower())}))
'''


class Notices:
    def __init__(self) -> None:
        self.groups: dict[str, dict] = {}   # text hash -> {"text", "users"}
        self.listing: list[tuple[str, list[str]]] = []

    def add(self, user: str, texts: list[str]) -> None:
        for text in texts:
            text = text.strip()
            if not text:
                continue
            key = hashlib.sha256(re.sub(r"\s+", " ", text).encode()).hexdigest()
            group = self.groups.setdefault(key, {"text": text, "users": []})
            if user not in group["users"]:
                group["users"].append(user)

    def section(self, title: str, lines: list[str]) -> None:
        self.listing.append((title, lines))

    def to_parts(self) -> dict:
        return {"listing": self.listing, "groups": self.groups}

    def merge_parts(self, parts: dict) -> None:
        titles = {title: lines for title, lines in self.listing}
        for title, lines in parts["listing"]:
            if title not in titles:
                titles[title] = []
                self.listing.append((title, titles[title]))
            titles[title].extend(ln for ln in lines if ln not in titles[title])
        for key, group in parts["groups"].items():
            mine = self.groups.setdefault(key, {"text": group["text"], "users": []})
            mine["users"].extend(u for u in group["users"] if u not in mine["users"])

    def render(self) -> str:
        out = ["KopiaLens third-party notices", "",
               "Built on Kopia, by Jarek Kowalski and contributors. Not affiliated with or endorsed by the Kopia project.", "",
               "KopiaLens includes the software listed below. Each component is used under its own licence;",
               "the full licence texts follow the list.", ""]
        for title, lines in self.listing:
            out += [RULE, title, RULE, *lines, ""]
        out += [RULE, "Licence texts", RULE, ""]
        for group in self.groups.values():
            out += ["-" * 78, "Used by: " + ", ".join(group["users"]), "-" * 78, group["text"], ""]
        return "\n".join(out) + "\n"


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def add_python(n: Notices, python: str) -> None:
    proc = subprocess.run([python, "-I", "-c", _PY_PROBE, os.path.join(KL_ROOT, "requirements.txt")],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    if proc.returncode != 0:
        raise SystemExit(f"notices: reading Python packages failed: {proc.stderr.strip()[-400:]}")
    data = json.loads(proc.stdout)
    lines = [f"Python {data['python']} (PSF License), the runtime compiled into the server"]
    n.add(f"Python {data['python']}", [data["python_license"]])
    for p in data["packages"]:
        label = f"{p['name']} {p['version']}"
        lic = p["license"] or "see package"
        if p["name"].lower() == "nuitka":   # only its runtime parts are compiled in
            lic += ", with the Nuitka Runtime Library Exception (runtime parts only)"
        missing = "" if p["texts"] else " (no licence file in the package)"
        lines.append(f"{label} - {lic}{missing}")
        n.add(label, p["texts"])
    n.section("Python runtime and packages (server)", lines)


def _vendor_version(pattern: str) -> str:
    found = glob.glob(os.path.join(VENDOR, pattern))
    m = re.search(r"-(\d+(?:\.\d+)+)", os.path.basename(found[0])) if found else None
    return m.group(1) if m else ""


def add_web(n: Notices) -> None:
    items = [
        ("Alpine.js", _vendor_version("alpine-*.min.js"), "MIT", [os.path.join(VENDOR, "alpine-LICENSE.txt")]),
        ("Tabulator", _vendor_version("tabulator-*.min.js"), "MIT", [os.path.join(VENDOR, "tabulator-LICENSE.txt")]),
        ("marked", _vendor_version("marked-*.min.js"), "MIT", [os.path.join(VENDOR, "marked-LICENSE.txt")]),
        ("Driver.js", _vendor_version("driver-*.min.js"), "MIT", [os.path.join(VENDOR, "driver-LICENSE.txt")]),
        ("Tailwind CSS (compiled styles)", "", "MIT", [os.path.join(VENDOR, "tailwind-LICENSE.txt")]),
    ]
    for lic in sorted(glob.glob(os.path.join(VENDOR, "fonts", "*-LICENSE.txt"))):
        items.append((os.path.basename(lic).removesuffix("-LICENSE.txt") + " font", "", "SIL Open Font License 1.1", [lic]))
    for folder in sorted(glob.glob(os.path.join(VENDOR, "icons", "*", ""))):
        lic = os.path.join(folder, "LICENSE.txt")
        if os.path.isfile(lic):
            items.append((os.path.basename(os.path.dirname(folder)) + " icons", "", "", [lic]))
    lines = []
    for name, version, lic, files in items:
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            raise SystemExit(f"notices: licence file missing: {missing[0]}")
        label = f"{name} {version}".strip()
        lines.append(f"{label}" + (f" - {lic}" if lic else ""))
        n.add(label, [_read(f) for f in files])
    n.section("Web interface (scripts, styles, fonts, icons)", lines)


def add_programs(n: Notices, kopia_license: str | None, es_license: str | None) -> None:
    lines = []
    if kopia_license:
        lines.append("kopia - Apache License 2.0 - https://github.com/kopia/kopia")
        n.add("kopia", [_read(kopia_license)])
    if es_license:
        lines.append("ES (Everything command-line interface) - MIT - https://github.com/voidtools/ES")
        n.add("ES", [_read(es_license)])
    if lines:
        n.section("Bundled programs", lines)


def _cargo_metadata(manifest: str, target: str | None) -> dict:
    cmd = ["cargo", "metadata", "--format-version", "1", "--locked", "--manifest-path", manifest]
    if target:
        cmd += ["--filter-platform", target]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600)
    if proc.returncode != 0:
        raise SystemExit(f"notices: cargo metadata failed for {manifest}: {proc.stderr.strip()[-400:]}")
    return json.loads(proc.stdout)


def _shipped_crates(meta: dict) -> list[dict]:
    """Packages reachable from the workspace through normal dependencies, minus proc-macros and our own."""
    pkgs = {p["id"]: p for p in meta["packages"]}
    nodes = {nd["id"]: nd for nd in meta["resolve"]["nodes"]}
    own = set(meta["workspace_members"])
    seen, todo = set(), list(own)
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        for dep in nodes[pid]["deps"]:
            if any(k["kind"] is None for k in dep["dep_kinds"]):
                todo.append(dep["pkg"])
    out = []
    for pid in seen - own:
        p = pkgs[pid]
        if all("proc-macro" in t["kind"] for t in p["targets"]):
            continue
        out.append(p)
    return out


def _crate_texts(p: dict) -> list[str]:
    root = os.path.dirname(p["manifest_path"])
    files = [os.path.join(root, f) for f in sorted(os.listdir(root)) if LICENSE_NAME.match(f)
             and os.path.isfile(os.path.join(root, f))]
    if p.get("license_file"):
        extra = os.path.join(root, p["license_file"])
        if os.path.isfile(extra) and extra not in files:
            files.append(extra)
    return [_read(f) for f in files]


def add_rust(n: Notices, metas: list[dict]) -> None:
    crates: dict[str, dict] = {}
    for meta in metas:
        for p in _shipped_crates(meta):
            crates[f"{p['name']} {p['version']}"] = p
    if not crates:
        return
    lines = []
    for label in sorted(crates, key=str.lower):
        p = crates[label]
        texts = _crate_texts(p)
        note = ""
        if not texts:
            authors = ", ".join(p.get("authors") or [])
            note = " (no licence file in the crate" + (f"; authors: {authors})" if authors else ")")
        lines.append(f"{label} - {p.get('license') or 'see crate'}{note}")
        n.add(label, texts)
    n.section("Rust crates (desktop app, command-line launcher, disk scanner)", lines)


def merge(out: str, parts: list[str]) -> None:
    n = Notices()
    for path in parts:
        with open(path, encoding="utf-8") as fh:
            n.merge_parts(json.load(fh))
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(n.render())
    print(f"notices: {out} merged from {len(parts)} part(s) ({len(n.groups)} licence texts)")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["merge"]:
        if len(argv) < 3:
            raise SystemExit("usage: notices.py merge <out file> <parts json>...")
        merge(argv[1], argv[2:])
        return
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--kopia-license")
    ap.add_argument("--es-license")
    ap.add_argument("--cargo", action="append", default=[], help="a Cargo.toml whose crates ship")
    ap.add_argument("--cargo-metadata", action="append", default=[], help="saved `cargo metadata` JSON")
    ap.add_argument("--target", help="Rust target triple for --cargo (default: this machine's)")
    ap.add_argument("--parts", help="also save the notices as data here, for `merge`")
    args = ap.parse_args(argv)
    if args.cargo and not args.target:
        host = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, timeout=60).stdout
        args.target = next((ln.split(":", 1)[1].strip() for ln in host.splitlines() if ln.startswith("host:")), None)
    n = Notices()
    add_programs(n, args.kopia_license, args.es_license)
    add_python(n, args.python)
    add_web(n)
    metas = [_cargo_metadata(m, args.target) for m in args.cargo]
    for path in args.cargo_metadata:
        with open(path, encoding="utf-8") as fh:
            metas.append(json.load(fh))
    add_rust(n, metas)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(n.render())
    if args.parts:
        with open(args.parts, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(n.to_parts(), fh)
    print(f"notices: {args.out} ({len(n.groups)} licence texts)")


if __name__ == "__main__":
    main()
