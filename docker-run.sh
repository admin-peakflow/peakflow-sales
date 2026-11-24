#!/usr/bin/env bash
# grizly in Docker without compose: build once, then run any grizly command.
#
#   ./docker-run.sh selftest
#   ./docker-run.sh pull --region jihomoravsky --limit 100
#   ./docker-run.sh enrich --in grizly.csv --limit 10 --dry-run
#   ./docker-run.sh enrich --in grizly.csv
#   ./docker-run.sh --shell            # interactive shell inside the image
#
# Data lives in ./data on the host (CSV, cache, cost log, suppression lists).
# CLAUDE_CODE_OAUTH_TOKEN is read from .env (cp .env.example .env) or from the
# environment of the shell you run this in.

set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${GRIZLY_IMAGE:-grizly:2.0}"
DATA_DIR="${GRIZLY_DATA:-$PWD/data}"
ENV_FILE="${GRIZLY_ENV_FILE:-$PWD/.env}"

die() { echo "ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not installed"

mkdir -p "$DATA_DIR"
# The container runs as uid 1000 and a bind mount keeps the host owner, so the
# first run on a fresh server needs this once (harmless when already right).
if [ "$(id -u)" = "0" ]; then
    chown -R 1000:1000 "$DATA_DIR"
fi

# Build on first use, or when asked: GRIZLY_BUILD=1 ./docker-run.sh ...
if [ "${GRIZLY_BUILD:-0}" = "1" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "building $IMAGE …"
    docker build -t "$IMAGE" .
fi

env_args=()
if [ -f "$ENV_FILE" ]; then
    env_args+=(--env-file "$ENV_FILE")
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    env_args+=(-e CLAUDE_CODE_OAUTH_TOKEN)
else
    echo "note: no $ENV_FILE, no CLAUDE_CODE_OAUTH_TOKEN — only free steps will work" >&2
    echo "      cp .env.example .env   # then fill the key" >&2
fi

if [ "${1:-}" = "--shell" ]; then
    shift
    exec docker run --rm -it ${env_args[@]+"${env_args[@]}"} \
        -v "$DATA_DIR:/app/data" \
        -w /app/data \
        "$IMAGE" --shell "$@"
fi

exec docker run --rm ${env_args[@]+"${env_args[@]}"} \
    -v "$DATA_DIR:/app/data" \
    -w /app/data \
    "$IMAGE" "$@"
