#!/bin/sh
# Runs KopiaLens as PUID:PGID when they are set (NAS convention), else as root like kopia's own image.
set -e
mkdir -p "$KOPIALENS_DATA_DIR" "$KOPIALENS_MOUNT_DIR"

if [ "$(id -u)" = "0" ] && [ -n "$PUID" ]; then
    PGID="${PGID:-$PUID}"
    getent group "$PGID" >/dev/null 2>&1 || groupadd -o -g "$PGID" kopialens
    getent passwd "$PUID" >/dev/null 2>&1 || useradd -o -u "$PUID" -g "$PGID" -M -d "$HOME" -s /usr/sbin/nologin kopialens
    # Only the folders KopiaLens owns, and only when they belong to someone else.
    for d in "$KOPIALENS_DATA_DIR" "$KOPIALENS_MOUNT_DIR"; do
        [ "$(stat -c %u:%g "$d")" = "$PUID:$PGID" ] || chown -R "$PUID:$PGID" "$d"
    done
    exec gosu "$PUID:$PGID" "$@"
fi
exec "$@"
