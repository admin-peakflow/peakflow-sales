# grizly in Docker (server)

The tool is standard library only; the container exists for one reason — Claude
Code needs Node, and the server should not have to care. The image carries
Node 22 + a pinned Claude Code + Python 3.11, and grizly from this repo.

```
pull → enrich → classify    one container, one command per step, CSV on a volume
```

## What is in the box

| File | Purpose |
|---|---|
| `Dockerfile` | Node 22 slim + Claude Code (pinned) + Python 3.11, runs as uid 1000 (`node`), cwd `/app/data` |
| `docker-entrypoint.sh` | fixes the volume's owner once, drops root, passes every grizly command through |
| `docker-compose.yml` | one-shot job, `restart: "no"`, `.env`, `./data` volume |
| `docker-run.sh` | same thing without compose (`./docker-run.sh enrich …`) |
| `.env.example` | every variable with its default |
| `.dockerignore` | keeps tests, caches, CSVs and keys out of the image |

Everything that changes between runs lives in `./data` on the host:

```
data/
  grizly.csv              companies + results (the working file)
  contacts-grizly.csv     contacts, appended across runs
  grizly-cache/           answers already paid for
  grizly-cost.jsonl       one line per company: what it cost
  neoslovovat.csv         do-not-contact list (optional, profile points here)
  klienti.csv             existing clients (optional)
```

## First run on the server

```bash
git clone git@github.com-peakflow:admin-peakflow/peakflow-sales.git grizly
cd grizly
cp .env.example .env
nano .env                        # CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-… or ANTHROPIC_API_KEY  (SERPER_API_KEY recommended)

mkdir -p data                    # no chown needed: the entrypoint fixes the owner once

docker compose build
docker compose run --rm grizly selftest          # free: checks claude, flags, key, registries
```

`selftest` must end with `All good.` before anything paid runs. It verifies the
installed Claude Code still has the flags `grizly/llm.py` needs for the lean run
(no tools, no MCP, no hooks). If it complains, bump `CLAUDE_CODE_VERSION`.

## The four commands

```bash
# 1. free: companies from the registry into data/grizly.csv
docker compose run --rm grizly pull --region jihomoravsky,zlinsky --limit 100

# 2. free: find and audit websites, no model
docker compose run --rm grizly enrich --in grizly.csv --limit 10 --dry-run

# 3. paid: extraction (~$0.07/company, hard cap GRIZLY_BUDGET)
docker compose run --rm grizly enrich --in grizly.csv --limit 10
docker compose run --rm grizly enrich --in grizly.csv            # rest of the file

# 4. free: re-run rules after a threshold change in profiles/peakflow.toml
docker compose run --rm grizly classify --in grizly.csv
```

`enrich` writes progress every 10 companies and resumes from `data/grizly.csv`,
so a killed run loses at most 10 companies. Running the same command again
continues; `--force` re-does finished rows.

## Coolify

1. **New resource → Docker Compose**, repo `peakflow-sales`, compose file
   `docker-compose.yml`.
2. **Environment variables:** `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` (one is required), `SERPER_API_KEY`,
   `GRIZLY_WORKERS`, `GRIZLY_BUDGET` — or paste `.env` in one block.
3. **Persistent storage:** volume `./data → /app/data`.
4. **Deploy** builds the image and runs the default `selftest --offline`.
   Nothing is spent. A real step is a separate one-off run, from the Coolify
   terminal or a server shell:

   ```bash
   docker compose run --rm grizly enrich --in grizly.csv --limit 20
   ```

   or change the resource's start command to the step you want.

Coolify treats this as a job, not a service: it should finish and stay stopped.
Do not set a healthcheck or `restart: always` — a batch that restarts itself
spends money twice.

## Plain docker, no compose

```bash
./docker-run.sh selftest
./docker-run.sh pull --region jihomoravsky --limit 100
./docker-run.sh enrich --in grizly.csv --limit 10
./docker-run.sh --shell          # look around inside the image
```

`GRIZLY_DATA=/srv/grizly-data` moves the volume, `GRIZLY_BUILD=1` forces a
rebuild.

## Nightly wave (cron)

```cron
# 02:10 — enrich 25 more companies, log to a file
10 2 * * * cd /opt/grizly && docker compose run --rm -T grizly enrich --in grizly.csv --limit 25 >> data/cron.log 2>&1
```

`-T` matters in cron (no TTY). Cost control: `GRIZLY_BUDGET` caps one company;
25 companies × ~$0.10 ≈ $2.50 per night. Check `data/grizly-cost.jsonl`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `permission denied` writing `grizly.csv` | volume owned by another uid and the container cannot chown it (rootless Docker, NFS) | run the container with `--user 1000:1000` and `sudo chown -R 1000:1000 data` on the host |
| `claude is not on PATH` | image built from a broken Dockerfile | rebuild; `selftest` names the missing piece |
| `Claude auth` ✗ in selftest | `.env` not passed | `docker compose run` reads `.env` automatically; `docker-run.sh` uses `--env-file` |
| `Claude Code too old, missing --json-schema` | image has an old CLI | `CLAUDE_CODE_VERSION=… docker compose build` |
| every company `website not found` | no `SERPER_API_KEY` and a generic name | add Serper, or fill the `web` column via `import` |
| `enrich` stops with `timeout 240s` | one slow site | rerun; failures are retried, cache keeps the rest free |
| empty result, `All good.` never printed | build step failed | `docker compose build` again and read the last lines |

## What the container does not do

- **No interactive login.** Use a long-lived subscription token from
  `claude setup-token` as `CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_API_KEY`.
  Never copy a laptop's `~/.claude` into the image. With the token, runs use the
  plan's usage limits; `grizly-cost.jsonl` still shows the notional API cost and
  `GRIZLY_BUDGET` still caps each company.
- **No Google Sheets / Raynet push.** Output is CSV on the volume; import it
  wherever it belongs.
- **No scheduler of its own.** Cron or Coolify starts it; it never loops.
- **No proxy.** If the server needs an HTTP proxy, set `HTTPS_PROXY` in `.env`;
  Python's `urllib` and Node both honour it.
