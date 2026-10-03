"""Build the web demo: a static site that runs KopiaLens in the browser on generated sample data.

    python packaging\\build_webdemo.py               build into <BUILD_ROOT>\\webdemo\\<version>
    python packaging\\build_webdemo.py --serve 7850  build, then serve it locally

The site needs no server of its own: Pyodide and its packages load from the CDN below, and the
app's API calls are answered in the page by the real backend (webdemo/kl_web.py)."""
import argparse
import html
import http.server
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_ROOT = os.environ.get("KOPIALENS_BUILD_ROOT") or r"J:\Excluded\KopiaLens-Build"
WEBDEMO = os.path.join(KL_ROOT, "webdemo")

PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/"
PACKAGES = ["fastapi", "httpx", "orjson", "duckdb", "pyarrow", "pydantic", "jinja2",
            "cryptography", "argon2-cffi", "zstandard"]
# Download sizes of this Pyodide release (core files; PACKAGES with their dependencies).
RUNTIME_MB, LIBRARIES_MB = 6.2, 31.3
SAMPLE_FILES = 20000


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def child(code: str, data_dir: str) -> str:
    env = {**os.environ, "KOPIALENS_DATA_DIR": data_dir, "PYTHONIOENCODING": "utf-8"}
    res = subprocess.run([sys.executable, "-c", code], cwd=KL_ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        sys.exit(f"build step failed:\n{res.stderr[-3000:]}")
    return res.stdout


def zip_tree(out: str, items: list[tuple[str, str]]) -> float:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, arc in items:
            z.write(src, arc)
    return os.path.getsize(out) / 1e6


def code_files() -> list[tuple[str, str]]:
    items = [(os.path.join(KL_ROOT, n), n) for n in ("config.py", "VERSION")]
    items.append((os.path.join(WEBDEMO, "kl_web.py"), "kl_web.py"))
    for dirpath, dirs, files in os.walk(os.path.join(KL_ROOT, "app")):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
        for f in files:
            if f.endswith((".py", ".json")):
                p = os.path.join(dirpath, f)
                items.append((p, os.path.relpath(p, KL_ROOT).replace("\\", "/")))
    return items


def data_files(root: str) -> list[tuple[str, str]]:
    items = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            items.append((p, os.path.relpath(p, root).replace("\\", "/")))
    return items


def page(version: str, steps: list[dict]) -> str:
    """The app's own page, rendered once, with static paths made relative and the demo's
    loader in front of the app scripts."""
    scratch = os.path.join(BUILD_ROOT, "webdemo", "_render")
    os.makedirs(scratch, exist_ok=True)
    text = child("import sys; from app.main import _compose_html; sys.stdout.write(_compose_html())", scratch)
    text = text.replace('"/static/', '"static/')
    if "/static/" in text:
        sys.exit("page still holds an absolute /static/ path")
    cfg = {"version": version, "pyodide": PYODIDE, "packages": PACKAGES, "base": "webdemo/",
           "steps": [{"id": s["id"], "label": s["label"], "mb": s["mb"]} for s in steps]}
    head = (f'  <link href="webdemo/webdemo.css?v={version}" rel="stylesheet">\n'
            f"  <script>window.KL_WEB_DEMO = {json.dumps(cfg)};</script>\n"
            f'  <script src="webdemo/bridge.js?v={version}"></script>\n</head>')
    with open(os.path.join(WEBDEMO, "loading.html"), encoding="utf-8") as f:
        loading = f.read()
    rows = "\n        ".join(f'<li id="kl-web-step-{s["id"]}" data-state="wait">{html.escape(s["label"])}</li>'
                           for s in steps)
    loading = loading.replace("{steps}", rows).replace(
        "{total_mb}", str(round(sum(s["mb"] for s in steps) / 5) * 5))
    text = text.replace("</head>", head, 1)
    at = text.index(">", text.index("<body")) + 1
    return text[:at] + "\n" + loading + text[at:]


def build(out: str, files: int) -> None:
    with open(os.path.join(KL_ROOT, "VERSION"), encoding="utf-8") as f:
        version = f.read().strip()
    building = out + ".building"
    if os.path.isdir(building):
        shutil.rmtree(building)
    site = os.path.join(building, "site")
    os.makedirs(os.path.join(site, "webdemo"))

    log(f"sample data ({files:,} files)")
    sample = os.path.join(building, "sample")
    child(f"from app.core.sampledata import build; build({sample!r}, files={files})",
          os.path.join(building, "_kl"))
    data_mb = zip_tree(os.path.join(site, "webdemo", "data.zip"), data_files(sample))
    code_mb = zip_tree(os.path.join(site, "webdemo", "app.zip"), code_files())

    log("page and static files")
    steps = [{"id": "runtime", "label": "Python runtime", "mb": RUNTIME_MB},
             {"id": "libraries", "label": "Libraries", "mb": LIBRARIES_MB},
             {"id": "data", "label": "Sample data", "mb": round(data_mb, 1)},
             {"id": "start", "label": "Starting KopiaLens", "mb": round(code_mb, 1)}]
    with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as f:
        f.write(page(version, steps))
    shutil.copytree(os.path.join(KL_ROOT, "frontend"), os.path.join(site, "static"),
                    ignore=shutil.ignore_patterns("partials", "index.html"))
    for name in ("bridge.js", "worker.js", "webdemo.css"):
        shutil.copy2(os.path.join(WEBDEMO, name), os.path.join(site, "webdemo", name))

    if os.path.isdir(out):
        shutil.rmtree(out)
    os.replace(building, out)
    site_mb = sum(os.path.getsize(p) for p, _ in data_files(os.path.join(out, "site"))) / 1e6
    log(f"built {os.path.join(out, 'site')}: site {site_mb:.1f} MB (data {data_mb:.1f}, code {code_mb:.1f}); "
        f"first visit also loads about {RUNTIME_MB + LIBRARIES_MB:.0f} MB of Pyodide from the CDN")


def current(out: str) -> bool:
    """The built site is newer than every source file it is made from."""
    try:
        built = os.path.getmtime(os.path.join(out, "site", "index.html"))
    except OSError:
        return False
    for top in ("app", "frontend", "webdemo"):
        for dirpath, dirs, files in os.walk(os.path.join(KL_ROOT, top)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
            if any(os.path.getmtime(os.path.join(dirpath, f)) > built for f in files):
                return False
    return True


def serve(root: str, port: int, open_browser: bool = False) -> None:
    os.chdir(root)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler)
    url = f"http://127.0.0.1:{port}/"
    log(f"serving {url} (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the KopiaLens web demo.")
    ap.add_argument("--out", help="output folder (default: <BUILD_ROOT>\\webdemo\\<version>)")
    ap.add_argument("--files", type=int, default=SAMPLE_FILES, help="sample size in files")
    ap.add_argument("--serve", type=int, metavar="PORT", help="serve the built site afterwards")
    ap.add_argument("--reuse", action="store_true", help="skip the build when the site is up to date")
    ap.add_argument("--open", action="store_true", help="open the browser when serving")
    args = ap.parse_args()
    with open(os.path.join(KL_ROOT, "VERSION"), encoding="utf-8") as f:
        version = f.read().strip()
    out = args.out or os.path.join(BUILD_ROOT, "webdemo", version)
    if args.reuse and current(out):
        log(f"up to date: {os.path.join(out, 'site')}")
    else:
        build(out, args.files)
    if args.serve:
        serve(os.path.join(out, "site"), args.serve, args.open)


if __name__ == "__main__":
    main()
