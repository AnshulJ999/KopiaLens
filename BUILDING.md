# Building KopiaLens from source

Most people should use the [official downloads](https://kopialens.com/download). This page is for running KopiaLens from its source code, building it yourself, or working on it.

## Run from source

You need Python 3.13 or newer and [kopia](https://kopia.io/docs/installation/).

```
pip install -r requirements.txt
python main.py
```

KopiaLens opens on `http://127.0.0.1:7123`. Set `KOPIALENS_PORT` to use another port, and `KOPIALENS_DATA_DIR` to choose where its settings and index are kept.

KopiaLens looks for kopia on `PATH`, in the usual install locations, and in the copy KopiaUI bundles. Set `KOPIALENS_KOPIA` to point it at a specific kopia binary.

To reset the sign-in password: `python main.py --reset-password`.

## Tests

```
pip install -r requirements.txt -r requirements-dev.txt pytest
python -m pytest
```

The browser tests in `app/tests/ui` also need kopia, Playwright (`pip install playwright`, then `python -m playwright install chromium`) and the disk scanner (`cargo build --release` in `shell/disk-scanner`, with [Rust](https://rustup.rs) installed); they are skipped when kopia or Chromium is missing.

## Build the server

The release server is compiled with [Nuitka](https://nuitka.net), which needs a C compiler (Visual Studio Build Tools on Windows, gcc on Linux, the Xcode command line tools on macOS):

```
pip install nuitka==4.2.2
set KOPIALENS_BUILD_OUT=C:\path\to\build-output
python packaging/build_server.py
```

(`export KOPIALENS_BUILD_OUT=...` on macOS and Linux.) The output is `kopialens.dist/` in that folder.

## Build the Docker image

```
docker build -f packaging/docker/Dockerfile -t kopialens:local .
```

## What the official builds add

- **The desktop app** (`shell/src-tauri`, built with [Tauri](https://tauri.app)) wraps the server with a window, a tray icon and updates. It's built by the maintainer's release pipeline (`packaging/release.py`), which needs the maintainer's signing keys and private licensing tools, so it doesn't run from a public checkout.
- **The Notes widget** (Settings > Experimental) is a review tool whose source lives in a separate project. Builds from this repository leave it out; nothing else changes.
- **Pro** is a licence key for the official builds. See the licence section of the [README](README.md#source-and-licence).
