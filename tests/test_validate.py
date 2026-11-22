from __future__ import annotations

import unittest

from grizly.validate import sanity_contacts, validate
from tests.helpers import load_profile

P = load_profile("peakflow")
PAGES = {
    "https://a.cz/": "Jsme velkoobchod. Tel. +420 777 123 456, info@a.cz",
    "https://a.cz/novinky": "V září jsme otevřeli nový sklad v Kroměříži o ploše 4 000 m2.",
}


class Personalization(unittest.TestCase):
    def test_quote_found_keeps_personalization_and_source(self) -> None:
        payload = {"industry": "velkoobchod", "personalization": "Nový sklad v Kroměříži.",
                   "personalization_quote": "otevřeli nový sklad v Kroměříži"}
        quality, _, url = validate(payload, PAGES, P)
        self.assertEqual(payload["personalization"], "Nový sklad v Kroměříži.")
        self.assertEqual(url, "https://a.cz/novinky")
        self.assertGreater(quality, 0)

    def test_invented_quote_drops_personalization(self) -> None:
        payload = {"personalization": "Rodinná tradice.",
                   "personalization_quote": "rodinná firma s tradicí od roku 1990"}
        _, problems, url = validate(payload, PAGES, P)
        self.assertIsNone(payload["personalization"])
        self.assertEqual(url, "")
        self.assertTrue(any("dropped" in p for p in problems))

    def test_personalization_without_quote_is_dropped(self) -> None:
        payload = {"personalization": "Něco."}
        validate(payload, PAGES, P)
        self.assertIsNone(payload["personalization"])

    def test_too_short_quote_is_not_evidence(self) -> None:
        payload = {"personalization": "x", "personalization_quote": "sklad"}
        validate(payload, PAGES, P)
        self.assertIsNone(payload["personalization"])

    def test_quality_counts_profile_quality_signals(self) -> None:
        full = {"industry": "x", "short_description": "y", "sells_physical_goods": True,
                "has_eshop": False, "sales_people_estimate": 3, "has_it_department": False,
                "personalization": "p", "personalization_quote": "otevřeli nový sklad"}
        quality, problems, _ = validate(full, PAGES, P)
        self.assertEqual(quality, 1.0, problems)


class Contacts(unittest.TestCase):
    def check(self, phone: str, pages: dict[str, str]) -> bool:
        payload = {"contacts": [{"name": "A", "role": "", "phone": phone, "email": None}]}
        sanity_contacts(payload, pages)
        return bool(payload["contacts"])

    def test_phone_formats_across_countries(self) -> None:
        self.assertTrue(self.check("777123456", PAGES))                       # CZ, no prefix
        self.assertTrue(self.check("+420777123456", PAGES))                   # CZ, prefix
        no = {"u": "Ring oss på 22 57 95 00"}
        self.assertTrue(self.check("+47 22 57 95 00", no))                    # NO, 8 digits
        fr = {"u": "Standard : 04 74 22 33 44"}
        self.assertTrue(self.check("+33 4 74 22 33 44", fr))                  # FR

    def test_invented_phone_is_removed(self) -> None:
        self.assertFalse(self.check("+420 777 999 888", PAGES))

    def test_numbers_glued_together_still_match(self) -> None:
        self.assertTrue(self.check("777123457", {"u": "777123456 777123457"}))

    def test_email_must_be_on_page(self) -> None:
        payload = {"contacts": [{"name": "A", "role": "", "phone": None, "email": "x@a.cz"},
                                {"name": "B", "role": "", "phone": None, "email": "INFO@a.cz"}]}
        sanity_contacts(payload, PAGES)
        self.assertEqual([c["name"] for c in payload["contacts"]], ["B"])

    def test_max_three(self) -> None:
        payload = {"contacts": [{"name": str(i), "role": "", "phone": None,
                                 "email": "info@a.cz"} for i in range(5)]}
        sanity_contacts(payload, PAGES)
        self.assertEqual(len(payload["contacts"]), 3)


if __name__ == "__main__":
    unittest.main()
