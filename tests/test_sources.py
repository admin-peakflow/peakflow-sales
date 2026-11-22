"""Adapters map recorded live responses (tests/fixtures, 2026-11-22) to canonical rows.

Person names in the fixtures are replaced with fake ones; company data is real.
"""

from __future__ import annotations

import copy
import unittest

from grizly.schema import FLAG_OPT_OUT
from grizly.sources import SOURCES, SourceError, cz_ares, for_country, fr_sirene, get, no_brreg
from tests.helpers import fixture


class Registry(unittest.TestCase):
    def test_names_follow_iso_prefix_convention(self) -> None:
        for name, cls in SOURCES.items():
            self.assertTrue(name.startswith(cls.country.lower() + "-"), name)
            self.assertEqual(len(cls.currency), 3)

    def test_unknown_source(self) -> None:
        with self.assertRaises(SourceError):
            get("xx-nothing", "ua")

    def test_for_country(self) -> None:
        src = for_country("no", "ua")
        self.assertIsNotNone(src)
        assert src is not None
        self.assertEqual(src.name, "no-brreg")
        self.assertIsNone(for_country("US", "ua"))


class Ares(unittest.TestCase):
    src = cz_ares.AresSource("ua")

    def test_search_record(self) -> None:
        rec = fixture("cz_ares_search.json")["ekonomickeSubjekty"][0]
        row = self.src.to_row(rec)
        self.assertEqual(row["company_id"], rec["ico"])
        self.assertEqual(row["country"], "CZ")
        self.assertEqual(row["hq_region_code"], "CZ-42")  # Ústecký kraj, kodKraje 60
        self.assertEqual(row["legal_form_class"], "company")
        self.assertIn("46110", row["industry_codes"].split(";"))
        self.assertEqual(row["source"], "cz-ares")

    def test_res_employee_band(self) -> None:
        out = cz_ares.employees_from_res(fixture("cz_ares_res.json"))
        self.assertEqual((out["employees_min"], out["employees_max"]), ("100", "199"))
        self.assertEqual(out["employees_band"], "100–199")

    def test_vr_only_active_officers(self) -> None:
        names = cz_ares.officers_from_vr(fixture("cz_ares_vr.json"))
        self.assertTrue(names)
        vr = fixture("cz_ares_vr.json")
        struck = {f"{c['fyzickaOsoba']['jmeno']} {c['fyzickaOsoba']['prijmeni']}"
                  for z in vr["zaznamy"] for o in z["statutarniOrgany"]
                  for c in o["clenoveOrganu"] if c.get("datumVymazu") and c.get("fyzickaOsoba")}
        active = {f"{c['fyzickaOsoba']['jmeno']} {c['fyzickaOsoba']['prijmeni']}"
                  for z in vr["zaznamy"] for o in z["statutarniOrgany"]
                  for c in o["clenoveOrganu"]
                  if not c.get("datumVymazu") and c.get("fyzickaOsoba")}
        self.assertEqual(set(names), active)
        self.assertFalse((struck - active) & set(names))

    def test_regions_resolve_like_v1(self) -> None:
        self.assertEqual(self.src.resolve_regions(["jihomoravsky", "zlinsky"]),
                         {"CZ-64", "CZ-72"})
        self.assertEqual(self.src.resolve_regions(["vysocina", "CZ-10"]), {"CZ-63", "CZ-10"})
        with self.assertRaises(SourceError):
            self.src.resolve_regions(["bavaria"])

    def test_normalize_backfills_legacy_rows(self) -> None:
        row = {"company_id": "1", "hq_region": "Zlínský kraj", "legal_form": "101",
               "industry_codes": "46110;62100"}
        self.src.normalize(row)
        self.assertEqual(row["hq_region_code"], "CZ-72")
        self.assertEqual(row["legal_form_class"], "sole_trader")
        self.assertEqual(row["industry_code_count"], "2")
        self.assertEqual(row["country"], "CZ")


