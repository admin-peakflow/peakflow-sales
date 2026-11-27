"""export: every grizly CSV in a folder -> three files a person works with.

    leady.csv        one row per company worth contacting, best first, contacts inline
    insolvence.csv   companies in the insolvency register (left out of leady.csv)
    log.txt          appended summary: what was found, what failed and why

Everything is rebuilt from the CSVs on every run, so it never drifts from the data.
A company that appears in several files (pull, batch, enriched, backups) counts once:
its best successful enrichment wins, the newest one on a tie.
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from grizly import csvio, sources, verify
from grizly.pipeline import Context
from grizly.schema import FLAG_INSOLVENCY, key
from grizly.util import split_cell

LEADS = "leady.csv"
INSOLVENT = "insolvence.csv"
LOG = "log.txt"
# never read as input: our own outputs, caches, the ledger (its columns look like
# an enriched file but carry no data), and backups that only repeat other files
SKIP_DIRS = {"grizly-cache", "logs", "ledger"}
SKIP_NAMES = {LEADS, INSOLVENT}

LEAD_COLUMNS = [
    "skore", "nabidka", "nazev", "ico", "kraj", "mesto", "obor", "zamestnanci",
    "obrat", "obrat_je_odhad", "web", "telefon", "email", "kontakt", "dalsi_kontakty",
    "jednatele", "potreby", "popis", "personalizace", "personalizace_zdroj", "obohaceno_dne",
]
INSOLVENT_COLUMNS = ["ico", "nazev", "kraj", "mesto", "zjisteno_dne", "zdroj"]

REASONS = {
    "website not found": "web nenalezen",
    "website belongs to another company": "web patří jiné firmě",
    "website not confirmed as the company's": "web nepotvrzen jako firemní",
    "website returned no text": "web bez textu",
}

Log = Callable[[str], None]


@dataclass
class Summary:
    files: int = 0
    companies: int = 0
    leads: int = 0
    insolvent: int = 0
    no_offer: int = 0
    excluded: Counter[str] = field(default_factory=Counter)
    failed: Counter[str] = field(default_factory=Counter)
    never_enriched: int = 0
    checked: int = 0
    unchecked: int = 0


def input_files(data: Path, out_dir: Path) -> tuple[list[Path], list[Path]]:
    """-> (company files, contact files) under `data`, recursively."""
    companies: list[Path] = []
    contacts: list[Path] = []
    for path in sorted(data.rglob("*.csv")):
        rel = path.relative_to(data)
        if set(rel.parts[:-1]) & SKIP_DIRS or ".before" in path.name:
            continue
        if path.parent.resolve() == out_dir.resolve() and path.name in SKIP_NAMES:
            continue
        (contacts if path.name.startswith("contacts-") else companies).append(path)
    return companies, contacts


def succeeded(row: dict[str, str]) -> bool:
    return (not row.get("enrich_error")
            and row.get("enrich_quality") not in (None, "", "0.00")
            and row.get("web_check") in verify.VERIFIED)


def rank(row: dict[str, str]) -> tuple[int, str, float]:
    try:
        quality = float(row.get("enrich_quality") or 0)
    except ValueError:
        quality = 0.0
    return (1 if succeeded(row) else 0, row.get("enriched_at") or "", quality)


def reason(error: str) -> str:
    base = error.split(":", 1)[0].strip() if error.startswith("unexpected") else error.strip()
    return REASONS.get(base, "jiná chyba" if base else "bez důvodu")


def export(ctx: Context, data: Path, out_dir: Path, *, check_registry: bool,
           log: Log | None = None) -> Summary:
    say = log or ctx.log
    s = Summary()
    company_files, contact_files = input_files(data, out_dir)
    s.files = len(company_files) + len(contact_files)

    best: dict[tuple[str, str], dict[str, str]] = {}
    flags: dict[tuple[str, str], set[str]] = {}
    for path in company_files:
        rows, header = csvio.read_rows(path, ctx.profile)
        if "company_id" not in header:
            continue
        for r in rows:
            if not r.get("company_id"):
                continue
            ctx.normalize(r)
            k = key(r)
            flags.setdefault(k, set()).update(split_cell(r.get("registry_flags")))
            if k not in best or rank(r) > rank(best[k]):
                best[k] = r
    s.companies = len(best)

    contacts: dict[tuple[str, str], list[dict[str, str]]] = {}
    for path in contact_files:
        rows, _ = csvio.read_rows(path, ctx.profile, contacts=True)
        for c in rows:
            if not c.get("country"):
                c["country"] = ctx.profile.default_country
            lst = contacts.setdefault(key(c), [])
            if (c.get("phone"), c.get("email")) not in {(x.get("phone"), x.get("email"))
                                                        for x in lst}:
                lst.append(c)

    good = [k for k, r in best.items() if succeeded(r)]
    if check_registry and good:
        say(f"Checking {len(good)} companies in the registry (insolvency)…")
        for i, k in enumerate(good, 1):
            adapter = sources.for_country(k[0], ctx.user_agent)
            live = adapter.registry_flags(k[1]) if adapter else None
            if live is None:
                s.unchecked += 1
            else:
                s.checked += 1
                flags[k] = (flags.get(k, set()) - {FLAG_INSOLVENCY}) | set(live)
            if i % 250 == 0:
                say(f"  {i}/{len(good)}")

    today = datetime.now().strftime("%Y-%m-%d")
    leads: list[dict[str, str]] = []
    insolvent: list[dict[str, str]] = []
    for k, r in best.items():
        r["registry_flags"] = ";".join(sorted(flags.get(k, set())))
        ctx.finish(r)
        if FLAG_INSOLVENCY in flags.get(k, set()):
            insolvent.append(insolvent_row(r, today))
            continue
        if not succeeded(r):
            if r.get("enrich_error") or r.get("enrich_quality") == "0.00":
                s.failed[reason(str(r.get("enrich_error") or ""))] += 1
            else:
                s.never_enriched += 1
            continue
        hit = set(split_cell(r.get("tags"))) & ctx.profile.exclude_tags
        if hit:
            s.excluded[sorted(hit)[0]] += 1
            continue
        if r.get("lead_offer") in ("", ctx.profile.no_offer):
            s.no_offer += 1
            continue
        leads.append(lead_row(r, contacts.get(k, [])))

    leads.sort(key=lambda x: (-int(x["skore"] or 0), x["nazev"]))
    insolvent.sort(key=lambda x: x["nazev"])
    s.leads, s.insolvent = len(leads), len(insolvent)
    out_dir.mkdir(parents=True, exist_ok=True)
    write(out_dir / LEADS, leads, LEAD_COLUMNS, ctx)
    write(out_dir / INSOLVENT, insolvent, INSOLVENT_COLUMNS, ctx)
    append_log(out_dir / LOG, s, data)
    return s


def lead_row(r: dict[str, str], contacts: list[dict[str, str]]) -> dict[str, str]:
    reachable = [c for c in contacts if c.get("phone") or c.get("email")]
    first = reachable[0] if reachable else {}
    rest = [c for c in contacts if c is not first]
    revenue, estimate = r.get("revenue") or "", ""
    if not revenue and r.get("revenue_estimate"):
        revenue, estimate = r["revenue_estimate"], "ano"
    return {
        "skore": r.get("score") or "0",
        "nabidka": r.get("lead_offer") or "",
        "nazev": r.get("name") or "",
        "ico": r.get("company_id") or "",
        "kraj": r.get("hq_region") or "",
        "mesto": r.get("hq_city") or "",
        "obor": r.get("industry") or "",
        "zamestnanci": r.get("employees_band") or "",
        "obrat": revenue,
        "obrat_je_odhad": estimate,
        "web": r.get("web") or "",
        "telefon": first.get("phone") or "",
        "email": first.get("email") or "",
        "kontakt": person(first),
        "dalsi_kontakty": " | ".join(contact_text(c) for c in rest),
        "jednatele": r.get("officers") or "",
        "potreby": (r.get("needs") or "").replace(";", ", "),
        "popis": r.get("short_description") or "",
        "personalizace": r.get("personalization") or "",
        "personalizace_zdroj": r.get("personalization_source_url") or "",
        "obohaceno_dne": (r.get("enriched_at") or "")[:10],
    }


def person(c: dict[str, str]) -> str:
    return " – ".join(x for x in (c.get("name") or "", c.get("role") or "") if x)


def contact_text(c: dict[str, str]) -> str:
    reach = ", ".join(x for x in (c.get("phone") or "", c.get("email") or "") if x)
    who = person(c)
    return f"{who}: {reach}" if who and reach else who or reach


def insolvent_row(r: dict[str, str], today: str) -> dict[str, str]:
    return {"ico": r.get("company_id") or "", "nazev": r.get("name") or "",
            "kraj": r.get("hq_region") or "", "mesto": r.get("hq_city") or "",
            "zjisteno_dne": today, "zdroj": "insolvenční rejstřík (ARES)"}


def write(path: Path, rows: list[dict[str, str]], columns: list[str], ctx: Context) -> None:
    """Fixed Czech headers, the profile's delimiter and encoding, atomic."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding=ctx.profile.encoding) as f:
        w = csv.writer(f, delimiter=ctx.profile.delimiter)
        w.writerow(columns)
        for r in rows:
            w.writerow([r.get(c, "") for c in columns])
    tmp.replace(path)


def n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


def append_log(path: Path, s: Summary, data: Path) -> None:
    def row(label: str, count: int, note: str = "") -> str:
        return f"{label + ':':33}{n(count):>7}{note}"

    lines = [
        f"=== {datetime.now():%Y-%m-%d %H:%M} export z {data}",
        f"Prošlo {n(s.files)} souborů, {n(s.companies)} různých firem.",
        row("Leady", s.leads, f"  -> {LEADS}"),
        row("V insolvenci", s.insolvent, f"  -> {INSOLVENT}"),
        row("Nesedí na nabídku", s.no_offer),
    ]
    lines += [row(f"Vyřazeno pravidlem {tag}", c) for tag, c in s.excluded.most_common()]
    lines.append(row("Nepovedlo se obohatit", sum(s.failed.values())))
    lines += [row(f"  {why}", c) for why, c in s.failed.most_common()]
    lines.append(row("Jen staženo, zatím neobohaceno", s.never_enriched))
    if s.checked or s.unchecked:
        lines.append(f"Insolvence ověřena v rejstříku u {n(s.checked)} firem"
                     + (f", u {n(s.unchecked)} se nepodařilo" if s.unchecked else "") + ".")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")
