# syntax=docker/dockerfile:1
# KopiaLens release image: the server compiled with Nuitka, plus a pinned kopia. No Python source ships.
# Build (from the KopiaLens folder):
#   docker build -f packaging/docker/Dockerfile -t kopialens:local .
# Testing build with the Notes widget (never published): stage it first, then pass the folder:
#   python packaging/notes_bundle.py packaging/docker/devnotes
#   docker build -f packaging/docker/Dockerfile --build-arg KOPIALENS_BUILD_NOTES=/src/packaging/docker/devnotes -t kopialens:testing .
ARG PYTHON_VERSION=3.14
ARG DEBIAN=trixie

# ---- compile the server ------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-${DEBIAN} AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev patchelf ccache \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt nuitka==4.2.2
COPY . .
ARG KOPIALENS_BUILD_NOTES=
# The commit shown in About (the build context has no git); the release pipeline passes it.
ARG KOPIALENS_COMMIT=
ENV KOPIALENS_BUILD_OUT=/out
RUN --mount=type=cache,target=/root/.cache/Nuitka \
    python packaging/build_server.py || (tail -40 /out/build.log; exit 1)

# ---- the disk scanner (shell/disk-scanner) ----------------------------------------------------
FROM rust:1-slim-${DEBIAN} AS scanner
WORKDIR /src
COPY shell/disk-scanner .
RUN cargo build --release --locked && ./target/release/kopialens-scan --version

# ---- kopia, checksum-verified ------------------------------------------------------------------
FROM debian:${DEBIAN}-slim AS kopia
ARG KOPIA_VERSION=0.23.1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "$(dpkg --print-architecture)" in amd64) k=x64 ;; arm64) k=arm64 ;; *) exit 1 ;; esac; \
    f="kopia-${KOPIA_VERSION}-linux-${k}.tar.gz"; \
    base="https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}"; \
    cd /tmp; curl -fsSLO "$base/checksums.txt"; curl -fsSLO "$base/$f"; \
    grep " $f\$" checksums.txt | sha256sum -c -; \
    tar -xzf "$f"; mkdir -p /kopia; mv kopia-*/kopia kopia-*/LICENSE /kopia/

# ---- final image -------------------------------------------------------------------------------
FROM debian:${DEBIAN}-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl fuse3 gosu tini tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/kopialens.dist /app
COPY --from=kopia /kopia /app/kopia
COPY --from=scanner /src/target/release/kopialens-scan /app/kopialens-scan
COPY packaging/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /app/kopia/kopia
# The command line: docker exec <container> klens status
RUN for n in klens kopialens-cli; do \
      printf '#!/bin/sh\nexec /app/kopialens-server --cli "$@"\n' > /usr/local/bin/$n && chmod 0755 /usr/local/bin/$n; \
    done

# /config holds settings, the index and kopia's cache and config (mount a volume there).
# /mounts holds snapshot mounts (optional; mount a host folder with :shared to see them outside).
# UTF-8 locale: without it the compiled server reads non-ASCII file names as ASCII.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    KOPIALENS_DATA_DIR=/config \
    KOPIALENS_MOUNT_DIR=/mounts \
    KOPIALENS_IN_DOCKER=1 \
    XDG_CACHE_HOME=/config/kopia-cache \
    XDG_CONFIG_HOME=/config/kopia-config \
    HOME=/config \
    KOPIA_CHECK_FOR_UPDATES=false \
    TZ=UTC
EXPOSE 7123
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD curl -fsSk https://127.0.0.1:7123/health || curl -fsS http://127.0.0.1:7123/health || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["/app/kopialens-server", "--server"]
