# grizly — context for Claude

Lead research for any seller in any country: registry → website → `claude -p`
→ CSV. Standard library only, Python 3.11+. Read `README.md` for usage.

## Layout

```
grizly.py              compatibility entry point (python3 grizly.py …)
grizly/
  cli.py               argparse, commands
  pipeline.py          pull / import / enrich / classify
  profile.py           TOML seller profile: load + strict validation
  rules.py             estimate, facts, conditions, classify, score
  schema.py            canonical columns + legacy Czech aliases
  csvio.py             read any dialect/header set, write the profile's dialect
  web.py               find site, fetch pages, html -> text
  llm.py               schema + instruction from profile, lean `claude -p`
  validate.py          quote and contact checks against page text
  fx.py                ECB rates for thresholds
  sources/             one module per registry (cz_ares, no_brreg, fr_sirene)
profiles/              peakflow.toml (default), example.toml
tests/                 offline, fixtures from live APIs, legacy_v1_grizly.py = 1.x oracle
gate.py                compile + ruff + mypy --strict + tests + profiles
```

## Rules that must not be bypassed

1. **Nothing is guessed.** Unknown stays empty; unknown never passes a rule
   comparison. The revenue estimate is flagged, made only without published
   revenue and never below `estimate.min_employees`.
2. **No seller specifics in Python.** Offers, thresholds, tags, regions that
   score, signals, output format: profile only. If you need a new kind of
   condition, add an operator or fact in `profile.py` + `rules.py`, not a
   special case.
3. **`personalization` without a verbatim quote found in the pages is dropped.**
   Phones and e-mails must be on the pages.
4. **`enrich_error` ≠ `enrich_warnings`.** Failure vs. reservation.
5. **`--dry-run` never sets `enrich_quality`.**
6. **`marketing_opt_out` → `DO_NOT_CONTACT`, always excluded**, whatever the profile says.
7. **`claude -p` runs lean** (`llm.call_claude`): system prompt replaced, no
   tools, MCP, skills, settings, CLAUDE.md, session. Removing a flag multiplies
   the cost per company (measured 40 700 vs 1 200 tokens of overhead).
8. **Registry behaviour is verified against production, not docs.** Each
   adapter documents its limits in the module docstring. Change a query only
   after re-checking the live API, and update the table.

## Done = proof

Nothing is done, deployed or handed over without `python3 gate.py` ending in
`GATE: PASS`. Fix code, never the test. The PeakFlow profile must keep matching
grizly 1.x in `tests/test_rules.py` unless a rule change is intended — then
change the profile and the oracle comparison together, and say so.

## Test without spending

`python3 grizly.py selftest` (Python, claude + flags, key, registries, parser, CSV)
and `enrich --dry-run` cost nothing. `probe` costs one model call (~$0.07).

## Vault references (PeakFlow)

Field spec: `10 Sales/(C) Grizly analýza — specifikace dat` · thresholds and
routing: `10 Sales/(C) Sales proces — ERP, CRM, Second Brain, automatizace` §3 ·
canonical names: `10 Sales/(C) Sales engine — rámec a datová specifikace` §4.1.
