#!/bin/sh
# Copies the read-only source at /src into /work (without the data folder or build output), then runs the command.
set -e
if [ -d /src ] && [ ! -f /work/main.py ]; then
    tar -C /src --exclude=./data --exclude=./shell/src-tauri/target --exclude=./shell/src-tauri/release-stage --exclude='*/__pycache__' \
        --exclude=./.pytest_cache -cf - . | tar -C /work -xf -
fi
exec "$@"
