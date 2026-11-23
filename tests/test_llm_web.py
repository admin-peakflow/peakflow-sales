from __future__ import annotations

import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

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
    def test_domain_matches_name_loosely(self) -> None:
        m = web.domain_matches_name
        self.assertTrue(m("https://www.lesamh.cz", "LESA - MH, s.r.o."))
        self.assertTrue(m("danlux.cz", "DANLUX s.r.o."))
        self.assertTrue(m("https://holik.com", "Holík International s.r.o."))
        self.assertTrue(m("https://cykloworld.eu", "CYKLO WORLD a.s."))
        self.assertFalse(m("https://argus.cz", "LESA - MH, s.r.o."))

    def test_serper_keeps_unnamed_hits_and_drops_directories(self) -> None:
        data = {"organic": [{"link": "https://rejstrik-firem.kurzy.cz/26234670/lesa-mh-sro/"},
                            {"link": "https://www.finmag.cz/obchodni-rejstrik/26234670"},
                            {"link": "https://www.brand-x.cz/kontakt"},
                            {"link": "https://www.lesamh.cz/"}]}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(data).encode()
        with mock.patch.dict("os.environ", {"SERPER_API_KEY": "k"}), \
                mock.patch("urllib.request.urlopen", return_value=resp):
            hits = web.serper("LESA - MH, s.r.o.", "Mutěnice", "CZ", "cs")
        self.assertEqual(hits, ["https://www.lesamh.cz", "https://www.brand-x.cz"])

    def test_serper_strips_quotes_from_key(self) -> None:
        # `docker run --env-file` keeps quotes: a quoted key got 403 on every query.
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"organic": []}'
        with mock.patch.dict("os.environ", {"SERPER_API_KEY": '"abc"'}), \
                mock.patch("urllib.request.urlopen", return_value=resp) as call:
            web.serper("LESA - MH, s.r.o.", "", "CZ", "cs")
        self.assertEqual(call.call_args[0][0].get_header("X-api-key"), "abc")

    def test_serper_http_error_is_reported(self) -> None:
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)  # type: ignore[arg-type]
        web._warned.clear()
        with mock.patch.dict("os.environ", {"SERPER_API_KEY": "k"}), \
                mock.patch("urllib.request.urlopen", side_effect=err), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as out:
            self.assertEqual(web.serper("X s.r.o.", "", "CZ", "cs"), [])
        self.assertIn("HTTP 403", out.getvalue())

    def test_normalize_base_drops_control_characters(self) -> None:
        self.assertEqual(web.normalize_base("https://eko\nchem.cz/x "), "https://ekochem.cz")

    def test_candidate_domains_per_country(self) -> None:
        cz = web.candidate_domains("Holík International s.r.o.", web.tlds_for("CZ"))
        self.assertEqual(cz[0], "https://holik.cz")
        no = web.candidate_domains("Bjørnstad Handel AS", web.tlds_for("NO"))
        self.assertIn("https://bjornstadhandel.no", no)
        fr = web.candidate_domains("SINGER FRANCE SAS", web.tlds_for("FR"))
        self.assertEqual(fr[0], "https://singer.fr")
        self.assertEqual(web.tlds_for("PT"), ("pt", "com"))

    def test_national_domain_first_for_every_name_variant(self) -> None:
        cz = web.candidate_domains("Stavebniny Hudečková s.r.o.", web.tlds_for("CZ"))
        tlds = [u.rsplit(".", 1)[1] for u in cz]
        self.assertEqual(tlds, sorted(tlds, key=["cz", "com", "eu"].index))
        self.assertIn("https://stavebniny-hudeckova.cz", cz[:4])

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
