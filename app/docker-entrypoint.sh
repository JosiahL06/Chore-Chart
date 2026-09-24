#!/bin/sh
# Container entrypoint: fix data-directory ownership, drop privileges, exec.
#
# The container starts as root for one reason only: Docker creates a missing
# bind-mount source (./data) as root:root, and data written by older root-run
# images is root-owned as well -- neither is writable by the unprivileged
# chore-chart user (uid 1000). So chown the data dir here, at start, then
# re-exec the real command as uid/gid 1000 via setpriv: server.py itself
# never runs as root. If the container was started with an explicit non-root
# `user:`/`--user`, the chown is skipped and the command runs as that user.
set -eu

if [ "$(id -u)" = 0 ]; then
    mkdir -p /app/data
    chown -R 1000:1000 /app/data
    export HOME=/home/chore-chart
    exec setpriv --reuid=1000 --regid=1000 --clear-groups -- "$@"
fi

exec "$@"