class Brreg(unittest.TestCase):
    src = no_brreg.BrregSource("ua")

    def test_search_record(self) -> None:
        rec = fixture("no_brreg_search.json")["_embedded"]["enheter"][0]
        row = self.src.to_row(rec)
        self.assertEqual(row["company_id"], rec["organisasjonsnummer"])
        self.assertEqual(row["country"], "NO")
        self.assertEqual(row["hq_region_code"],
                         "NO-" + rec["forretningsadresse"]["kommunenummer"][:2])
        self.assertEqual(row["employees_min"], str(rec["antallAnsatte"]))
        self.assertEqual(row["employees_min"], row["employees_max"])
        self.assertEqual(row["industry_primary"], rec["naeringskode1"]["kode"])
        self.assertEqual(row["legal_form_class"], "company")

    def test_website_is_normalized(self) -> None:
        self.assertEqual(no_brreg.normalize_web("www.balco.no"), "https://www.balco.no")
        self.assertEqual(no_brreg.normalize_web(""), "")

    def test_officers_ceo_first(self) -> None:
        names = no_brreg.officers_from_roles(fixture("no_brreg_roles.json"))
        self.assertTrue(names)
        roles = fixture("no_brreg_roles.json")
        ceo = [f"{r['person']['navn']['fornavn']} {r['person']['navn']['etternavn']}"
               for g in roles["rollegrupper"] for r in g["roller"]
               if r["type"]["kode"] == "DAGL" and r.get("person") and not r.get("avregistrert")]
        if ceo:
            self.assertEqual(names[0], ceo[0])

    def test_revenue_from_latest_company_accounts(self) -> None:
        accounts = fixture("no_brreg_accounts.json")
        out = no_brreg.revenue_from_accounts(accounts)
        latest = max((a for a in accounts if a["regnskapstype"] == "SELSKAP"),
                     key=lambda a: a["regnskapsperiode"]["tilDato"])
        expected = latest["resultatregnskapResultat"]["driftsresultat"]["driftsinntekter"][
            "sumDriftsinntekter"]
        self.assertEqual(out["revenue"], str(int(expected)))
        self.assertEqual(out["revenue_currency"], "NOK")
        self.assertEqual(out["revenue_year"], latest["regnskapsperiode"]["tilDato"][:4])

    def test_regions(self) -> None:
        self.assertEqual(self.src.resolve_regions(["oslo", "NO-46", "more og romsdal"]),
                         {"NO-03", "NO-46", "NO-15"})


class Sirene(unittest.TestCase):
    src = fr_sirene.SireneSource("ua")

    def test_search_record(self) -> None:
        rec = fixture("fr_search.json")["results"][0]
        row = self.src.to_row(rec)
        self.assertEqual(row["company_id"], rec["siren"])
        self.assertEqual(row["country"], "FR")
        self.assertEqual(row["hq_region_code"], "FR-ARA")  # INSEE 84
        self.assertEqual((row["employees_min"], row["employees_max"]), ("50", "99"))
        self.assertEqual(row["legal_form_class"], "company")  # 5710 SAS
        year = max(rec["finances"])
        self.assertEqual(row["revenue"], str(rec["finances"][year]["ca"]))
        self.assertEqual(row["revenue_currency"], "EUR")

    def test_officers_skip_auditors_and_legal_entities(self) -> None:
        rec = copy.deepcopy(fixture("fr_search.json")["results"][1])
        rec["dirigeants"].append({"type_dirigeant": "personne physique", "nom": "DUPONT",
                                  "prenoms": "MARIE CLAIRE", "qualite": "Directeur Général"})
        names = fr_sirene.officers(rec["dirigeants"])
        self.assertEqual(names, ["Marie Dupont"])

    def test_opt_out_becomes_registry_flag(self) -> None:
        rec = copy.deepcopy(fixture("fr_search.json")["results"][0])
        rec["opposition_diffusion_commerciale"] = True
        self.assertEqual(self.src.to_row(rec)["registry_flags"], FLAG_OPT_OUT)

    def test_legal_form_classes(self) -> None:
        self.assertEqual(fr_sirene.form_class("1000"), "sole_trader")
        self.assertEqual(fr_sirene.form_class("5499"), "company")
        self.assertEqual(fr_sirene.form_class("5202"), "partnership")
        self.assertEqual(fr_sirene.form_class("7220"), "public")
        self.assertEqual(fr_sirene.form_class("9220"), "nonprofit")

    def test_regions(self) -> None:
        self.assertEqual(self.src.resolve_regions(["ile-de-france", "auvergne", "FR-20R"]),
                         {"FR-IDF", "FR-ARA", "FR-20R"})


if __name__ == "__main__":
    unittest.main()
