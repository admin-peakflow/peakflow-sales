"""export: many files in -> leady.csv, insolvence.csv, log.txt out. Offline."""

from __future__ import annotations

import csv
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from grizly import csvio, export
from grizly.pipeline import Context
from grizly.schema import CONTACT_COLUMNS, company_columns
from grizly.sources import cz_ares
from tests.helpers import load_profile

P = load_profile("peakflow")

GOOD = {"company_id": "11111111", "country": "CZ", "name": "Dobrá s.r.o.",
        "industry_codes": "46110", "employees_min": "40", "employees_max": "99",
        "employees_band": "50–99", "revenue": "500000000", "sells_physical_goods": "yes",
        "sales_people_estimate": "5", "web": "https://dobra.cz", "web_check": "id",
        "enrich_quality": "0.80", "enriched_at": "2026-11-25", "hq_city": "Brno"}


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


class Export(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.ctx = Context(P, cache_dir=self.data / "grizly-cache", log=lambda _m: None)
        cols = company_columns(P.signals)
        failed = dict(GOOD, company_id="22222222", name="Bez webu s.r.o.", web="",
                      web_check="", enrich_quality="0.00", enrich_error="website not found")
        broke = dict(GOOD, company_id="33333333", name="Krach a.s.",
                     registry_flags="insolvency")
        pulled = dict(GOOD, company_id="44444444", enrich_quality="", enriched_at="",
                      web_check="")
        # the same company twice: an older failure must not beat the success
        old = dict(GOOD, enrich_quality="0.00", enrich_error="website not found",
                   enriched_at="2026-11-20")
        csvio.write_rows(self.data / "enriched.csv", [GOOD, failed, broke], cols, P)
        csvio.write_rows(self.data / "a" / "pull.csv", [old, pulled], cols, P)
        csvio.write_rows(self.data / "grizly.before-x.csv", [dict(GOOD, name="ZÁLOHA")],
                         cols, P)
        csvio.write_rows(self.data / "ledger" / "companies-touched.csv",
                         [dict(GOOD, company_id="99999999")], cols, P)
        contacts = [
            {"company_id": "11111111", "country": "CZ", "company_name": "Dobrá s.r.o.",
             "name": "", "role": "Obchod", "phone": "+420 111", "email": "obchod@dobra.cz"},
            {"company_id": "11111111", "country": "CZ", "company_name": "Dobrá s.r.o.",
             "name": "Jan Novák", "role": "jednatel", "phone": "", "email": "jan@dobra.cz"},
        ]
        csvio.write_rows(self.data / "contacts-enriched.csv", contacts, CONTACT_COLUMNS, P,
                         contacts=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_export(self, check: bool = False) -> export.Summary:
        return export.export(self.ctx, self.data, self.data, check_registry=check,
                             log=lambda _m: None)

    def test_one_lead_file_with_contacts_inline(self) -> None:
        s = self.run_export()
        leads = read(self.data / export.LEADS)
        self.assertEqual([r["ico"] for r in leads], ["11111111"])
        lead = leads[0]
        self.assertEqual(list(lead), export.LEAD_COLUMNS)
        self.assertEqual(lead["nazev"], "Dobrá s.r.o.")  # not the backup's name
        self.assertEqual(lead["telefon"], "+420 111")
        self.assertEqual(lead["kontakt"], "Obchod")
        self.assertEqual(lead["dalsi_kontakty"], "Jan Novák – jednatel: jan@dobra.cz")
        self.assertNotEqual(lead["nabidka"], "NONE")
        self.assertEqual(s.companies, 4)  # ledger and backup are not inputs

    def test_insolvent_listed_separately_failures_only_counted(self) -> None:
        s = self.run_export()
        self.assertEqual([r["ico"] for r in read(self.data / export.INSOLVENT)], ["33333333"])
        self.assertEqual(s.failed["web nenalezen"], 1)
        self.assertEqual(s.never_enriched, 1)
        log = (self.data / export.LOG).read_text(encoding="utf-8")
        self.assertIn("web nenalezen", log)
        self.assertIn("V insolvenci:", log)

    def test_registry_check_moves_lead_to_insolvent(self) -> None:
        with unittest.mock.patch.object(cz_ares.AresSource, "registry_flags",
                                        return_value=["insolvency"]):
            s = self.run_export(check=True)
        self.assertEqual(s.leads, 0)
        self.assertEqual(s.checked, 2)  # both enriched rows
        icos = {r["ico"] for r in read(self.data / export.INSOLVENT)}
        self.assertEqual(icos, {"11111111", "33333333"})

    def test_rerun_does_not_read_its_own_output(self) -> None:
        self.run_export()
        s = self.run_export()
        self.assertEqual(s.leads, 1)
        self.assertEqual(s.companies, 4)


class AresInsolvency(unittest.TestCase):
    def test_flag_from_registry_record(self) -> None:
        rec = {"ico": "1", "obchodniJmeno": "X", "seznamRegistraci": {"stavZdrojeIr": "AKTIVNI"}}
        self.assertEqual(cz_ares.AresSource("ua").to_row(rec)["registry_flags"], "insolvency")
        rec["seznamRegistraci"]["stavZdrojeIr"] = "NEEXISTUJICI"
        self.assertEqual(cz_ares.AresSource("ua").to_row(rec)["registry_flags"], "")


if __name__ == "__main__":
    unittest.main()
