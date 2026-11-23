# grizly

Company lead research for any seller, in any country:
**public business registry → company website → Claude → CSV.**

Standard library only (Python 3.11+). No `pip install`, no build.
You need `claude` (Claude Code) on PATH and either a Claude subscription token
(`CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`) or an `ANTHROPIC_API_KEY`
for the paid step. Everything else is free public data.

Docker is optional and only there for servers where you do not want to install
Node and Claude Code by hand: `Dockerfile`, `docker-compose.yml` and
`docker-run.sh` in this folder, described in [DOCKER.md](DOCKER.md).

```
pull       registry  ──►  CSV with every company in your segment          free
import     any CSV   ──►  same CSV format (countries without an adapter)   free
enrich     website + Claude ──► industry, signals, personalization, contacts  ~$0.02–0.07 / company
classify   rules from your profile ──► tags, needs, ONE offer, score 0–100  free
```

Three pieces, kept apart on purpose:

| Piece | What it holds | Where |
|---|---|---|
| **Profile** | everything about ONE seller: offers, thresholds, routing, scoring, website signals, output format, do-not-contact lists | `profiles/<seller>.toml` |
| **Source** | one public registry turned into canonical rows | `grizly/sources/<cc>_<registry>.py` |
| **Schema** | the canonical columns every step reads and writes | `grizly/schema.py` |

Running the tool for another company means writing a profile, never editing Python.
Covering another country means adding one adapter, never touching rules.

---

## Quick start

```bash
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...   # or ANTHROPIC_API_KEY=sk-ant-...
python3 grizly.py selftest                      # checks everything, costs nothing

python3 grizly.py pull --region jihomoravsky --limit 100          # default profile
python3 grizly.py enrich --in grizly.csv --limit 3 --dry-run      # websites only, no model
python3 grizly.py enrich --in grizly.csv --limit 20               # real run
```

Another seller, another country:

```bash
python3 grizly.py pull   --profile example --market no --region oslo --limit 50
python3 grizly.py enrich --profile example --in grizly.csv --limit 10
```

One company while debugging:

```bash
python3 grizly.py probe --id 25322214 --name "Holík International s.r.o." --city Zlín
```

The default profile is `$GRIZLY_PROFILE`, else `peakflow`.

---

## Registries

```bash
python3 grizly.py sources
```

| Source | Country | Free / key | Gives | Revenue |
|---|---|---|---|---|
| `cz-ares` | CZ | free, no key | name, CZ-NACE, region, legal form, founded, employee band, officers | estimate (profile) |
| `no-brreg` | NO | free, no key | name, SN code, county, legal form, founded, **exact employees**, **website**, officers | **published accounts** |
| `fr-sirene` | FR | free, no key | name, NAF, region, legal form, founded, employee band, officers, **opt-out flag** | **published (when filed)** |

Every adapter's verified limits (caps, which filters work, quirks) are in its
module docstring — read them before changing a query. Examples: ARES matches
industry codes exactly and ignores the region filter; Brreg matches prefixes;
SIRENE's region filter matches any branch, not the head office.

**Any other country** (or a list from a fair, a CRM export…):

```bash
python3 grizly.py import --in fair.csv --country DE --map name=Firma --map web=Website
```

Rows without a registry id get a stable `x-<domain>` id. Then `enrich` as usual.

---

## Profiles

A profile is a TOML file. Copy `profiles/example.toml`, then:

```bash
python3 grizly.py profile --profile acme      # validates + runs its [[examples]]
```

