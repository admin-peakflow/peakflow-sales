from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from grizly import csvio
from grizly.schema import company_columns
from tests.helpers import load_profile

PF = load_profile("peakflow")
EX = load_profile("example")

V1_HEADER = ("ico;nazev;web;nace;obor;kraj;mesto;pravni_forma;datum_vzniku;vek_let;jednatele;"
             "zamestnanci_kat;zamestnanci_min;zamestnanci_max;obrat;obrat_rok;obrat_odhad;"
             "obrat_odhad_priznak;fyzicke_zbozi;eshop;obchodnici_odhad;crm_signal;it_oddeleni;"
             "ai_obsah;popis_kratky;personalizace;tagy;potreby;lead_offer;skore;enriched_dne;"
             "enrich_quality;enrich_warnings;enrich_cost_usd;enrich_error")


class Csv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_grizly_1x_file(self) -> None:
        p = self.dir / "v1.csv"
        cells = [""] * len(V1_HEADER.split(";"))
        values = dict(zip(V1_HEADER.split(";"), cells, strict=True))
        values.update({"ico": "25322214", "nazev": "Holík", "kraj": "Zlínský kraj",
                       "fyzicke_zbozi": "ano", "obrat_odhad_priznak": "odhad"})
        p.write_text("﻿" + V1_HEADER + "\n" + ";".join(values.values()) + "\n",
                     encoding="utf-8")
        rows, header = csvio.read_rows(p, PF)
        self.assertEqual(rows[0]["company_id"], "25322214")
        self.assertEqual(rows[0]["sells_physical_goods"], "yes")
        self.assertEqual(rows[0]["revenue_estimate_flag"], "estimate")
        self.assertEqual(header[0], "company_id")

    def test_cs_output_keeps_v1_headers_and_values(self) -> None:
        p = self.dir / "out.csv"
        cols = company_columns(PF.signals)
        csvio.write_rows(p, [{"company_id": "1", "name": "Žluťoučký", "sells_physical_goods":
                              "yes", "revenue_estimate_flag": "estimate"}], cols, PF)
        raw = p.read_text(encoding="utf-8-sig").splitlines()
        header = raw[0].split(";")
        for old in V1_HEADER.split(";"):
            self.assertIn(old, header)  # every 1.x column still exists for Gatsby & sheets
        self.assertIn("ano", raw[1])
        self.assertIn("odhad", raw[1])
        self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_en_output_is_plain_csv(self) -> None:
        p = self.dir / "en.csv"
        csvio.write_rows(p, [{"company_id": "1", "name": "Ærø AS", "own_warehouse": "ano"}],
                         company_columns(EX.signals), EX)
        data = p.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        text = data.decode("utf-8")
        self.assertTrue(text.startswith("company_id,country,name"))
        self.assertIn(",yes,", text)

    def test_roundtrip_between_profiles(self) -> None:
        a, b = self.dir / "a.csv", self.dir / "b.csv"
        row = {"company_id": "1", "country": "CZ", "name": "Řeřicha s.r.o.",
               "industry_codes": "46110;46900", "sells_physical_goods": "no"}
        csvio.write_rows(a, [row], company_columns(PF.signals), PF)
        rows, _ = csvio.read_rows(a, PF)
        csvio.write_rows(b, rows, company_columns(PF.signals), EX)  # cs file -> en file
        back, _ = csvio.read_rows(b, EX)
        for k, v in row.items():
            self.assertEqual(back[0][k], v)

    def test_sniff(self) -> None:
        self.assertEqual(csvio.sniff_delimiter("a;b;c"), ";")
        self.assertEqual(csvio.sniff_delimiter("a,b"), ",")
        self.assertEqual(csvio.sniff_delimiter("a\tb"), "\t")
        self.assertEqual(csvio.sniff_delimiter("single"), ",")

    def test_atomic_write_leaves_no_tmp(self) -> None:
        p = self.dir / "x.csv"
        csvio.write_rows(p, [], ["company_id"], EX)
        self.assertFalse((self.dir / "x.csv.tmp").exists())


if __name__ == "__main__":
    unittest.main()
