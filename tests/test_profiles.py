from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from grizly import profile as profiles
from grizly.cli import run_examples
from tests.helpers import ROOT

MINIMAL = """
[seller]
name = "X"
currency = "EUR"
[rules]
[[rules.offers]]
offer = "A"
when = { employees_min = { min = 10 } }
"""


def parse(text: str) -> profiles.Profile:
    return profiles.parse(tomllib.loads(text), Path("t.toml"))


class ShippedProfiles(unittest.TestCase):
    def test_every_profile_loads_and_its_examples_pass(self) -> None:
        paths = sorted((ROOT / "profiles").glob("*.toml"))
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            p = profiles.load(str(path))
            self.assertTrue(p.examples, f"{path.name} has no [[examples]]")
            self.assertEqual(run_examples(p), [], path.name)


class Validation(unittest.TestCase):
    def test_minimal_profile(self) -> None:
        p = parse(MINIMAL)
        self.assertEqual(p.output_columns, "en")
        self.assertEqual(p.delimiter, ",")
        self.assertIn("DO_NOT_CONTACT", p.exclude_tags)  # always, for every seller

    def test_unknown_field_is_an_error_not_a_silent_miss(self) -> None:
        with self.assertRaisesRegex(profiles.ProfileError, "unknown field 'revenu'"):
            parse(MINIMAL.replace("employees_min", "revenu"))

    def test_wrong_operator_for_kind(self) -> None:
        with self.assertRaisesRegex(profiles.ProfileError, "operator 'prefix'"):
            parse(MINIMAL.replace("{ min = 10 }", "{ prefix = ['1'] }"))

    def test_wrong_value_type(self) -> None:
        with self.assertRaisesRegex(profiles.ProfileError, "must be a number"):
            parse(MINIMAL.replace("{ min = 10 }", "{ min = 'ten' }"))

    def test_signal_becomes_a_rule_field(self) -> None:
        text = MINIMAL + '[[signals]]\nname = "uses_sap"\ntype = "boolean"\n' \
                         'description = "Mentions SAP."\n'
        text = text.replace("employees_min = { min = 10 }", "uses_sap = { is = true }")
        self.assertEqual(parse(text).signals[0].name, "uses_sap")

    def test_signal_cannot_shadow_core_field(self) -> None:
        text = MINIMAL + '[[signals]]\nname = "revenue"\ntype = "integer"\ndescription = "x"\n'
        with self.assertRaisesRegex(profiles.ProfileError, "clashes"):
            parse(text)

    def test_market_default_must_exist(self) -> None:
        text = MINIMAL + '[markets]\ndefault = "de"\n[markets.cz]\nsource = "cz-ares"\n'
        with self.assertRaisesRegex(profiles.ProfileError, "markets.default"):
            parse(text)

    def test_language_per_market(self) -> None:
        text = MINIMAL + ('[markets.fr]\nsource = "fr-sirene"\nlanguage = "fr"\n'
                          '[markets.no]\nsource = "no-brreg"\n')
        p = parse(text)
        self.assertEqual(p.language_for("FR"), "fr")
        self.assertEqual(p.language_for("NO"), "en")
        self.assertEqual(p.language_name("FR"), "French")


if __name__ == "__main__":
    unittest.main()