| Section | Purpose |
|---|---|
| `[seller]` | name, website (goes into the crawler's User-Agent), `language` of generated text, `currency` of thresholds, optional `context` for the model |
| `[output]` | `columns = "en"` (default) or `"cs"` (legacy Czech headers), `delimiter`, `encoding` |
| `[markets.<key>]` | `source`, `industry_codes` (the registry's own codes), `regions`, `legal_form_classes`, `min_employees`, `language`; `[markets] default = "<key>"` |
| `[[signals]]` | facts the model extracts from the website: `name`, `type` (boolean/integer/number/string), `description`, `quality` (counts toward `enrich_quality`) |
| `[estimate]` | revenue per employee by country and industry prefix, used only when no revenue is published; always flagged |
| `[fx]` | `source = "ecb"` (daily ECB rates, cached) or `"static"` + `rates = { NOK = 11.7 }` (units per EUR) |
| `[rules]` | `exclude_tags`, `no_offer`, `[[rules.tags]]`, `[[rules.needs]]`, `[[rules.offers]]` (first match wins) |
| `[scoring]` | `size_points`, `estimate_factor`, `cap`, `[[scoring.points]]` |
| `[[suppression]]` | do-not-contact CSV (column `company_id` or `ico`) → tag; the tag always excludes |
| `[[examples]]` | input row + expected decision; run by `profile`, `selftest` and the tests |

### Conditions (`when`)

All fields in one `when` must match (AND). Repeat a rule with the same tag for OR.
**An unknown value never passes a comparison** — missing data must not qualify
or disqualify silently. Typos in field or operator names are load errors.

| Field kind | Fields | Operators |
|---|---|---|
| number | `revenue` (published, else estimate; in profile currency), `revenue_actual`, `employees_min`, `employees_max`, `company_age_years`, `industry_code_count`, integer/number signals | `min`, `max`, `lt`, `gt`, `eq`, `known` |
| bool | `revenue_is_estimate`, boolean signals | `is`, `known` |
| text | `country`, `legal_form`, `legal_form_class`, `hq_region_code`, `hq_region`, `hq_city`, `personalization`, `officers`, `web`, `industry`, `lead_offer`, string signals… | `eq`, `in`, `present`, `known`, `prefix` |
| list | `industry_codes`, `registry_flags`, `tags`, `needs` | `any`, `none`, `prefix`, `present` |

`legal_form_class` is the same everywhere: `company`, `sole_trader`, `partnership`,
`cooperative`, `public`, `nonprofit`, `other`. Regions are ISO 3166-2 (`CZ-64`,
`NO-03`, `FR-IDF`). Industry `prefix` ignores dots and letters, so `["62"]`
matches `62100`, `62.010` and `62.01Z`.

```toml
[[rules.tags]]
tag = "TOO_SMALL"
when = { revenue = { known = false }, employees_max = { gt = 0, max = 5 } }

[[rules.offers]]
offer = "CRM"
when = { needs = { any = ["CRM"] } }
size = { basis = "revenue", min = 10_000_000, max = 1_000_000_000 }
```

Changed a threshold? `python3 grizly.py classify --in grizly.csv` re-runs the
rules on the file for free.

---

## What the tool refuses to do

1. **Invent numbers.** What the registry does not say stays empty. The revenue
   estimate is only made when no figure is published, never below
   `estimate.min_employees` (a 7-person wholesaler can turn over 30 or 300
   million), always flagged, and earns only `estimate_factor` of the size points.
2. **Trust the model.** `personalization` without a **verbatim** quote found in
   the page text is dropped; the page it came from goes to
   `personalization_source_url`. Phones and e-mails must appear on the site.
3. **Accept a namesake website.** Two distinctive name words must appear on a
   guessed site (one if the name has one) — `happy.cz` is not HAPPY LYFE THERAPEUTICS.
4. **Contact companies that objected.** A registry opt-out (France:
   `opposition_diffusion_commerciale`) becomes `DO_NOT_CONTACT` for every profile.
   Prospecting law differs per country — the profile owner is responsible for the rest.
5. **Scrape search engines or LinkedIn.** Websites are found by guessing domains
   and verifying the page (optionally Serper.dev with `SERPER_API_KEY`); robots.txt is respected.
6. **Mix failure and doubt.** `enrich_error` = the run failed; `enrich_warnings` =
   finished with reservations. `--dry-run` never sets `enrich_quality`, so a
   real run does not skip the row.

---

## Output

One row per company, key `(country, company_id)`. Contacts go to
`contacts-<file>.csv` (max 3 per company, only published ones, appended across runs).

| Column | Meaning |
|---|---|
| `company_id`, `country`, `source` | registry id (IČO, org.nr., SIREN…), ISO country, adapter |
| `industry_codes`, `industry_scheme` | registry codes (`;`-separated) and their scheme |
| `hq_region_code` | ISO 3166-2 |
| `revenue`, `revenue_currency`, `revenue_year` | published figure |
| `revenue_estimate`, `revenue_estimate_flag` | estimate, only without a published figure |
| `registry_flags` | e.g. `marketing_opt_out` |
| *signals* | whatever the profile defines |
| `personalization`, `personalization_source_url` | one sentence + the page it is proven on |
| `tags`, `needs`, `lead_offer`, `score` | decisions; `lead_offer = NONE` (or the profile's `no_offer`) = do not contact |
| `enrich_quality`, `enrich_warnings`, `enrich_error`, `enrich_cost_usd` | how the enrichment went |

Files from grizly 1.x (Czech headers, `;`, BOM) are read as they are. A profile
with `[output] columns = "cs"` writes the legacy headers back.

## Costs

Claude Code runs lean: our instruction is the system prompt, no tools, no MCP
servers, no skills, no settings or hooks, no CLAUDE.md, no saved session.
Measured 2026-11-22: fixed overhead ~1 200 tokens instead of ~40 700; a company
with 6 pages (27 000 chars) costs **~$0.07** on Sonnet, less when pages are short.

- Cap per company `GRIZLY_BUDGET` (default $0.15).
- Cache in `grizly-cache/`: the same pages and the same question cost nothing.
  The key includes the profile's prompt, so a changed profile never reuses old answers.
- `grizly-cost.jsonl`: one line per company with what was spent in that run.
- Progress is written every 10 companies; a restart resumes from the output file.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | — | subscription token for `enrich`; wins over the API key |
| `ANTHROPIC_API_KEY` | — | API key for `enrich`, if no OAuth token |
| `GRIZLY_PROFILE` | `peakflow` | default profile |
| `SERPER_API_KEY` | — | optional website search |
| `GRIZLY_MODEL` | `sonnet` | model for `claude -p` |
| `GRIZLY_WORKERS` | `3` | companies in parallel |
| `GRIZLY_BUDGET` | `0.15` | USD cap per company |
| `GRIZLY_CACHE` | `grizly-cache` | cache directory |
| `GRIZLY_LOG` | `grizly-cost.jsonl` | cost log |

## Adding a country

1. Create `grizly/sources/<cc>_<registry>.py` with a `Source` subclass
   (`grizly/sources/base.py` is the contract): `regions()`, `search()`,
   optionally `details()`, `normalize()`, `selftest()`.
2. **Verify every behaviour against the live API**, not the docs: does the
   industry filter match exactly or by prefix, does the region filter work,
   what is the page cap, where are employees, officers, revenue, opt-outs.
   Write what you found as a table in the module docstring.
3. Record trimmed responses into `tests/fixtures/` (replace people's names) and
   write mapping tests in `tests/test_sources.py`.
4. Add one line to `SOURCES` in `grizly/sources/__init__.py`.
5. `python3 gate.py` must end with `GATE: PASS`.

Candidates: UK Companies House (free key), Denmark CVR, Finland PRH, Estonia
e-Business Register, Poland KRS/REGON.

## Development

```bash
python3 gate.py      # compile, ruff, mypy --strict, unittest, profiles -> GATE: PASS / FAIL
```

ruff and mypy run through `uvx` when not installed. Tests are offline and free;
`tests/test_rules.py` proves the PeakFlow profile reproduces grizly 1.x
decisions on 5 000 random rows (the 1.x code is kept in `tests/legacy_v1_grizly.py`).

## Docker

For a server where Node and Claude Code should not be installed by hand. Build
once, then run any command; all state stays in `./data`:

```bash
cp .env.example .env                       # ANTHROPIC_API_KEY (+ optional SERPER_API_KEY)
mkdir -p data
docker compose build
docker compose run --rm grizly selftest    # free: claude, flags, key, registries
docker compose run --rm grizly pull --region jihomoravsky --limit 100
docker compose run --rm grizly enrich --in grizly.csv --limit 10
```

No compose: `./docker-run.sh enrich --in grizly.csv --limit 10`. Details,
Coolify setup, cron and troubleshooting: [DOCKER.md](DOCKER.md).

## Upgrading from 1.x

- Copy or pull the **whole folder** (`grizly/`, `profiles/`, `grizly.py`), not one file.
- Old commands keep working: `--kraj`, `--nace` and `--ico` are aliases of
  `--region`, `--industry` and `--id`.
- The PeakFlow profile writes the same Czech headers plus new columns
  (`zeme`, `kraj_kod`, `personalizace_zdroj`, `nace_pocet`, `obrat_mena`, …).
- The cache key now includes the prompt, so the first run after the upgrade
  re-asks the model once per company.
- Stricter than 1.x: a guessed website needs two distinctive name words;
  phone numbers shorter than 8 digits are dropped instead of kept unverified.
