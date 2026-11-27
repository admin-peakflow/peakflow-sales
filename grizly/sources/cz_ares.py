"""Czech Republic — ARES (ares.gov.cz), free, no key.

Verified against production (2026-11-22), not taken from docs:

| What                         | Behaviour                                                  |
|------------------------------|------------------------------------------------------------|
| `czNace` in search           | EXACT match on codes stored on the record (`46110`, `461`, |
|                              | `46`). `4611` returns 0. No prefix search.                 |
| `sidlo.kodKraje` in search   | does NOT filter (returns the national count) -> filter     |
|                              | on the client                                              |
| Cap                          | 1 000 results per query, else `CHYBA_VSTUPU` "příliš mnoho |
|                              | výsledků (N)"; `start` pages correctly                     |
| `financniUrad` in search     | DOES filter (list of 3-digit územní pracoviště codes, e.g. |
|                              | 289 = Brno II). Codes + parent FÚ (`kodNadrizeny`) from    |
|                              | `ciselniky-nazevniky` FinancniUrad/ufo. Used to split a    |
|                              | code over the cap: 46190 = 28 450 nationally, 17–29 per    |
|                              | Brno ÚP (2026-11-23). Records with no FÚ are not found.    |
| `sidlo.kodObce` in search    | filters (Brno 46190 = 130); `kodOkresu`, `psc` do not      |
| Liquidation                  | no field in the record; the law requires the name suffix   |
|                              | "v likvidaci" -> skipped on the name                       |
| Insolvency                   | `seznamRegistraci.stavZdrojeIr` = `AKTIVNI` when the firm  |
|                              | is in the insolvency register, also in search results (1   |
|                              | of 130 Brno 46190 firms, 2026-11-27). Kept, flagged        |
|                              | `insolvency`; `GET /ekonomicke-subjekty/{ico}` re-checks   |
| Several NACE in one query    | counts add up and overflow the cap -> one code per query   |
| Employee band                | only in `/ekonomicke-subjekty-res/{ico}`, not in search    |
| Officers                     | `/ekonomicke-subjekty-vr/{ico}`; active = no `datumVymazu` |
| NACE 2025                    | `czNace` = 2025, `czNace2008` = older; codes differ        |
| Revenue                      | not in ARES (Sbírka listin is PDF) -> estimate in rules    |
| Rate                         | MF may block > 500 req/min; we stay ~4 req/s               |
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any

from grizly.schema import FLAG_INSOLVENCY, blank_company
from grizly.sources.base import Log, Query, Source, SourceError, person_name
from grizly.util import age_years, fold

ARES = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"
DELAY = 0.25

# kodKraje -> (ISO 3166-2, name)
KRAJE: dict[int, tuple[str, str]] = {
    19: ("CZ-10", "Hlavní město Praha"), 27: ("CZ-20", "Středočeský kraj"),
    35: ("CZ-31", "Jihočeský kraj"), 43: ("CZ-32", "Plzeňský kraj"),
    51: ("CZ-41", "Karlovarský kraj"), 60: ("CZ-42", "Ústecký kraj"),
    78: ("CZ-51", "Liberecký kraj"), 86: ("CZ-52", "Královéhradecký kraj"),
    94: ("CZ-53", "Pardubický kraj"), 108: ("CZ-63", "Kraj Vysočina"),
    116: ("CZ-64", "Jihomoravský kraj"), 124: ("CZ-71", "Olomoucký kraj"),
    132: ("CZ-80", "Moravskoslezský kraj"), 141: ("CZ-72", "Zlínský kraj"),
}
ISO_BY_NAME = {name: iso for iso, name in KRAJE.values()}
# Region (ISO) -> Finanční úřad code, parent of the územní pracoviště in `financniUrad`.
FU_BY_REGION: dict[str, str] = {
    "CZ-10": "451", "CZ-20": "452", "CZ-31": "453", "CZ-32": "454", "CZ-41": "455",
    "CZ-42": "456", "CZ-51": "457", "CZ-52": "458", "CZ-53": "459", "CZ-63": "460",
    "CZ-64": "461", "CZ-71": "462", "CZ-80": "463", "CZ-72": "464",
}
TOO_MANY = "příliš mnoho"
LIQUIDATION = re.compile(r"\bv\s+likvidaci\b")


def in_liquidation(name: str) -> bool:
    return bool(LIQUIDATION.search(fold(name.lower())))


def in_insolvency(rec: dict[str, Any]) -> bool:
    return (rec.get("seznamRegistraci") or {}).get("stavZdrojeIr") == "AKTIVNI"

# kategoriePoctuPracovniku (ČSÚ KATPO) -> (min, max); "000" = not stated
KATPO: dict[str, tuple[int, int | None]] = {
    "110": (0, 0), "120": (1, 5), "130": (6, 9), "210": (10, 19), "220": (20, 24),
    "230": (25, 49), "240": (50, 99), "310": (100, 199), "320": (200, 249),
    "330": (250, 499), "340": (500, 999), "410": (1000, 1499), "420": (1500, 1999),
    "430": (2000, 2499), "440": (2500, 2999), "450": (3000, 3999), "460": (4000, 4999),
    "470": (5000, 9999), "510": (10000, None),
}

FORM_CLASS = {
    "101": "sole_trader", "111": "partnership", "112": "company", "113": "partnership",
    "121": "company", "205": "cooperative", "301": "public", "331": "public",
    "706": "nonprofit",
}
QUERY_FORMS = {"company": ["112", "121"], "partnership": ["111", "113"],
               "cooperative": ["205"], "sole_trader": ["101"]}


def band(lo: int | None, hi: int | None) -> str:
    if lo is None:
        return ""
    if hi is None:
        return f"{lo}+"
    return str(lo) if lo == hi else f"{lo}–{hi}"


class AresSource(Source):
    name = "cz-ares"
    country = "CZ"
    currency = "CZK"
    language = "cs"
    industry_scheme = "CZ-NACE"
    id_label = "IČO"
    has_details = True
    provides = "name, CZ-NACE, region, legal form, founded, employee band, officers"

    _offices: list[tuple[str, str]] | None = None

    def regions(self) -> dict[str, str]:
        return {iso: name for iso, name in KRAJE.values()}

    def error_message(self, body: str) -> str:
        try:
            return str(json.loads(body).get("popis") or body[:300])
        except (json.JSONDecodeError, AttributeError):
            return body[:300]

    def search(self, q: Query, log: Log) -> Iterator[dict[str, str]]:
        forms: list[str] = []
        for cls in q.legal_form_classes:
            forms += QUERY_FORMS.get(cls, [])
        liquidation = 0
        for code in q.industry_codes:
            try:
                records = self._search_code(code, forms)
            except SourceError as e:
                if TOO_MANY not in str(e):
                    log(f"  ! {code}: {e}")
                    continue
                records = self._search_split(code, forms, q.regions, log)
            log(f"  {code}: {len(records)} in ARES")
            for rec in records:
                if rec.get("datumZaniku"):
                    continue  # dissolved
                if in_liquidation(str(rec.get("obchodniJmeno") or "")):
                    liquidation += 1
                    continue
                kraj = KRAJE.get(int((rec.get("sidlo") or {}).get("kodKraje") or 0))
                if q.regions and (not kraj or kraj[0] not in q.regions):
                    continue
                row = self.to_row(rec)
                if row["company_id"]:
                    yield row
        if liquidation:
            log(f"  skipped {liquidation} in liquidation")

    def tax_offices(self, regions: set[str]) -> list[str]:
        """Územní pracoviště codes under the regions' Finanční úřad (all if no region)."""
        if self._offices is None:
            res = self.http_json(f"{ARES}/ciselniky-nazevniky/vyhledat",
                                 {"kodCiselniku": "FinancniUrad", "zdrojCiselniku": "ufo"})
            items = ((res.get("ciselniky") or [{}])[0]).get("polozkyCiselniku") or []
            self._offices = [(str(i.get("kod")), str(i.get("kodNadrizeny") or ""))
                             for i in items if not i.get("platnostDo")]
        parents = {FU_BY_REGION[r] for r in regions if r in FU_BY_REGION}
        return [k for k, parent in self._offices
                if parent and parent != "000" and (not parents or parent in parents)]

    def _search_split(self, code: str, forms: list[str], regions: set[str],
                      log: Log) -> list[dict[str, Any]]:
        """A code over the 1 000 cap, one query per tax office (see the table)."""
        offices = self.tax_offices(regions)
        log(f"  {code}: over the cap, split into {len(offices)} tax-office queries")
        out: list[dict[str, Any]] = []
        for fu in offices:
            try:
                out += self._search_code(code, forms, {"financniUrad": [fu]})
            except SourceError as e:
                log(f"  ! {code} / FÚ {fu}: {e}")
            time.sleep(DELAY)
        return out

    def _search_code(self, code: str, forms: list[str],
                     extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            payload: dict[str, Any] = {"czNace": [code], "pocet": 1000, "start": start,
                                       **(extra or {})}
            if forms:
                payload["pravniFormaRos"] = forms
            res = self.http_json(f"{ARES}/ekonomicke-subjekty/vyhledat", payload)
            batch = res.get("ekonomickeSubjekty") or []
            total = int(res.get("pocetCelkem") or 0)
            out.extend(batch)
            start += len(batch)
            if not batch or start >= total:
                return out
            time.sleep(DELAY)

    def to_row(self, rec: dict[str, Any]) -> dict[str, str]:
        sidlo = rec.get("sidlo") or {}
        kraj = KRAJE.get(int(sidlo.get("kodKraje") or 0))
        codes = [str(c) for c in (rec.get("czNace") or rec.get("czNace2008") or [])]
        founded = str(rec.get("datumVzniku") or "")[:10]
        age = age_years(founded)
        form = str(rec.get("pravniForma") or "")
        row = blank_company([])
        row.update({
            "company_id": str(rec.get("ico") or ""),
            "country": self.country,
            "name": str(rec.get("obchodniJmeno") or "").strip(),
            "industry_codes": ";".join(codes),
            "industry_code_count": str(len(codes)) if codes else "",
            "industry_scheme": self.industry_scheme,
            "hq_region": kraj[1] if kraj else str(sidlo.get("nazevKraje") or ""),
            "hq_region_code": kraj[0] if kraj else "",
            "hq_city": str(sidlo.get("nazevObce") or ""),
            "legal_form": form,
            "legal_form_class": FORM_CLASS.get(form, "other" if form else ""),
            "founded_on": founded,
            "company_age_years": str(age) if age else "",
            "registry_flags": FLAG_INSOLVENCY if in_insolvency(rec) else "",
            "source": self.name,
        })
        return row

    def registry_flags(self, company_id: str) -> list[str] | None:
        """Current flags from the live record (insolvency changes over time)."""
        try:
            rec = self.http_json(f"{ARES}/ekonomicke-subjekty/{company_id}")
        except (SourceError, RuntimeError):
            return None
        time.sleep(DELAY)
        if not rec:
            return None
        return [FLAG_INSOLVENCY] if in_insolvency(rec) else []

    def details(self, row: dict[str, str]) -> dict[str, str]:
        """Employee band (RES) + active officers (VR). Two calls per company."""
        ico = row["company_id"]
        out: dict[str, str] = {}
        try:
            res = self.http_json(f"{ARES}/ekonomicke-subjekty-res/{ico}")
        except (SourceError, RuntimeError):
            res = {}
        out.update(employees_from_res(res))
        time.sleep(DELAY)
        try:
            vr = self.http_json(f"{ARES}/ekonomicke-subjekty-vr/{ico}")
        except (SourceError, RuntimeError):
            vr = {}
        out["officers"] = "; ".join(officers_from_vr(vr))
        time.sleep(DELAY)
        return out

    def normalize(self, row: dict[str, str]) -> None:
        super().normalize(row)
        if not row.get("hq_region_code") and row.get("hq_region") in ISO_BY_NAME:
            row["hq_region_code"] = ISO_BY_NAME[row["hq_region"]]
        if not row.get("legal_form_class") and row.get("legal_form"):
            row["legal_form_class"] = FORM_CLASS.get(row["legal_form"], "other")
        if not row.get("industry_code_count") and row.get("industry_codes"):
            row["industry_code_count"] = str(len(row["industry_codes"].split(";")))

    def selftest(self) -> str:
        res = self.http_json(f"{ARES}/ekonomicke-subjekty/23903554")
        return f"{res.get('obchodniJmeno')} ({res.get('ico')})"


def employees_from_res(res: dict[str, Any]) -> dict[str, str]:
    zaznamy = res.get("zaznamy") or [{}]
    first = zaznamy[0] if isinstance(zaznamy, list) and zaznamy else {}
    kod = (first.get("statistickeUdaje") or {}).get("kategoriePoctuPracovniku")
    if kod not in KATPO:
        return {}
    lo, hi = KATPO[kod]
    return {"employees_band": band(lo, hi), "employees_min": str(lo),
            "employees_max": "" if hi is None else str(hi)}


def officers_from_vr(vr: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for zaznam in vr.get("zaznamy") or []:
        for organ in zaznam.get("statutarniOrgany") or []:
            for clen in organ.get("clenoveOrganu") or []:
                if clen.get("datumVymazu"):
                    continue  # struck off — not current
                o = clen.get("fyzickaOsoba") or {}
                name = person_name(o.get("titulPred"), o.get("jmeno"), o.get("prijmeni"))
                if name and name not in names:
                    names.append(name)
    return names
