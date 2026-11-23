"""Command line. `python3 grizly.py <command>` or `python3 -m grizly <command>`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from grizly import VERSION, csvio, llm, rules, sources, web
from grizly import profile as profiles
from grizly.fx import FX
from grizly.pipeline import (
    MODEL,
    WORKERS,
    Context,
    EnrichOptions,
    enrich,
    import_csv,
    pull,
    reclassify,
)
from grizly.sources.base import Query, SourceError
from grizly.util import split_arg
from grizly.validate import sanity_contacts, validate


def _ctx(args: argparse.Namespace) -> Context:
    return Context(profiles.load(args.profile))


# --------------------------------------------------------------------------- commands

def cmd_pull(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    p = ctx.profile
    market = p.market(args.market) if (args.market or p.markets) else None
    source_name = args.source or (market.source if market else "")
    if not source_name:
        raise SourceError("no source: pass --source or define [markets] in the profile")
    source = sources.get(source_name, ctx.user_agent)
    codes = split_arg(args.industry, spaces=True) or (market.industry_codes if market else [])
    if not codes:
        raise SourceError("no industry codes: pass --industry or set industry_codes in the market")
    region_names = split_arg(args.region) or (market.regions if market else [])
    query = Query(
        industry_codes=codes,
        regions=source.resolve_regions(region_names),
        legal_form_classes=market.legal_form_classes if market else ["company"],
        min_employees=args.min_employees if args.min_employees is not None
        else (market.min_employees if market else None),
        limit=args.limit or 0,
    )
    ctx.log(f"{p.name} · {source.name} · {len(codes)} industry codes"
            + (f" · {', '.join(sorted(query.regions))}" if query.regions else ""))
    rc = pull(ctx, source, query, details=not args.no_detail, out=Path(args.out))
    if rc == 0:
        ctx.log(f"Next: python3 grizly.py enrich --in {args.out}")
    return rc


def cmd_import(args: argparse.Namespace) -> int:
    mapping: dict[str, str] = {}
    for item in args.map or []:
        if "=" not in item:
            raise SourceError(f"--map expects column=Header, got '{item}'")
        k, v = item.split("=", 1)
        mapping[k.strip()] = v.strip()
    return import_csv(_ctx(args), Path(args.input), Path(args.out), mapping, args.country)


def cmd_enrich(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.exists():
        print(f"{src} not found. Run `grizly.py pull` first.", file=sys.stderr)
        return 1
    opt = EnrichOptions(model=args.model, force=args.force, use_search=not args.no_web_search,
                        dry_run=args.dry_run, workers=args.workers)
    return enrich(_ctx(args), src, Path(args.output or args.input), opt, args.limit)


def cmd_classify(args: argparse.Namespace) -> int:
    return reclassify(_ctx(args), Path(args.input), Path(args.output or args.input))


def cmd_all(args: argparse.Namespace) -> int:
    rc = cmd_pull(args)
    if rc:
        return rc
    ns = argparse.Namespace(profile=args.profile, input=args.out, output=args.out,
                            limit=args.enrich_limit, workers=args.workers, model=args.model,
                            force=args.force, no_web_search=args.no_web_search,
                            dry_run=args.dry_run)
    return cmd_enrich(ns)


def cmd_probe(args: argparse.Namespace) -> int:
    """One company: show the website, pages, what the model extracted and why."""
    ctx = _ctx(args)
    country = (args.country or ctx.profile.default_country).upper()
    company = {"company_id": args.id, "name": args.name, "hq_city": args.city,
               "country": country, "hq_region": ""}
    adapter = sources.for_country(country, ctx.user_agent)
    fetcher = web.Fetcher(ctx.user_agent, adapter.language if adapter else ctx.profile.language)
    base = args.web or web.find_website(fetcher, args.name, args.city, country)
    print(f"website: {base}")
    if not base:
        return 1
    pages = web.gather_site(fetcher, web.normalize_base(base))
    print(f"pages: {len(pages)}, chars: {sum(len(t) for t in pages.values())}")
    for url, text in pages.items():
        print(f"  {url} ({len(text)})")
    if not pages:
        return 1
    if args.dry_run:
        print("\n[dry-run] model not called.")
        return 0
    schema = llm.build_schema(ctx.profile)
    instruction = llm.build_instruction(ctx.profile, ctx.profile.language_name(country))
    res = llm.call_claude(llm.page_blob(company, pages, adapter.id_label if adapter else "id"),
                          instruction=instruction, schema=schema, model=args.model, budget=0.5)
    if res.error:
        print(f"ERROR: {res.error}", file=sys.stderr)
        return 1
    payload = res.payload
    sanity_contacts(payload, pages)
    quality, problems, quote_url = validate(payload, pages, ctx.profile)
    print("\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\nquality={quality:.2f} cost=${res.cost_usd:.4f} quote_on={quote_url or '-'}")
    for p in problems:
        print(f"  ! {p}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    print("Registry adapters (pull --source NAME):\n")
    for name, cls in sources.SOURCES.items():
        key = f", needs {cls.needs_key}" if cls.needs_key else ", free, no key"
        print(f"  {name:12} {cls.country}  {cls.industry_scheme:8} {cls.currency}{key}")
        print(f"  {'':12} {cls.provides}")
    print("\nAny other country: `grizly.py import --in list.csv --country XX --map ...`")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    """Validate a profile and run its [[examples]]. Free, offline."""
    p = profiles.load(args.profile)
    failures = run_examples(p)
    markets = ", ".join(f"{k} ({m.source})" for k, m in p.markets.items()) or "none"
    print(f"{p.path}: {p.name}, currency {p.currency}, output {p.output_columns}, "
          f"markets: {markets}")
    print(f"  {len(p.signals)} signals, {len(p.tags)} tag rules, {len(p.needs)} need rules, "
          f"{len(p.offers)} offers, {len(p.examples)} examples")
    for f in failures:
        print(f"  ✗ {f}")
    if not failures:
        print("  ✓ all examples pass")
    return 1 if failures else 0


def run_examples(p: profiles.Profile) -> list[str]:
    """Offline: static FX only, no suppression lists."""
    rates = FX(source="static", rates=p.fx_rates, cache_dir=Path(tempfile.gettempdir()))
    failures = []
    for ex in p.examples:
        row = dict(ex.row)
        row.setdefault("country", p.default_country)
        rules.ensure_estimate(row, p)
        rules.classify(row, p, rates)
        e = ex.expect
        problems = []
        if "lead_offer" in e and row["lead_offer"] != e["lead_offer"]:
            problems.append(f"lead_offer {row['lead_offer']} != {e['lead_offer']}")
        for t in e.get("tags_include") or []:
            if t not in row["tags"].split(";"):
                problems.append(f"tag {t} missing (got {row['tags'] or '-'})")
        for n in e.get("needs_include") or []:
            if n not in row["needs"].split(";"):
                problems.append(f"need {n} missing (got {row['needs'] or '-'})")
        if "score" in e and int(row["score"]) != int(e["score"]):
            problems.append(f"score {row['score']} != {e['score']}")
        if problems:
            failures.append(f"{ex.name}: " + "; ".join(problems))
    return failures


def cmd_selftest(args: argparse.Namespace) -> int:
    """Check the environment BEFORE spending money. Costs nothing."""
    ok = True

    def check(label: str, fn: Callable[[], str]) -> None:
        nonlocal ok
        try:
            print(f"  ✓ {label}: {fn()}")
        except Exception as e:
            ok = False
            print(f"  ✗ {label}: {e}")

    print(f"grizly {VERSION} — selftest\n")
    check("Python", lambda: sys.version.split()[0])
    p = profiles.load(args.profile)
    check("Profile", lambda: _profile_line(p))

    def _claude() -> str:
        path = llm.claude_bin()
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "claude --version failed").strip()[:200])
        return f"{proc.stdout.strip()} ({path})"

    def _key() -> str:
        mode = llm.auth_mode()
        if mode == "oauth":
            t = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
            if not t.startswith("sk-ant-oat"):
                raise RuntimeError("CLAUDE_CODE_OAUTH_TOKEN does not look like a token "
                                   "(make one with `claude setup-token`)")
            note = ", ANTHROPIC_API_KEY ignored" if os.environ.get("ANTHROPIC_API_KEY") else ""
            return f"OAuth subscription token ({t[:10]}…{t[-4:]}){note}"
        if mode == "api_key":
            k = os.environ["ANTHROPIC_API_KEY"]
            if not k.startswith("sk-"):
                raise RuntimeError("ANTHROPIC_API_KEY does not look like a key")
            return f"API key ({k[:7]}…{k[-4:]})"
        raise RuntimeError("neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set")

    def _flags() -> str:
        proc = subprocess.run([llm.claude_bin(), "--help"], capture_output=True, text=True,
                              timeout=60)
        missing = llm.missing_flags(proc.stdout + proc.stderr)
        if missing:
            raise RuntimeError(f"Claude Code too old, missing {', '.join(missing)}. "
                               "Update: npm i -g @anthropic-ai/claude-code")
        return "lean run supported (no tools, MCP, skills, hooks, CLAUDE.md)"

    if not args.offline:
        check("Claude Code", _claude)
        check("Claude Code flags", _flags)
        check("Claude auth", _key)
        names = split_arg(args.sources) or sorted({m.source for m in p.markets.values()})
        for name in names:
            src = sources.get(name, "grizly-selftest")
            check(f"Source {name}", src.selftest)

    def _html() -> str:
        text = web.html_to_text("<html><body><p>Řeřicha Bjørn</p><script>x</script></body></html>")
        if "Řeřicha Bjørn" not in text or "x" in text.replace("Řeřicha Bjørn", ""):
            raise RuntimeError("parser broken")
        return f"{len(text)} chars, diacritics OK"

    def _csv() -> str:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.csv"
            csvio.write_rows(path, [{"company_id": "12345678", "name": "Žluťoučký Ærø"}],
                             ["company_id", "name"], p)
            rows, _ = csvio.read_rows(path, p)
            if rows[0]["name"] != "Žluťoučký Ærø":
                raise RuntimeError("CSV loses diacritics")
            return f"roundtrip OK ({p.output_columns} headers, '{p.delimiter}', {p.encoding})"

    check("HTML -> text", _html)
    check("CSV", _csv)
    print()
    if ok:
        print("All good. Try:")
        print("  python3 grizly.py pull --limit 20")
        print("  python3 grizly.py enrich --in grizly.csv --limit 3 --dry-run")
        return 0
    print("Something is missing — fix it, or `enrich` fails or spends money for nothing.")
    return 1


def _profile_line(p: profiles.Profile) -> str:
    failures = run_examples(p)
    if failures:
        raise RuntimeError(f"{p.path.name}: " + " | ".join(failures))
    return f"{p.name} ({p.path.name}), {len(p.examples)} examples pass"


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grizly.py",
        description="Company lead research: registry -> website -> Claude -> CSV. "
                    "Standard library only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python3 grizly.py pull --region jihomoravsky,zlinsky --limit 100
  python3 grizly.py pull --profile example --market no --region oslo --limit 50
  python3 grizly.py enrich --in grizly.csv --limit 5 --dry-run
  python3 grizly.py enrich --in grizly.csv --limit 20
  python3 grizly.py import --in fair.csv --country DE --map name=Firma --map web=Website
  python3 grizly.py probe --id 25322214 --name "Holík International s.r.o." --city Zlín
""")
    p.add_argument("--version", action="version", version=f"grizly {VERSION}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default=None,
                        help="profile name or path (default: $GRIZLY_PROFILE or peakflow)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_pull(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--market", help="market from the profile (default: markets.default)")
        sp.add_argument("--source", help="registry adapter, see `sources`")
        sp.add_argument("--industry", "--nace", dest="industry",
                        help="registry industry codes, comma separated (default: market)")
        sp.add_argument("--region", "--kraj", dest="region",
                        help="regions: ISO 3166-2 or names, e.g. CZ-64,zlinsky")
        sp.add_argument("--min-employees", type=int, default=None,
                        help="where the registry filters on it (NO, FR)")
        sp.add_argument("--limit", type=int, default=0, help="max companies")
        sp.add_argument("--no-detail", action="store_true",
                        help="skip per-company calls (faster, no employees/officers/accounts)")
        sp.add_argument("--out", default="grizly.csv")

    def add_enrich_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--workers", type=int, default=WORKERS)
        sp.add_argument("--model", default=MODEL)
        sp.add_argument("--force", action="store_true", help="ignore cache and finished rows")
        sp.add_argument("--no-web-search", action="store_true",
                        help="use the `web` column only, do not look for the site")
        sp.add_argument("--dry-run", action="store_true",
                        help="fetch websites, do not call the model")

    sp = sub.add_parser("pull", parents=[common], help="list companies from a registry (free)")
    add_pull(sp)
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("import", parents=[common], help="any CSV -> grizly CSV (free)")
    sp.add_argument("--in", dest="input", required=True)
    sp.add_argument("--out", default="grizly.csv")
    sp.add_argument("--country", help="ISO 3166-1 alpha-2 for rows without one")
    sp.add_argument("--map", action="append", metavar="COLUMN=Header",
                    help="grizly column = header in your file; repeat")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("enrich", parents=[common], help="website + Claude (costs money)")
    sp.add_argument("--in", dest="input", default="grizly.csv")
    sp.add_argument("--out", dest="output", default=None, help="default: overwrite input")
    sp.add_argument("--limit", type=int, default=0)
    add_enrich_opts(sp)
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("classify", parents=[common],
                        help="re-run estimate + rules after changing the profile (free)")
    sp.add_argument("--in", dest="input", default="grizly.csv")
    sp.add_argument("--out", dest="output", default=None, help="default: overwrite input")
    sp.set_defaults(func=cmd_classify)

    sp = sub.add_parser("all", parents=[common], help="pull + enrich")
    add_pull(sp)
    sp.add_argument("--enrich-limit", type=int, default=0, help="how many to enrich (0 = all)")
    add_enrich_opts(sp)
    sp.set_defaults(func=cmd_all)

    sp = sub.add_parser("probe", parents=[common], help="one company, show what the model sees")
    sp.add_argument("--id", "--ico", dest="id", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--city", default="")
    sp.add_argument("--country", default="")
    sp.add_argument("--web", default="")
    sp.add_argument("--model", default=MODEL)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("sources", help="list registry adapters")
    sp.set_defaults(func=cmd_sources)

    sp = sub.add_parser("profile", parents=[common], help="validate a profile, run its examples")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("selftest", parents=[common], help="check the environment (free)")
    sp.add_argument("--offline", action="store_true", help="skip claude, key and registries")
    sp.add_argument("--sources", help="registries to ping (default: those in the profile)")
    sp.set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        func: Callable[[argparse.Namespace], Any] = args.func
        return int(func(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Work so far is in the CSV.", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError) as e:  # SourceError, ProfileError included
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
