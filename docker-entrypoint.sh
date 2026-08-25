#!/bin/sh
# Start unprivileged, but repair an inherited root-owned data directory first.
#
# Before v1.13.6 the image ran as root, so every existing deployment's
# /app/data (named volume or bind mount) is owned by root. Simply adding a
# USER directive made those installs fail on upgrade with
# "sqlite3.OperationalError: attempt to write a readonly database".
#
# So: if the container starts as root, hand the data directory to the app user
# and drop privileges before exec'ing. The application process itself never
# runs as root. If the container was started with an explicit --user, there is
# nothing to repair and nothing to drop, so exec straight through.
set -e

APP_UID=10001
APP_GID=10001

if [ "$(id -u)" = "0" ]; then
    # Only touch ownership when the app user actually cannot write, so a bind
    # mount that is already correct (or deliberately world-writable) is left
    # alone rather than being rewritten on the host.
    if ! su feedecho -s /bin/sh -c 'test -w /app/data' 2>/dev/null; then
        echo "entrypoint: /app/data is not writable by feedecho; fixing ownership" >&2
        chown -R "$APP_UID:$APP_GID" /app/data 2>/dev/null \
            || echo "entrypoint: could not chown /app/data (read-only mount?)" >&2
    fi
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups "$@"
fi

exec "$@"
