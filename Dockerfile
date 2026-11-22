# syntax=docker/dockerfile:1
#
# grizly in a container. Node carries Claude Code, Python carries grizly
# (standard library only — nothing to pip install).
#
#   docker build -t grizly:2.0 .
#   docker run --rm -v "$PWD/data:/app/data" --env-file .env grizly:2.0 selftest
#
# Claude Code authenticates with ANTHROPIC_API_KEY inside a container. The OAuth
# login of a desktop machine does not travel with the image.

FROM node:22-bookworm-slim

# Claude Code is pinned: an unexpected CLI update must not change the lean flags
# `grizly/llm.py` depends on (`selftest` checks them).
ARG CLAUDE_CODE_VERSION=2.1.330

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/node \
    GRIZLY_PROFILE=peakflow \
    GRIZLY_CACHE=/app/data/grizly-cache \
    GRIZLY_LOG=/app/data/grizly-cost.jsonl \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1

# python3 = 3.11 on bookworm, the minimum grizly asks for. ca-certificates for
# the registries and ECB, git/curl/ripgrep because Claude Code expects them,
# tzdata so TZ=Europe/Prague gives Czech dates in enriched_at / found_on,
# gosu so the entrypoint can fix the volume's owner and drop root.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 ca-certificates curl git ripgrep tzdata gosu \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# Claude Code refuses to run as root in a container. The base image already has
# an unprivileged user `node` (uid 1000); reuse it instead of adding a second
# uid 1000. The entrypoint below drops root before grizly starts.
#
# The config is pre-seeded with `hasCompletedOnboarding`: on a fresh server there
# is no ~/.claude.json, and a non-interactive `-p` run must not stop at a
# first-start dialog nobody can answer.
RUN mkdir -p /home/node/.claude \
    && printf '%s\n' '{"hasCompletedOnboarding":true}' > /home/node/.claude.json \
    && chown -R node:node /home/node

WORKDIR /app
COPY --chown=node:node grizly.py /app/grizly.py
COPY --chown=node:node grizly /app/grizly
COPY --chown=node:node profiles /app/profiles

# /app/data is the volume: grizly.csv, contacts-*.csv, grizly-cache/,
# grizly-cost.jsonl and the suppression lists (neoslovovat.csv, klienti.csv)
# all live next to each other, because the profile paths are relative.
RUN mkdir -p /app/data && chown -R node:node /app

# Starts as root only to fix the volume's owner, then drops to `node`.
COPY --chown=node:node docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# No USER here on purpose: see docker-entrypoint.sh. grizly itself always runs
# as `node` (uid 1000), never as root.
# Run from the volume: every default path in grizly is relative to the cwd.
WORKDIR /app/data

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# Costs nothing, needs no network: proves the image is sane before a paid run.
CMD ["selftest", "--offline"]
