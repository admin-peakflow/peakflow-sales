"""Steps: pull (registry -> CSV), import (any CSV -> CSV), enrich (web + Claude),
classify (re-run the rules). Each step reads and writes the canonical schema.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from grizly import VERSION, csvio, llm, rules, sources, verify, web
from grizly.fx import FX
from grizly.profile import Profile
from grizly.schema import CONTACT_COLUMNS, company_columns, key
from grizly.sources.base import Query, Source
from grizly.util import fold
from grizly.validate import sanity_contacts, validate

Log = Callable[[str], None]


def echo(msg: str) -> None:
    """print + flush: progress must reach a log file at once, not at exit."""
    print(msg, flush=True)

MODEL = os.environ.get("GRIZLY_MODEL", "sonnet")
WORKERS = int(os.environ.get("GRIZLY_WORKERS", "3"))
BUDGET_PER_COMPANY = float(os.environ.get("GRIZLY_BUDGET", "0.15"))
CACHE_DIR = Path(os.environ.get("GRIZLY_CACHE", "grizly-cache"))
LOG_FILE = Path(os.environ.get("GRIZLY_LOG", "grizly-cost.jsonl"))


@dataclass
class Context:
    profile: Profile
    cache_dir: Path = CACHE_DIR
    log_file: Path = LOG_FILE
    log: Log = echo
    fx: FX = field(init=False)
    suppressor: rules.Suppressor = field(init=False)

    def __post_init__(self) -> None:
        self.fx = FX(source=self.profile.fx_source, rates=self.profile.fx_rates,
                     cache_dir=self.cache_dir)
        self.suppressor = rules.Suppressor(self.profile)
        for missing in self.suppressor.missing:
            self.log(f"  ! suppression list {missing} not found — skipped")

    @property
    def user_agent(self) -> str:
        contact = f"; +{self.profile.website}" if self.profile.website else ""
        return f"Mozilla/5.0 (compatible; GrizlyLeadgen/{VERSION}{contact})"

    def columns(self) -> list[str]:
        return company_columns(self.profile.signals)

    def finish(self, row: dict[str, str]) -> dict[str, str]:
        """Estimate (if no published revenue) + rules. Safe to call repeatedly."""
        rules.ensure_estimate(row, self.profile)
        return rules.classify(row, self.profile, self.fx, self.suppressor)

    def normalize(self, row: dict[str, str]) -> None:
        """Fill country and derived columns on rows from older files or imports."""
        if not row.get("country"):
            row["country"] = self.profile.default_country
        adapter = sources.for_country(row["country"], self.user_agent)
        if adapter:
            adapter.normalize(row)


# --------------------------------------------------------------------------- pull

def pull(ctx: Context, source: Source, query: Query, *, details: bool, out: Path) -> int:
    """Registry -> CSV. Free (registries are public), no API key."""
    companies: dict[tuple[str, str], dict[str, str]] = {}
    for row in source.search(query, ctx.log):
        k = key(row)
        if k in companies:  # same company under two codes: keep one row, all codes
            have = companies[k]
            codes = sorted((set(have["industry_codes"].split(";")) |
                            set(row["industry_codes"].split(";"))) - {""})
            have["industry_codes"] = ";".join(codes)
            continue
        companies[k] = row
        if query.limit and len(companies) >= query.limit:
            break
    if not companies:
        ctx.log("Nothing found. Check industry codes and region.")
        return 1
    selected = list(companies.values())
    if details and source.has_details:
        ctx.log(f"Details for {len(selected)} companies from {source.name}…")
        for i, c in enumerate(selected, 1):
            c.update(source.details(c))
            if i % 25 == 0:
                ctx.log(f"  {i}/{len(selected)}")
    for c in selected:
        ctx.finish(c)
    csvio.write_rows(out, selected, ctx.columns(), ctx.profile)
    _report_fx(ctx)
    ctx.log(f"\n{len(selected)} companies -> {out}")
    return 0


# --------------------------------------------------------------------------- import

def import_csv(ctx: Context, src: Path, out: Path, mapping: dict[str, str],
               country: str | None) -> int:
    """Any CSV (export from a CRM, a list from a trade fair…) -> canonical CSV.

    `mapping` is {canonical column: header in the source file}. Rows without an
    id get a stable one from the website domain or the name, prefixed `x-`.
    """
    raw_rows, header = csvio.read_raw(src)
    missing = [h for h in mapping.values() if h not in header]
    if missing:
        ctx.log(f"Columns not in {src.name}: {', '.join(missing)} (have: {', '.join(header)})")
        return 1
    unknown = [c for c in mapping if c not in ctx.columns()]
    if unknown:
        ctx.log(f"Not grizly columns: {', '.join(unknown)}")
        return 1
    back = csvio.reverse_aliases(ctx.profile)
    out_rows = []
    for raw in raw_rows:
        r = csvio.canonical(raw, ctx.profile, back)
        row = dict.fromkeys(ctx.columns(), "")
        row.update({k: v for k, v in r.items() if k in row})
        for canon, header_name in mapping.items():
            row[canon] = raw.get(header_name, "")
        row.update(csvio.canonical({k: row[k] for k in row}, ctx.profile, {}))
        if country and not row.get("country"):
            row["country"] = country.upper()
        if not row.get("name"):
            continue
        if not row.get("company_id"):
            row["company_id"] = synthetic_id(row)
        row["source"] = row.get("source") or f"import:{src.name}"
        ctx.normalize(row)
        out_rows.append(ctx.finish(row))
    csvio.write_rows(out, out_rows, ctx.columns(), ctx.profile)
    _report_fx(ctx)
    ctx.log(f"{len(out_rows)} companies -> {out}")
    return 0


def synthetic_id(row: dict[str, str]) -> str:
    web_host = re.sub(r"^https?://(www\.)?", "", str(row.get("web") or "")).split("/")[0]
    basis = web_host or re.sub(r"[^a-z0-9]+", "-", fold(str(row.get("name") or "").lower()))
    return f"x-{basis.strip('-')}"


# --------------------------------------------------------------------------- classify

def reclassify(ctx: Context, src: Path, out: Path) -> int:
    """Re-run estimate + rules on a file, e.g. after changing thresholds. Free."""
    rows, header = csvio.read_rows(src, ctx.profile)
    for r in rows:
        ctx.normalize(r)
        ctx.finish(r)
    csvio.write_rows(out, rows, csvio.merge_columns(header, ctx.columns()), ctx.profile)
    _report_fx(ctx)
    offers: dict[str, int] = {}
    for r in rows:
        offers[r["lead_offer"]] = offers.get(r["lead_offer"], 0) + 1
    ctx.log(f"{len(rows)} companies -> {out}: " +
            ", ".join(f"{k} {v}" for k, v in sorted(offers.items())))
    return 0


# --------------------------------------------------------------------------- enrich

@dataclass
class EnrichOptions:
    model: str = MODEL
    force: bool = False
    use_search: bool = True
    dry_run: bool = False
    budget: float = BUDGET_PER_COMPANY
    workers: int = WORKERS
    verify_model: str = verify.MODEL
    verify_budget: float = verify.BUDGET
    max_candidates: int = 3


@dataclass
class Enriched:
    row: dict[str, str]
    contacts: list[dict[str, str]] = field(default_factory=list)
    spent_usd: float = 0.0  # money spent in THIS run (0 for a cache hit)


def enrich_one(ctx: Context, company: dict[str, str], opt: EnrichOptions) -> Enriched:
    """Enrich one company. Never raises — errors go into `enrich_error`."""
    row = dict(company)
    cid, country = str(company.get("company_id") or ""), str(company.get("country") or "")
    adapter = sources.for_country(country, ctx.user_agent)
    language = adapter.language if adapter else ctx.profile.language_for(country)
    fetcher = web.Fetcher(ctx.user_agent, language)
    row["enriched_at"] = date.today().isoformat()
    row["enrich_error"] = ""
    for col in ("enrich_quality", "enrich_warnings", "enrich_cost_usd"):
        row.setdefault(col, "")

    id_label = adapter.id_label if adapter else "id"
    found = find_verified_site(ctx, row, fetcher, opt, id_label)
    if found.base is None:
        row["web"] = ""
        row["enrich_error"] = found.error
        row["enrich_quality"] = "0.00"
        return Enriched(row, spent_usd=found.spent)
    base, pages = found.base, found.pages
    row["web"] = base

    if opt.dry_run:
        ctx.log(f"    [dry-run] {cid} {base} [{row['web_check']}]: {len(pages)} pages, "
                f"{sum(len(t) for t in pages.values())} chars")
        for url, text in pages.items():
            ctx.log(f"      {url} — {len(text)} chars")
        # enrich_quality stays EMPTY on purpose: otherwise the row looks done and
        # the next real run skips it.
        row["enrich_quality"] = ""
        row["enrich_warnings"] = f"dry-run: website {row['web_check']}, extraction not called"
        return Enriched(row)

    schema = llm.build_schema(ctx.profile)
    instruction = llm.build_instruction(ctx.profile, ctx.profile.language_name(country))
    cache = llm.cache_path(ctx.cache_dir, company, pages, llm.prompt_digest(instruction, schema))
    res: llm.ClaudeResult | None = None
    if cache.exists() and not opt.force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            res = llm.ClaudeResult(payload=data["payload"], cost_usd=float(data.get("cost") or 0),
                                   cached=True)
        except (json.JSONDecodeError, KeyError, TypeError):
            res = None  # corrupt cache = no cache
    if res is None:
        res = llm.call_claude(llm.page_blob(company, pages, id_label), instruction=instruction,
                              schema=schema, model=opt.model, budget=opt.budget)
        # Written on every successful call, --force included; otherwise a forced
        # run would leave the old answer for the next one.
        if not res.error:
            ctx.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"payload": res.payload, "cost": res.cost_usd},
                                        ensure_ascii=False), encoding="utf-8")

    if res.error:
        row["enrich_error"] = res.error
        row["enrich_quality"] = "0.00"
        log_cost(ctx.log_file, row, res.cost_usd, 0.0, res.error)
        return Enriched(row, spent_usd=found.spent + res.cost_usd)

    payload = res.payload
    sanity_contacts(payload, pages)
    quality, problems, quote_url = validate(payload, pages, ctx.profile)
    apply_payload(row, payload, ctx.profile)
    row["personalization_source_url"] = quote_url if row.get("personalization") else ""
    row["enrich_quality"] = f"{quality:.2f}"
    # The row keeps what the answer cost when it was made; the run log and the
    # run total only count money spent now (a cache hit costs nothing).
    row["enrich_cost_usd"] = f"{res.cost_usd:.4f}"
    spent = 0.0 if res.cached else res.cost_usd
    # enrich_error = run failed, enrich_warnings = finished with reservations.
    # Mixing them makes the real failures impossible to find in 100 rows.
    row["enrich_warnings"] = "; ".join(problems)[:400] if problems else ""
    note = "cache" if res.cached else None
    log_cost(ctx.log_file, row, spent, quality, row["enrich_warnings"] or note)

    contacts = []
    for c in payload.get("contacts") or []:
        contacts.append({
            "company_id": cid, "country": country, "company_name": str(company.get("name") or ""),
            "name": str(c.get("name") or ""), "role": str(c.get("role") or ""),
            "phone": str(c.get("phone") or ""), "email": str(c.get("email") or ""),
            "source_url": base, "found_on": date.today().isoformat(),
        })
    return Enriched(row, contacts, found.spent + spent)


@dataclass
class FoundSite:
    base: str | None
    pages: dict[str, str] = field(default_factory=dict)
    error: str = ""
    spent: float = 0.0


def find_verified_site(ctx: Context, row: dict[str, str], fetcher: web.Fetcher,
                       opt: EnrichOptions, id_label: str) -> FoundSite:
    """First candidate site that passes the audit (`grizly.verify`). Fills the row's
    `web_check*` columns. No extraction happens before this says yes."""
    given = str(row.get("web") or "").strip()
    prior = str(row.get("web_check") or "")
    if given:
        candidates = [web.normalize_base(given)]
    elif opt.use_search:
        candidates = web.website_candidates(fetcher, str(row.get("name") or ""),
                                            str(row.get("hq_city") or ""),
                                            str(row.get("country") or ""),
                                            limit=opt.max_candidates)
    else:
        candidates = []
    if not candidates:
        row["web_check"], row["web_check_evidence"], row["web_check_reason"] = "", "", ""
        return FoundSite(None, error="website not found")

    rejected: list[str] = []
    spent = 0.0
    last = ""
    for base in candidates:
        chrome: dict[str, str] = {}
        pages = web.gather_site(fetcher, base, chrome=chrome)
        if not pages:
            rejected.append(f"{base}: no text")
            continue
        # A search hit whose domain does not look like the name must print the
        # registry id: free to check, and it keeps the paid audit off random hits.
        id_only = not given and not web.domain_matches_name(base, str(row.get("name") or ""))
        if given and prior in verify.VERIFIED:
            check = verify.SiteCheck(prior, row.get("web_check_evidence", ""),
                                     row.get("web_check_reason", ""))
        else:
            check = verify.check_site(
                row, base, chrome, id_label=id_label,
                language_name=ctx.profile.language_name(str(row.get("country") or "")),
                cache_dir=ctx.cache_dir, model=opt.verify_model, budget=opt.verify_budget,
                use_model=not opt.dry_run and not id_only)
            if id_only and not check.ok:
                rejected.append(f"{base}: {id_label} not on the site")
                last = last or "unsure"
                continue
            if check.cost_usd and not check.cached:
                spent += check.cost_usd
                log_cost(ctx.log_file, row, check.cost_usd, 0.0, f"verify {base}: {check.status}")
        last = check.status
        if check.ok or (opt.dry_run and check.status == "pending"):
            row["web_check"], row["web_check_evidence"] = check.status, check.evidence[:300]
            row["web_check_reason"] = check.reason[:300]
            return FoundSite(base, pages, spent=spent)
        rejected.append(f"{base}: {check.status} — {check.reason}")

    row["web_check"] = last or "no_text"
    row["web_check_evidence"] = ""
    row["web_check_reason"] = " | ".join(rejected)[:400]
    if last == "different_company":
        error = "website belongs to another company"
    elif last == "unsure":
        error = "website not confirmed as the company's"
    else:
        error = "website returned no text"
    return FoundSite(None, error=error, spent=spent)


def apply_payload(row: dict[str, str], payload: dict[str, Any], profile: Profile) -> None:
    for col in ("industry", "short_description", "personalization", "trade_name"):
        if payload.get(col):
            row[col] = str(payload[col])
    for s in profile.signals:
        val = payload.get(s.name)
        if s.type == "boolean" and isinstance(val, bool):
            row[s.name] = "yes" if val else "no"
        elif (s.type in ("integer", "number") and not isinstance(val, bool)
              and isinstance(val, int if s.type == "integer" else (int, float))):
            row[s.name] = str(val)
        elif s.type == "string" and isinstance(val, str) and val.strip():
            row[s.name] = val.strip()


def enrich(ctx: Context, src: Path, out: Path, opt: EnrichOptions, limit: int) -> int:
    """Website + Claude. This is where money is spent: hence --limit and --dry-run."""
    rows, header = csvio.read_rows(src, ctx.profile)
    columns = csvio.merge_columns(header, ctx.columns())
    for r in rows:
        ctx.normalize(r)

    done: dict[tuple[str, str], dict[str, str]] = {}
    # Resume reads the TARGET file, even when it is the input. Without this every
    # run starts from zero and --limit enriches the same first N forever.
    if out.exists() and not opt.force:
        done_rows, _ = csvio.read_rows(out, ctx.profile)
        for r in done_rows:
            ctx.normalize(r)
        # Failures (quality 0.00) are retried: a missing website is no reason to
        # drop a company for good.
        # ...and only rows whose website passed the audit count as done. Rows enriched
        # before the audit existed get audited now (extraction comes from the cache).
        done = {key(r): r for r in done_rows
                if r.get("company_id") and r.get("enrich_quality") not in (None, "", "0.00")
                and r.get("web_check") in verify.VERIFIED}
        if done:
            ctx.log(f"Resuming: {len(done)} companies already enriched.")

    todo = [r for r in rows if r.get("company_id") and key(r) not in done]
    if limit:
        todo = todo[:limit]
    if not todo:
        ctx.log("Nothing to do.")
        return 0

    mode = "DRY-RUN (nothing sent to the model)" if opt.dry_run else f"model {opt.model}"
    ctx.log(f"Enriching {len(todo)} companies, {mode}, {opt.workers} in parallel\n")

    # Finished rows must be in `results` from the start, otherwise a flush would
    # overwrite them with the un-enriched input version.
    results: dict[tuple[str, str], dict[str, str]] = dict(done)
    contacts: list[dict[str, str]] = []
    total_cost = 0.0
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=max(1, opt.workers)) as pool:
        futures = {pool.submit(enrich_one, ctx, c, opt): c for c in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            company = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = Enriched(dict(company))
                res.row["enrich_error"] = f"unexpected error: {e}"[:400]
                res.row["enrich_quality"] = "0.00"
            row = res.row
            results[key(company)] = row
            contacts.extend(res.contacts)
            total_cost += res.spent_usd
            if row.get("enrich_error"):
                fail += 1
            elif row.get("enrich_quality"):
                ok += 1
            note = str(row.get("enrich_error") or row.get("enrich_warnings") or "")
            ctx.log(f"  [{i}/{len(todo)}] {str(company.get('name', ''))[:36]:36} "
                    f"q={row.get('enrich_quality') or '-':>5} {note[:56]}")
            if i % 10 == 0:
                _flush(ctx, rows, results, columns, out)
                if contacts and not opt.dry_run:  # an interrupted run keeps them
                    _append_contacts(ctx, out.with_name("contacts-" + out.name), contacts)
                    contacts = []

    _flush(ctx, rows, results, columns, out)
    if contacts and not opt.dry_run:
        _append_contacts(ctx, out.with_name("contacts-" + out.name), contacts)
    _report_fx(ctx)
    ctx.log(f"\ndone: {ok} enriched, {fail} failed, ${total_cost:.3f} -> {out}")
    if total_cost and (ok + fail):
        ctx.log(f"cost per company: ${total_cost / (ok + fail):.3f}")
    return 0


def _flush(ctx: Context, rows: list[dict[str, str]],
           results: dict[tuple[str, str], dict[str, str]], columns: list[str], out: Path) -> None:
    merged = [results.get(key(r), r) for r in rows]
    for r in merged:
        ctx.finish(r)
    csvio.write_rows(out, merged, columns, ctx.profile)


def _append_contacts(ctx: Context, path: Path, contacts: list[dict[str, str]]) -> None:
    """Appended, not overwritten — a second run must not delete the first run's contacts."""
    existing: list[dict[str, str]] = []
    if path.exists():
        existing, _ = csvio.read_rows(path, ctx.profile, contacts=True)
    seen = {(r.get("company_id"), r.get("phone"), r.get("email")) for r in existing}
    for c in contacts:
        k = (c.get("company_id"), c.get("phone"), c.get("email"))
        if k not in seen:
            existing.append(c)
            seen.add(k)
    csvio.write_rows(path, existing, CONTACT_COLUMNS, ctx.profile, contacts=True)
    ctx.log(f"contacts -> {path} ({len(existing)} total)")


def log_cost(path: Path, row: dict[str, str], cost: float, quality: float,
             note: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "country": row.get("country"), "company_id": row.get("company_id"),
            "cost_usd": round(cost, 5), "quality": round(quality, 2), "note": note,
        }, ensure_ascii=False) + "\n")


def _report_fx(ctx: Context) -> None:
    for w in ctx.fx.warnings:
        ctx.log(f"  ! {w}")
    ctx.fx.warnings.clear()
