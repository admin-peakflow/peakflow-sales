#!/bin/sh
# Container entrypoint. Three jobs, then hand over to grizly.
#
# 1. Ownership of the data volume. Docker creates a bind mount or a named volume
#    owned by root, but Claude Code refuses to run as root and grizly must write
#    grizly.csv into that volume. Starting as root, fixing the owner and dropping
#    to `node` (the same pattern the postgres image uses) means the first run on
#    a fresh server does not fail with "permission denied".
# 2. `--shell`: an interactive shell inside the image, as the same user.
# 3. Nothing else. Every grizly command is passed straight through, so
#    `docker run … enrich --in grizly.csv` behaves like `python3 grizly.py enrich …`.

set -e

if [ "$(id -u)" = "0" ]; then
    if [ -d /app/data ] && [ "$(stat -c %u /app/data)" != "$(id -u node)" ]; then
        echo "fixing ownership of /app/data for uid $(id -u node)…" >&2
        chown -R node:node /app/data || {
            echo "ERROR: cannot change the owner of /app/data." >&2
            echo "       On the host run: sudo chown -R 1000:1000 ./data" >&2
            echo "       (read-only mounts and NFS root_squash need this by hand)" >&2
            exit 1
        }
    fi
    if [ "${1:-}" = "--shell" ]; then
        shift
        exec gosu node bash -l "$@"
    fi
    exec gosu node python3 /app/grizly.py "$@"
fi

# Already unprivileged (compose `user:`, or a hardened runtime): run directly.
if [ "${1:-}" = "--shell" ]; then
    shift
    exec bash -l "$@"
fi
exec python3 /app/grizly.py "$@"
