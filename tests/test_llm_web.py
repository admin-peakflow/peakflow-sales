from __future__ import annotations

import json
import unittest
from pathlib import Path

from grizly import llm, web
from tests.helpers import load_profile

PF = load_profile("peakflow")
EX = load_profile("example")


class Schema(unittest.TestCase):
    def test_profile_signals_are_in_the_schema(self) -> None:
        s = llm.build_schema(EX)
        for name in ("own_warehouse", "sku_estimate", "current_wms"):
            self.assertIn(name, s["properties"])
            self.assertIn(name, s["required"])
        self.assertEqual(s["properties"]["sku_estimate"]["type"], ["integer", "null"])
        self.assertFalse(s["additionalProperties"])

    def test_instruction_language_and_signal_rules(self) -> None:
        text = llm.build_instruction(PF, "Czech")
        self.assertIn("in Czech", text)
        self.assertIn("`sales_people_estimate`", text)
        self.assertIn("VERBATIM", text)

    def test_cache_key_changes_with_the_question(self) -> None:
        pages = {"https://a.cz/": "text"}
        company = {"company_id": "1", "country": "CZ"}
        d1 = llm.prompt_digest(llm.build_instruction(PF, "Czech"), llm.build_schema(PF))
        d2 = llm.prompt_digest(llm.build_instruction(EX, "English"), llm.build_schema(EX))
        self.assertNotEqual(llm.cache_path(Path("c"), company, pages, d1),
                            llm.cache_path(Path("c"), company, pages, d2))

    def test_parse_output(self) -> None:
        ok = json.dumps({"total_cost_usd": 0.03, "structured_output": {"industry": "x"}})
        r = llm.parse_output(0, ok, "")
        self.assertEqual((r.payload, r.cost_usd, r.error), ({"industry": "x"}, 0.03, None))
        r = llm.parse_output(0, json.dumps({"result": '{"industry": "y"}'}), "")
        self.assertEqual(r.payload, {"industry": "y"})
        r = llm.parse_output(0, json.dumps({"is_error": True, "result": "budget",
                                            "total_cost_usd": 0.15}), "")
        self.assertEqual((r.error, r.cost_usd), ("budget", 0.15))
        self.assertIsNotNone(llm.parse_output(1, "", "Failed to authenticate").error)
        self.assertIsNotNone(llm.parse_output(0, "not json", "").error)


class Web(unittest.TestCase):
    def test_candidate_domains_per_country(self) -> None:
        cz = web.candidate_domains("Holík International s.r.o.", web.tlds_for("CZ"))
        self.assertEqual(cz[0], "https://holik.cz")
        no = web.candidate_domains("Bjørnstad Handel AS", web.tlds_for("NO"))
        self.assertIn("https://bjornstadhandel.no", no)
        fr = web.candidate_domains("SINGER FRANCE SAS", web.tlds_for("FR"))
        self.assertEqual(fr[0], "https://singer.fr")
        self.assertEqual(web.tlds_for("PT"), ("pt", "com"))

    def test_parked_pages_in_several_languages(self) -> None:
        for text in ("Doména je na prodej", "Domenet er til salgs", "Ce domaine est à vendre"):
            html = f"<html><body><p>Holikstadt {text}</p></body></html>"
            self.assertFalse(web.looks_like_company_page(html, "Holikstadt s.r.o."))
        self.assertTrue(web.looks_like_company_page(
            "<html><body><p>Vítejte u Holíkstadt</p></body></html>", "Holíkstadt s.r.o."))

    def test_namesake_domain_is_rejected(self) -> None:
        page = "<html><body><p>Happy shop — dárky pro radost</p></body></html>"
        self.assertFalse(web.looks_like_company_page(page, "HAPPY LYFE THERAPEUTICS s.r.o."))
        page = "<html><body><p>Happy Lyfe Therapeutics — research</p></body></html>"
        self.assertTrue(web.looks_like_company_page(page, "HAPPY LYFE THERAPEUTICS s.r.o."))
        # legal form and generic words do not count as evidence
        page = "<html><body><p>Holík — ochranné rukavice</p></body></html>"
        self.assertTrue(web.looks_like_company_page(page, "Holík International s.r.o."))

    def test_signal_links_first_including_diacritics_and_encoding(self) -> None:
        html = ('<a href="/blog/x">b</a><a href="/kari%C3%A9ra">k</a>'
                '<a href="/om-oss">o</a><a href="https://other.com/kontakt">e</a>'
                '<a href="/logo.png">i</a>')
        links = web.internal_links("https://a.cz", html)
        self.assertEqual(links[:2], ["https://a.cz/kari%C3%A9ra", "https://a.cz/om-oss"])
        self.assertNotIn("https://other.com/kontakt", links)
        self.assertNotIn("https://a.cz/logo.png", links)

    def test_html_to_text_drops_chrome(self) -> None:
        html = ("<html><nav>Menu</nav><body><p>Řeřicha</p><script>x()</script>"
                "<footer>©</footer></body></html>")
        self.assertEqual(web.html_to_text(html), "Řeřicha")


if __name__ == "__main__":
    unittest.main()
