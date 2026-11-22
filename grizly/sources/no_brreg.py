"""Norway — Brønnøysund registers (data.brreg.no), free, no key.

Verified against production (2026-11-22):

| What                       | Behaviour                                                   |
|----------------------------|-------------------------------------------------------------|
| `naeringskode`             | PREFIX match: `46` -> 26 519, `46.1` -> 2 488, `46.110` -> 6 |
| Codes                      | dotted SN codes, e.g. `46.180`                              |
| `organisasjonsform`        | comma list works (`AS,ASA`)                                 |
| `fraAntallAnsatte`         | server-side employee filter                                 |
| Paging cap                 | `size * (page + 1)` must be <= 10 000, else HTTP 400        |
| Region                     | no county filter; county = first 2 digits of                |
|                            | `kommunenummer` = ISO 3166-2:NO -> filter on the client     |
| Employees                  | exact `antallAnsatte` in search results                     |
| Website                    | `hjemmeside` often present -> no domain guessing needed     |
| Officers                   | `/enheter/{orgnr}/roller`: DAGL (CEO), LEDE (chair)         |
| Revenue                    | regnskapsregisteret `/regnskap/{orgnr}`: published accounts, |
|                            | `sumDriftsinntekter`, currency in `valuta`                  |
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from typing import Any

from grizly.schema import blank_company
from grizly.sources.base import Log, Query, Source, SourceError, person_name
from grizly.util import age_years

ENHETER = "https://data.brreg.no/enhetsregisteret/api/enheter"
REGNSKAP = "https://data.brreg.no/regnskapsregisteret/regnskap"
PAGE = 1000
CAP = 10_000
DELAY = 0.2

# Counties since 2024-01-01 (verified against ws.geonorge.no/kommuneinfo/v1/fylker).
FYLKER = {
    "NO-03": "Oslo", "NO-11": "Rogaland", "NO-15": "Møre og Romsdal", "NO-18": "Nordland",
    "NO-31": "Østfold", "NO-32": "Akershus", "NO-33": "Buskerud", "NO-34": "Innlandet",
    "NO-39": "Vestfold", "NO-40": "Telemark", "NO-42": "Agder", "NO-46": "Vestland",
    "NO-50": "Trøndelag", "NO-55": "Troms", "NO-56": "Finnmark",
}

FORM_CLASS = {
    "AS": "company", "ASA": "company", "SE": "company",
    "ENK": "sole_trader",
    "ANS": "partnership", "DA": "partnership", "KS": "partnership",
    "SA": "cooperative", "BBL": "cooperative", "BRL": "cooperative",
    "ADOS": "public", "FKF": "public", "FYLK": "public", "IKS": "public", "KF": "public",
    "KOMM": "public", "ORGL": "public", "SF": "public", "STAT": "public", "KIRK": "public",
    "FLI": "nonprofit", "STI": "nonprofit",
}
QUERY_FORMS = {cls: [k for k, v in FORM_CLASS.items() if v == cls]
               for cls in set(FORM_CLASS.values())}

OFFICER_ROLES = ("DAGL", "LEDE", "INNH")  # CEO, chair of the board, sole proprietor


class BrregSource(Source):
    name = "no-brreg"
    country = "NO"
    currency = "NOK"
    language = "no"
    industry_scheme = "SN"
    id_label = "org.nr."
    has_details = True
    provides = ("name, SN industry, county, legal form, founded, exact employees, website, "
                "officers, revenue from published accounts")

    def regions(self) -> dict[str, str]:
        return dict(FYLKER)

    def error_message(self, body: str) -> str:
        try:
            data = json.loads(body)
            errs = data.get("valideringsfeil") or []
            return "; ".join(str(e.get("feilmelding")) for e in errs) or str(data)[:300]
        except (json.JSONDecodeError, AttributeError):
            return body[:300]

    def search(self, q: Query, log: Log) -> Iterator[dict[str, str]]:
        forms: list[str] = []
        for cls in q.legal_form_classes:
            forms += QUERY_FORMS.get(cls, [])
        for code in q.industry_codes:
            page = 0
            while True:
                params = [f"naeringskode={code}", f"size={PAGE}", f"page={page}",
                          "konkurs=false", "underAvvikling=false"]
                if forms:
                    params.append("organisasjonsform=" + ",".join(forms))
                if q.min_employees:
                    params.append(f"fraAntallAnsatte={q.min_employees}")
                try:
                    res = self.http_json(f"{ENHETER}?{'&'.join(params)}")
                except SourceError as e:
                    log(f"  ! {code}: {e}")
                    break
                info = res.get("page") or {}
                total = int(info.get("totalElements") or 0)
                if page == 0:
                    note = " (only the first 10 000 are reachable)" if total > CAP else ""
                    log(f"  {code}: {total} in Brreg{note}")
                for rec in (res.get("_embedded") or {}).get("enheter") or []:
                    if rec.get("slettedato") or rec.get("konkurs") or rec.get("underAvvikling") \
                            or rec.get("underTvangsavviklingEllerTvangsopplosning"):
                        continue
                    row = self.to_row(rec)
                    if q.regions and row["hq_region_code"] not in q.regions:
                        continue
                    if row["company_id"]:
                        yield row
                page += 1
                if page >= int(info.get("totalPages") or 0) or PAGE * (page + 1) > CAP:
                    break
                time.sleep(DELAY)

    def to_row(self, rec: dict[str, Any]) -> dict[str, str]:
        addr = rec.get("forretningsadresse") or rec.get("postadresse") or {}
        kommune = str(addr.get("kommunenummer") or "")
        region = f"NO-{kommune[:2]}" if len(kommune) >= 2 else ""
        codes = [str(rec[k]["kode"]) for k in ("naeringskode1", "naeringskode2", "naeringskode3")
                 if isinstance(rec.get(k), dict) and rec[k].get("kode")]
        form = str((rec.get("organisasjonsform") or {}).get("kode") or "")
        founded = str(rec.get("stiftelsesdato") or rec.get("registreringsdatoEnhetsregisteret")
                      or "")[:10]
        age = age_years(founded)
        row = blank_company([])
        row.update({
            "company_id": str(rec.get("organisasjonsnummer") or ""),
            "country": self.country,
            "name": str(rec.get("navn") or "").strip(),
            "web": normalize_web(str(rec.get("hjemmeside") or "")),
            "web_check": "registry" if rec.get("hjemmeside") else "",
            "industry_codes": ";".join(codes),
            "industry_primary": codes[0] if codes else "",
            "industry_code_count": str(len(codes)) if codes else "",
            "industry_scheme": self.industry_scheme,
            "hq_region": FYLKER.get(region, ""),
            "hq_region_code": region if region in FYLKER else "",
            "hq_city": str(addr.get("poststed") or addr.get("kommune") or "").title(),
            "legal_form": form,
            "legal_form_class": FORM_CLASS.get(form, "other" if form else ""),
            "founded_on": founded,
            "company_age_years": str(age) if age else "",
            "source": self.name,
        })
        if rec.get("harRegistrertAntallAnsatte") and rec.get("antallAnsatte") is not None:
            n = int(rec["antallAnsatte"])
            row.update({"employees_band": str(n), "employees_min": str(n),
                        "employees_max": str(n)})
        return row

    def details(self, row: dict[str, str]) -> dict[str, str]:
        """Officers + latest published revenue. Two calls per company."""
        orgnr = row["company_id"]
        out: dict[str, str] = {}
        with contextlib.suppress(SourceError, RuntimeError):
            out["officers"] = "; ".join(officers_from_roles(
                self.http_json(f"{ENHETER}/{orgnr}/roller")))
        time.sleep(DELAY)
        with contextlib.suppress(SourceError, RuntimeError):
            out.update(revenue_from_accounts(self.http_json(f"{REGNSKAP}/{orgnr}")))
        time.sleep(DELAY)
        return out

    def selftest(self) -> str:
        res = self.http_json(f"{ENHETER}/923609016")
        return f"{res.get('navn')} ({res.get('organisasjonsnummer')})"


def normalize_web(v: str) -> str:
    v = v.strip()
    if not v:
        return ""
    return v if v.startswith(("http://", "https://")) else f"https://{v}"


def officers_from_roles(data: Any) -> list[str]:
    found: dict[str, list[str]] = {r: [] for r in OFFICER_ROLES}
    for group in (data or {}).get("rollegrupper") or []:
        for role in group.get("roller") or []:
            code = str((role.get("type") or {}).get("kode") or "")
            person = role.get("person") or {}
            if code not in found or role.get("avregistrert") or person.get("erDoed"):
                continue
            n = person.get("navn") or {}
            name = person_name(n.get("fornavn"), n.get("mellomnavn"), n.get("etternavn"))
            if name:
                found[code].append(name)
    out: list[str] = []
    for code in OFFICER_ROLES:  # CEO first: that is who decides
        for name in found[code]:
            if name not in out:
                out.append(name)
    return out


def revenue_from_accounts(data: Any) -> dict[str, str]:
    """Latest company (not group) accounts -> revenue, year, currency."""
    if not isinstance(data, list):
        return {}
    accounts = [a for a in data if a.get("regnskapstype") == "SELSKAP"] or data
    accounts.sort(key=lambda a: str((a.get("regnskapsperiode") or {}).get("tilDato") or ""))
    if not accounts:
        return {}
    latest = accounts[-1]
    drift = ((latest.get("resultatregnskapResultat") or {}).get("driftsresultat") or {})
    revenue = (drift.get("driftsinntekter") or {}).get("sumDriftsinntekter")
    if revenue is None:
        return {}
    return {"revenue": str(int(float(revenue))),
            "revenue_year": str((latest.get("regnskapsperiode") or {}).get("tilDato") or "")[:4],
            "revenue_currency": str(latest.get("valuta") or "NOK")}
