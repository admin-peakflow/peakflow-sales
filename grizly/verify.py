"""Does this website really belong to THIS registered company, or only share a name?

Runs before any extraction. A wrong site is worse than none: the
personalization would describe another business and nobody would notice.

Order, cheapest first:
1. `registry` — the registry itself lists the website (e.g. Brreg `hjemmeside`).
2. `id`       — the company's registry id (IČO, org.nr., SIREN…) is printed on
                the site. Czech and EU sites must show it (imprint, footer,
                contacts), so this settles most companies without a model.
3. model      — Opus compares registry facts with the page text and returns
                `match` / `different_company` / `unsure` with a VERBATIM quote.
                `match` without a quote found in the text becomes `unsure`.

Only `registry`, `id`, `match` and `manual` (set by a human in the CSV) let the
company continue to extraction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grizly import llm
from grizly.util import norm_space

VERIFIED = frozenset({"registry", "id", "match", "manual"})
MODEL = os.environ.get("GRIZLY_VERIFY_MODEL", "opus")
BUDGET = float(os.environ.get("GRIZLY_VERIFY_BUDGET", "0.30"))
MAX_CHARS = 14_000
MIN_EVIDENCE = 10
CONTACT_HINTS = ("kontakt", "contact", "impressum", "imprint", "om-oss", "o-nas", "about",
                 "propos", "mentions", "legal")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "evidence", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["match", "different_company", "unsure"]},
        "evidence": {"type": ["string", "null"],
                     "description": "Verbatim excerpt of the page text the verdict rests on."},
        "reason": {"type": "string", "description": "One sentence: why."},
    },
}


@dataclass
class SiteCheck:
    status: str            # registry | id | match | manual | different_company | unsure | pending
    evidence: str = ""
    reason: str = ""
    cost_usd: float = 0.0
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.status in VERIFIED


def instruction(language_name: str) -> str:
    return "\n".join([
        "You audit whether a website belongs to one specific registered company, or only "
        "shares part of its name (a namesake, an unrelated business, a directory, a parked "
        "domain, a marketplace).",
        "",
        "Compare the registry record with the page text: legal name and brand, registered "
        "office (city), people (officers), what the business does versus its registry "
        "industry codes, company or VAT id, phone prefix and language.",
        "",
        "Verdicts:",
        "- match: the site clearly belongs to this legal entity (its own site, or its brand "
        "site naming this entity or its people or its town).",
        "- different_company: the site belongs to someone else.",
        "- unsure: not enough evidence either way.",
        "",
        "Be strict. Sharing one generic word (\"happy\", \"consulting\", \"stavby\") is not a "
        "match. A different town and a different field of business mean different_company.",
        "`evidence` is a VERBATIM excerpt of the page text (do not change a character). It is "
        "required for match.",
        f"Write `reason` in {language_name}, one sentence.",
        "Answer only with JSON matching the schema.",
    ])


def id_on_pages(company_id: str, texts: list[str]) -> str:
    """Snippet around the registry id if it is printed on a page, else ''.

    Digits may be split by spaces or dots (`255 82 844`), prefixed (`CZ25582844`)
    or followed by an establishment suffix (French SIRET = SIREN + 5 digits).
    """
    cid = re.sub(r"\D", "", company_id)
    if len(cid) < 6 or company_id.startswith("x-"):
        return ""
    pattern = re.compile(rf"(?<!\d){cid}(?:\d{{5}})?(?!\d)")
    for text in texts:
        squashed = re.sub(r"(?<=\d)[  .](?=\d)", "", text)
        m = pattern.search(squashed)
        if m:
            start = max(0, m.start() - 40)
            return " ".join(squashed[start:m.end() + 20].split())
    return ""


def registry_record(company: dict[str, str], id_label: str) -> str:
    fields = [
        ("Legal name", company.get("name")),
        (id_label, company.get("company_id")),
        ("Country", company.get("country")),
        ("Registered office", ", ".join(x for x in (company.get("hq_city"),
                                                    company.get("hq_region")) if x)),
        ("Officers", company.get("officers")),
        ("Industry codes", company.get("industry_codes")),
        ("Founded", company.get("founded_on")),
        ("Employees", company.get("employees_band")),
    ]
    return "\n".join(f"{k}: {v}" for k, v in fields if v)


def page_excerpt(url: str, pages: dict[str, str]) -> str:
    """Home page and contact/imprint pages first — that is where identity lives."""
    def rank(u: str) -> int:
        return 0 if u.rstrip("/") == url.rstrip("/") else (
            1 if any(h in u.lower() for h in CONTACT_HINTS) else 2)

    parts, total = [], 0
    for u in sorted(pages, key=rank):
        chunk = pages[u][: MAX_CHARS - total]
        if not chunk:
            break
        parts.append(f"## Page {u}\n{chunk}")
        total += len(chunk)
    return "\n\n".join(parts)


def check_site(company: dict[str, str], url: str, pages: dict[str, str], *, id_label: str,
               language_name: str, cache_dir: Path, model: str = MODEL,
               budget: float = BUDGET, use_model: bool = True) -> SiteCheck:
    """Verify one candidate site. `pages` must include header/footer text."""
    snippet = id_on_pages(str(company.get("company_id") or ""), list(pages.values()))
    if snippet:
        return SiteCheck("id", evidence=snippet, reason=f"{id_label} on the website")
    if not use_model:
        return SiteCheck("pending", reason="no id on the site; model not called (dry-run)")

    text = page_excerpt(url, pages)
    prompt = instruction(language_name)
    blob = (f"# Registry record\n{registry_record(company, id_label)}\n\n"
            f"# Website {url}\n{text}")
    cache = cache_dir / f"verify-{_digest(company, url, text, prompt, model)}.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return SiteCheck(data["status"], data.get("evidence", ""), data.get("reason", ""),
                             float(data.get("cost") or 0), cached=True)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    res = llm.call_claude(blob, instruction=prompt, schema=SCHEMA, model=model, budget=budget)
    if res.error:
        return SiteCheck("unsure", reason=f"verification failed: {res.error}"[:300],
                         cost_usd=res.cost_usd)
    check = interpret(res.payload, pages)
    check.cost_usd = res.cost_usd
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"status": check.status, "evidence": check.evidence,
                                 "reason": check.reason, "cost": res.cost_usd},
                                ensure_ascii=False), encoding="utf-8")
    return check


def interpret(payload: dict[str, Any], pages: dict[str, str]) -> SiteCheck:
    """Model answer -> SiteCheck. A match must be backed by a quote found in the text."""
    verdict = str(payload.get("verdict") or "unsure")
    if verdict not in ("match", "different_company", "unsure"):
        verdict = "unsure"
    evidence = str(payload.get("evidence") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    found = len(evidence) >= MIN_EVIDENCE and any(
        norm_space(evidence) in norm_space(t) for t in pages.values())
    if verdict == "match" and not found:
        return SiteCheck("unsure", evidence="",
                         reason=f"{reason} [model said match, but its quote is not on the page]")
    return SiteCheck(verdict, evidence if found else "", reason)


def _digest(company: dict[str, str], url: str, text: str, prompt: str, model: str) -> str:
    h = hashlib.sha256()
    for part in (str(company.get("country")), str(company.get("company_id")), url, model,
                 prompt, text, json.dumps(SCHEMA, sort_keys=True)):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:32]
