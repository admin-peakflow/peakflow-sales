"""The PeakFlow profile must reproduce grizly 1.x decisions exactly.

`legacy_v1_grizly.py` is grizly.py from commit fb2f09f, unchanged. Its
`classify` and `odhad_obratu` are the oracle; the new rule engine driven by
profiles/peakflow.toml must agree on tags, needs, offer and score.
"""

from __future__ import annotations

import random
import unittest

from grizly import csvio, rules
from grizly.sources.cz_ares import AresSource
from tests import legacy_v1_grizly as legacy
from tests.helpers import load_profile, static_fx

P = load_profile("peakflow")
BACK = csvio.reverse_aliases(P)
ARES = AresSource("test")


def to_canonical(legacy_row: dict[str, str]) -> dict[str, str]:
    row = csvio.canonical(dict(legacy_row), P, BACK)
    ARES.normalize(row)
    return row


def random_legacy_row(rnd: random.Random) -> dict[str, str]:
    lo, hi = rnd.choice([("", ""), ("0", "0"), ("1", "5"), ("6", "9"), ("10", "19"),
                         ("20", "24"), ("25", "49"), ("50", "99"), ("100", "199"),
                         ("250", "499"), ("10000", "")])
    obrat = rnd.choice(["", "", "5000000", "15000000", "35000000", "150000000",
                        "2000000000"])
    row = {
        "ico": str(rnd.randint(1, 10**8)), "nazev": "T",
        "nace": rnd.choice(["46110", "62100", "63110", "25620;46110", "70220", "28990",
                            "01610;62200", "46900;25400"]),
        "pravni_forma": rnd.choice(["112", "121", "101", "331", "301", "706", "205"]),
        "vek_let": rnd.choice(["", "3", "9", "10", "25"]),
        "zamestnanci_min": lo, "zamestnanci_max": hi,
        "obrat": obrat,
        "fyzicke_zbozi": rnd.choice(["", "ano", "ne"]),
        "obchodnici_odhad": rnd.choice(["", "0", "2", "3", "8"]),
        "it_oddeleni": rnd.choice(["", "ano", "ne"]),
        "ai_obsah": rnd.choice(["", "ano", "ne"]),
        "personalizace": rnd.choice(["", "Otevřeli novou pobočku v Brně."]),
        "jednatele": rnd.choice(["", "Jan Novák"]),
        "kraj": rnd.choice(["", "Zlínský kraj", "Hlavní město Praha", "Kraj Vysočina",
                            "Jihomoravský kraj", "Ústecký kraj"]),
    }
    # 1.x only estimated when no published revenue existed; mirror real data.
    if not obrat:
        amount, flag = legacy.odhad_obratu(row)
        if amount:
            row["obrat_odhad"], row["obrat_odhad_priznak"] = str(amount), flag
        elif rnd.random() < 0.3:
            row["obrat_odhad"] = rnd.choice(["8000000", "40000000", "400000000"])
            row["obrat_odhad_priznak"] = "odhad"
    return row


class LegacyParity(unittest.TestCase):
    def test_classify_matches_v1_on_5000_rows(self) -> None:
        rnd = random.Random(20261122)
        fx = static_fx()
        offers: dict[str, int] = {}
        for i in range(5000):
            old = random_legacy_row(rnd)
            new = to_canonical(old)
            legacy.classify(old)
            rules.classify(new, P, fx)
            for a, b in (("tagy", "tags"), ("potreby", "needs"), ("lead_offer", "lead_offer"),
                         ("skore", "score")):
                self.assertEqual(old[a], new[b], f"row {i} {b}: {old}")
            offers[new["lead_offer"]] = offers.get(new["lead_offer"], 0) + 1
        # The sample must exercise every route, or parity proves nothing.
        for offer in ("ERP", "CRM", "SECOND_BRAIN", "NONE"):
            self.assertGreater(offers.get(offer, 0), 50, offers)

    def test_estimate_matches_v1(self) -> None:
        rnd = random.Random(7)
        for _ in range(2000):
            row = random_legacy_row(rnd)
            old_amount, old_flag = legacy.odhad_obratu(row)
            new_amount, ccy = rules.estimate_revenue(to_canonical(row), P)
            self.assertEqual(old_amount, int(new_amount) if new_amount else None, row)
            self.assertEqual(bool(old_flag), bool(new_amount))
            if new_amount:
                self.assertEqual(ccy, "CZK")


class Operators(unittest.TestCase):
    def test_unknown_never_passes_a_comparison(self) -> None:
        f = {"revenue": None, "flag": None}
        for op in ("min", "max", "lt", "gt"):
            self.assertFalse(rules.check({"revenue": {op: 0}}, f))
        self.assertFalse(rules.check({"flag": {"is": False}}, f))
        self.assertTrue(rules.check({"revenue": {"known": False}}, f))

    def test_prefix_works_across_code_formats(self) -> None:
        f = {"industry_codes": ["46.11Z", "62.010"]}
        self.assertTrue(rules.check({"industry_codes": {"prefix": ["4611"]}}, f))
        self.assertTrue(rules.check({"industry_codes": {"prefix": ["62.0"]}}, f))
        self.assertFalse(rules.check({"industry_codes": {"prefix": ["47"]}}, f))

    def test_list_any_none(self) -> None:
        f = {"needs": ["CRM", "SKLAD"]}
        self.assertTrue(rules.check({"needs": {"any": ["ERP", "CRM"]}}, f))
        self.assertFalse(rules.check({"needs": {"none": ["CRM"]}}, f))


class Currency(unittest.TestCase):
    def test_foreign_revenue_is_converted_to_profile_currency(self) -> None:
        fx = static_fx({"CZK": 25.0, "NOK": 11.0})
        row = {"company_id": "1", "country": "NO", "revenue": "110000000",
               "revenue_currency": "NOK"}
        f = rules.facts(row, P, fx)
        self.assertAlmostEqual(f["revenue"], 250_000_000)

    def test_missing_rate_means_unknown_revenue_not_a_guess(self) -> None:
        fx = static_fx({"CZK": 25.0})
        row = {"company_id": "1", "country": "NO", "revenue": "110000000",
               "revenue_currency": "NOK", "employees_min": "1", "employees_max": "5"}
        rules.classify(row, P, fx)
        self.assertIsNone(rules.facts(row, P, fx)["revenue"])
        self.assertIn("NOK", fx.warnings[0])


class Exclusions(unittest.TestCase):
    def test_registry_opt_out_beats_every_rule(self) -> None:
        row = {"company_id": "1", "country": "CZ", "industry_codes": "46110",
               "employees_min": "40", "employees_max": "99", "revenue": "500000000",
               "sells_physical_goods": "yes", "sales_people_estimate": "5",
               "registry_flags": "marketing_opt_out"}
        rules.classify(row, P, static_fx())
        self.assertEqual(row["lead_offer"], "NONE")
        self.assertIn("DO_NOT_CONTACT", row["tags"])


if __name__ == "__main__":
    unittest.main()
