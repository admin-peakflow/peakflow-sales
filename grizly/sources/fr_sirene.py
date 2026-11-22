"""France — API Recherche d'entreprises (recherche-entreprises.api.gouv.fr), free, no key.
SIRENE (INSEE) + RNE data, run by DINUM.

Verified against production (2026-11-22):

| What                        | Behaviour                                                   |
|-----------------------------|-------------------------------------------------------------|
| `activite_principale`       | EXACT NAF codes (`46.11Z`), comma list OK, no prefix        |
| `region`                    | INSEE codes, comma list OK, but matches ANY establishment,  |
|                             | not the head office -> narrowed again on the client         |
| `nature_juridique`          | exact 4-digit INSEE codes; the API rejects unknown ones     |
|                             | (e.g. `5498`, `5720`) and lists the valid set in the error  |
| `tranche_effectif_salarie`  | INSEE size bands, server-side                               |
| Paging                      | `per_page` 1–25; `page * per_page` <= 10 000                |
| Rate limit                  | 7 requests / second per IP                                  |
| Officers                    | `dirigeants` in search results (persons and legal entities) |
| Revenue                     | `finances.{year}.ca` when published — real figure, EUR      |
| Opt-out                     | `opposition_diffusion_commerciale` = company objects to     |
|                             | commercial use -> registry flag `marketing_opt_out`         |
| Website                     | not provided                                                |
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Iterator
from typing import Any

from grizly.schema import FLAG_OPT_OUT, blank_company
from grizly.sources.base import Log, Query, Source, SourceError, person_name, title_name
from grizly.util import age_years

SEARCH = "https://recherche-entreprises.api.gouv.fr/search"
PER_PAGE = 25
CAP = 10_000
DELAY = 0.2  # 5 req/s, under the 7 req/s limit

# INSEE region -> (ISO 3166-2, name). Valid INSEE codes taken from the API error message.
REGIONS: dict[str, tuple[str, str]] = {
    "11": ("FR-IDF", "Île-de-France"), "24": ("FR-CVL", "Centre-Val de Loire"),
    "27": ("FR-BFC", "Bourgogne-Franche-Comté"), "28": ("FR-NOR", "Normandie"),
    "32": ("FR-HDF", "Hauts-de-France"), "44": ("FR-GES", "Grand Est"),
    "52": ("FR-PDL", "Pays de la Loire"), "53": ("FR-BRE", "Bretagne"),
    "75": ("FR-NAQ", "Nouvelle-Aquitaine"), "76": ("FR-OCC", "Occitanie"),
    "84": ("FR-ARA", "Auvergne-Rhône-Alpes"), "93": ("FR-PAC", "Provence-Alpes-Côte d'Azur"),
    "94": ("FR-20R", "Corse"), "01": ("FR-971", "Guadeloupe"), "02": ("FR-972", "Martinique"),
    "03": ("FR-973", "Guyane"), "04": ("FR-974", "La Réunion"), "06": ("FR-976", "Mayotte"),
}
INSEE_BY_ISO = {iso: insee for insee, (iso, _) in REGIONS.items()}

# INSEE tranche d'effectif salarié -> (min, max). "NN" = not an employer / unknown.
TRANCHES: dict[str, tuple[int, int | None]] = {
    "00": (0, 0), "01": (1, 2), "02": (3, 5), "03": (6, 9), "11": (10, 19), "12": (20, 49),
    "21": (50, 99), "22": (100, 199), "31": (200, 249), "32": (250, 499), "41": (500, 999),
    "42": (1000, 1999), "51": (2000, 4999), "52": (5000, 9999), "53": (10000, None),
}

# Valid commercial-company codes (54xx SARL, 55xx/56xx SA, 57xx SAS, 58xx SE),
# exactly as accepted by the API on 2026-11-22.
COMPANY_CODES = [
    "5410", "5415", "5422", "5426", "5430", "5431", "5432", "5442", "5443", "5451", "5453",
    "5454", "5455", "5458", "5459", "5460", "5470", "5485", "5499", "5505", "5510", "5515",
    "5520", "5522", "5525", "5530", "5531", "5532", "5542", "5543", "5546", "5547", "5548",
    "5551", "5552", "5553", "5554", "5555", "5558", "5559", "5560", "5570", "5585", "5599",
    "5605", "5610", "5615", "5620", "5622", "5625", "5630", "5631", "5632", "5642", "5643",
    "5646", "5647", "5648", "5651", "5652", "5653", "5654", "5655", "5658", "5659", "5660",
    "5670", "5685", "5699", "5710", "5770", "5785", "5800",
]
QUERY_FORMS = {"company": COMPANY_CODES, "sole_trader": ["1000"],
               "partnership": ["5202", "5203", "5306", "5307", "5308", "5309", "5310", "5370",
                               "5385"]}


def form_class(code: str) -> str:
    if not code:
        return ""
    if code == "1000":
        return "sole_trader"
    if code[:2] in ("52", "53"):
        return "partnership"
    if code[:2] == "51":
        return "cooperative"
    if code[:1] == "5":
        return "company"
    if code[:1] in ("4", "7"):
        return "public"
    if code[:1] in ("8", "9"):
        return "nonprofit"
    return "other"


def band(lo: int, hi: int | None) -> str:
    if hi is None:
        return f"{lo}+"
    return str(lo) if lo == hi else f"{lo}–{hi}"


class SireneSource(Source):
    name = "fr-sirene"
    country = "FR"
    currency = "EUR"
    language = "fr"
    industry_scheme = "NAF"
    id_label = "SIREN"
    provides = ("name, NAF, region, legal form, founded, employee band, officers, "
                "published revenue, commercial-use opt-out")

    def regions(self) -> dict[str, str]:
        return {iso: name for iso, name in REGIONS.values()}

    def error_message(self, body: str) -> str:
        try:
            return str(json.loads(body).get("erreur") or body[:300])[:300]
        except (json.JSONDecodeError, AttributeError):
            return body[:300]

    def search(self, q: Query, log: Log) -> Iterator[dict[str, str]]:
        base: dict[str, str] = {"etat_administratif": "A", "per_page": str(PER_PAGE),
                                "minimal": "true", "include": "siege,dirigeants,finances"}
        forms: list[str] = []
        for cls in q.legal_form_classes:
            forms += QUERY_FORMS.get(cls, [])
        if forms:
            base["nature_juridique"] = ",".join(forms)
        if q.regions:
            base["region"] = ",".join(sorted(INSEE_BY_ISO[r] for r in q.regions
                                             if r in INSEE_BY_ISO))
        if q.min_employees:
            base["tranche_effectif_salarie"] = ",".join(
                code for code, (_, hi) in TRANCHES.items()
                if hi is None or hi >= q.min_employees)
        for code in q.industry_codes:
            page = 1
            while True:
                params = dict(base, activite_principale=code, page=str(page))
                try:
                    res = self.http_json(f"{SEARCH}?{urllib.parse.urlencode(params)}")
                except SourceError as e:
                    log(f"  ! {code}: {e}")
                    break
                total = int(res.get("total_results") or 0)
                if page == 1:
                    note = " (only the first 10 000 are reachable)" if total >= CAP else ""
                    log(f"  {code}: {total} in SIRENE{note}")
                for rec in res.get("results") or []:
                    if rec.get("etat_administratif") != "A" or rec.get("date_fermeture"):
                        continue
                    row = self.to_row(rec)
                    if q.regions and row["hq_region_code"] not in q.regions:
                        continue  # a branch is in the region, the head office is not
                    if row["company_id"]:
                        yield row
                if page * PER_PAGE >= min(total, CAP):
                    break
                page += 1
                time.sleep(DELAY)

    def to_row(self, rec: dict[str, Any]) -> dict[str, str]:
        siege = rec.get("siege") or {}
        region = REGIONS.get(str(siege.get("region") or ""))
        code = str(rec.get("activite_principale") or siege.get("activite_principale") or "")
        form = str(rec.get("nature_juridique") or "")
        founded = str(rec.get("date_creation") or "")[:10]
        age = age_years(founded)
        row = blank_company([])
        row.update({
            "company_id": str(rec.get("siren") or ""),
            "country": self.country,
            "name": str(rec.get("nom_raison_sociale") or rec.get("nom_complet") or "").strip(),
            "trade_name": str(siege.get("nom_commercial") or rec.get("sigle") or ""),
            "industry_codes": code,
            "industry_primary": code,
            "industry_code_count": "1" if code else "",
            "industry_scheme": self.industry_scheme,
            "hq_region": region[1] if region else "",
            "hq_region_code": region[0] if region else "",
            "hq_city": title_name(str(siege.get("libelle_commune") or "")),
            "legal_form": form,
            "legal_form_class": form_class(form),
            "founded_on": founded,
            "company_age_years": str(age) if age else "",
            "officers": "; ".join(officers(rec.get("dirigeants") or [])),
            "registry_flags": FLAG_OPT_OUT if rec.get("opposition_diffusion_commerciale") else "",
            "source": self.name,
        })
        tranche = TRANCHES.get(str(rec.get("tranche_effectif_salarie") or ""))
        if tranche:
            lo, hi = tranche
            row.update({"employees_band": band(lo, hi), "employees_min": str(lo),
                        "employees_max": "" if hi is None else str(hi)})
        row.update(revenue(rec.get("finances")))
        return row

    def selftest(self) -> str:
        res = self.http_json(f"{SEARCH}?q=130025265&per_page=1&minimal=true")
        first = (res.get("results") or [{}])[0]
        return f"{first.get('nom_complet')} ({first.get('siren')})"


def officers(dirigeants: list[dict[str, Any]]) -> list[str]:
    """Natural persons only, auditors (commissaires aux comptes) excluded."""
    out: list[str] = []
    for d in dirigeants:
        if d.get("type_dirigeant") != "personne physique":
            continue
        if "commissaire" in str(d.get("qualite") or "").lower():
            continue
        first = str(d.get("prenoms") or "").split(" ")[0]
        name = person_name(title_name(first), title_name(str(d.get("nom") or "")))
        if name and name not in out:
            out.append(name)
    return out


def revenue(finances: Any) -> dict[str, str]:
    if not isinstance(finances, dict) or not finances:
        return {}
    year = max(finances)
    ca = (finances.get(year) or {}).get("ca")
    if ca is None:
        return {}
    return {"revenue": str(int(float(ca))), "revenue_year": str(year), "revenue_currency": "EUR"}
