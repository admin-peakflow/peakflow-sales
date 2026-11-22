"""Canonical record layout. One company = one row, keyed by (country, company_id).

Column names are English snake_case everywhere inside the code. A profile can
ask for legacy Czech headers on output (`[output] columns = "cs"`), which keeps
older consumers (agents, sheets) working. Input files are accepted in either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grizly.profile import Signal

IDENTITY = [
    "company_id", "country", "name", "trade_name", "web",
    "web_check", "web_check_evidence", "web_check_reason",
    "industry_codes", "industry_primary", "industry_code_count", "industry_scheme", "industry",
    "hq_region", "hq_region_code", "hq_city",
    "legal_form", "legal_form_class", "founded_on", "company_age_years", "officers",
    "registry_flags",
]
SIZE = [
    "employees_band", "employees_min", "employees_max",
    "revenue", "revenue_year", "revenue_currency", "revenue_estimate", "revenue_estimate_flag",
]
DESCRIPTION = ["short_description", "personalization", "personalization_source_url"]
DECISION = ["tags", "needs", "lead_offer", "score"]
META = ["source", "enriched_at", "enrich_quality", "enrich_warnings", "enrich_cost_usd",
        "enrich_error"]

CONTACT_COLUMNS = ["company_id", "country", "company_name", "name", "role", "phone", "email",
                   "source_url", "found_on"]

# Legal form classes every adapter maps its native codes to. Rules use these,
# so one profile works in every country.
LEGAL_FORM_CLASSES = ("company", "sole_trader", "partnership", "cooperative", "public",
                      "nonprofit", "other")

# Registry flags adapters may set. `marketing_opt_out` always excludes the company:
# it is the company's own legal objection to prospecting, not a sales preference.
FLAG_OPT_OUT = "marketing_opt_out"
TAG_DO_NOT_CONTACT = "DO_NOT_CONTACT"

ESTIMATE_FLAG = "estimate"

# Legacy Czech headers (grizly 1.x, Gatsby playbook, vault specs).
CS_COMPANY = {
    "company_id": "ico", "country": "zeme", "name": "nazev", "trade_name": "brand",
    "web": "web", "web_check": "web_overeni", "web_check_evidence": "web_overeni_dukaz",
    "web_check_reason": "web_overeni_duvod", "industry_codes": "nace",
    "industry_primary": "nace_hlavni",
    "industry_code_count": "nace_pocet", "industry_scheme": "nace_system", "industry": "obor",
    "hq_region": "kraj", "hq_region_code": "kraj_kod", "hq_city": "mesto",
    "legal_form": "pravni_forma", "legal_form_class": "pravni_forma_typ",
    "founded_on": "datum_vzniku", "company_age_years": "vek_let", "officers": "jednatele",
    "registry_flags": "rejstrik_priznaky",
    "employees_band": "zamestnanci_kat", "employees_min": "zamestnanci_min",
    "employees_max": "zamestnanci_max", "revenue": "obrat", "revenue_year": "obrat_rok",
    "revenue_currency": "obrat_mena", "revenue_estimate": "obrat_odhad",
    "revenue_estimate_flag": "obrat_odhad_priznak",
    "short_description": "popis_kratky", "personalization": "personalizace",
    "personalization_source_url": "personalizace_zdroj",
    "tags": "tagy", "needs": "potreby", "lead_offer": "lead_offer", "score": "skore",
    "source": "zdroj", "enriched_at": "enriched_dne", "enrich_quality": "enrich_quality",
    "enrich_warnings": "enrich_warnings", "enrich_cost_usd": "enrich_cost_usd",
    "enrich_error": "enrich_error",
}
CS_CONTACT = {
    "company_id": "ico", "country": "zeme", "company_name": "nazev", "name": "jmeno",
    "role": "role", "phone": "telefon", "email": "email", "source_url": "source_url",
    "found_on": "zjisteno_dne",
}

# Value vocabulary per output language. Internally always the English form.
BOOL_WORDS = {"en": ("yes", "no"), "cs": ("ano", "ne")}
ESTIMATE_WORDS = {"en": ESTIMATE_FLAG, "cs": "odhad"}


def company_columns(signals: list[Signal]) -> list[str]:
    return IDENTITY + SIZE + [s.name for s in signals] + DESCRIPTION + DECISION + META


def blank_company(signals: list[Signal]) -> dict[str, str]:
    row = dict.fromkeys(company_columns(signals), "")
    row["score"] = "0"
    return row


def key(row: dict[str, str]) -> tuple[str, str]:
    """Identity of a company across files and runs."""
    return (str(row.get("country") or "").upper(), str(row.get("company_id") or "").strip())
