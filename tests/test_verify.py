from __future__ import annotations

import unittest

from grizly.verify import id_on_pages, interpret, registry_record


class CompanyIdOnPage(unittest.TestCase):
    def test_formats(self) -> None:
        for text in ("IČ: 25582844", "IČO 255 82 844", "DIČ: CZ25582844", "IČ: 25.582.844"):
            self.assertTrue(id_on_pages("25582844", [text]), text)

    def test_siret_contains_siren(self) -> None:
        self.assertTrue(id_on_pages("831325261", ["SIRET 831 325 261 00012"]))

    def test_no_false_match_inside_longer_numbers(self) -> None:
        self.assertFalse(id_on_pages("25582844", ["účet 1255828441/0100"]))
        self.assertFalse(id_on_pages("25582844", ["IČ: 25582845"]))

    def test_synthetic_or_short_ids_are_ignored(self) -> None:
        self.assertFalse(id_on_pages("x-mueller.de", ["x-mueller.de"]))
        self.assertFalse(id_on_pages("123", ["123"]))


PAGES = {"https://a.cz/": "AGRODELTA, s.r.o. Zlín. Obchod s komoditami."}


class ModelAnswer(unittest.TestCase):
    def test_match_needs_quote_on_page(self) -> None:
        ok = interpret({"verdict": "match", "evidence": "AGRODELTA, s.r.o. Zlín",
                        "reason": "r"}, PAGES)
        self.assertEqual(ok.status, "match")
        bad = interpret({"verdict": "match", "evidence": "AGRODELTA Brno", "reason": "r"},
                        PAGES)
        self.assertEqual(bad.status, "unsure")
        self.assertFalse(bad.ok)

    def test_unknown_verdict_is_unsure(self) -> None:
        self.assertEqual(interpret({"verdict": "yes"}, PAGES).status, "unsure")

    def test_registry_record_lists_identity(self) -> None:
        rec = registry_record({"name": "A", "company_id": "1", "hq_city": "Zlín",
                               "officers": "Jan Novák"}, "IČO")
        self.assertIn("IČO: 1", rec)
        self.assertIn("Officers: Jan Novák", rec)


if __name__ == "__main__":
    unittest.main()
