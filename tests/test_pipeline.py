"""The 1.x invariants (repo CLAUDE.md), now for every country and profile.

Network and Claude are patched out; nothing here costs money.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from grizly import csvio, llm, pipeline
from grizly.schema import company_columns
from tests.helpers import load_profile

PAGES = {"https://acme.no/": "Acme Lager AS. Vi åpnet et nytt lager i Drammen i mars. "
                             "Telefon 22 57 95 00. post@acme.no " * 3}
PAYLOAD = {
    "industry": "grossist", "short_description": "Grossist.", "trade_name": None,
    "personalization": "Nytt lager i Drammen.",
    "personalization_quote": "Vi åpnet et nytt lager i Drammen i mars",
    "contacts": [{"name": "Kari", "role": "CEO", "phone": "+47 22579500", "email": None},
                 {"name": "Fake", "role": "", "phone": "+47 99999999", "email": None}],
    "own_warehouse": True, "sku_estimate": 2000, "current_wms": None,
    "hiring_logistics": None,
}


class Pipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ctx = pipeline.Context(load_profile("example"), cache_dir=self.dir / "cache",
                                    log_file=self.dir / "cost.jsonl", log=lambda _: None)
        self.src = self.dir / "in.csv"
        rows = [{"company_id": str(i), "country": "NO", "name": f"Acme {i} AS",
                 "web": "https://acme.no", "employees_min": "40", "employees_max": "40",
                 "revenue": "120000000", "revenue_currency": "NOK"} for i in range(3)]
        csvio.write_rows(self.src, rows, company_columns(self.ctx.profile.signals),
                         self.ctx.profile)
        self.calls = 0          # extraction calls
        self.verify_calls = 0   # site-audit calls
        self.verdict = {"verdict": "match", "evidence": "Acme Lager AS", "reason": "ok"}

        def fake_claude(*_: Any, schema: dict[str, Any], **__: Any) -> llm.ClaudeResult:
            if "verdict" in schema["properties"]:
                self.verify_calls += 1
                return llm.ClaudeResult(payload=dict(self.verdict), cost_usd=0.02)
            self.calls += 1
            return llm.ClaudeResult(payload=json.loads(json.dumps(PAYLOAD)), cost_usd=0.03)

        def fake_gather(_f: Any, base: str, max_pages: int = 6,
                        chrome: dict[str, str] | None = None) -> dict[str, str]:
            if chrome is not None:
                chrome.update(PAGES)
            return dict(PAGES)

        self.ctx.fx.pinned.update({"NOK": 11.0})
        patches = [mock.patch("grizly.web.gather_site", side_effect=fake_gather),
                   mock.patch("grizly.llm.call_claude", side_effect=fake_claude)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_enrich(self, limit: int = 0, **kw: Any) -> list[dict[str, str]]:
        opt = pipeline.EnrichOptions(workers=1, **kw)
        pipeline.enrich(self.ctx, self.src, self.src, opt, limit=limit)
        return csvio.read_rows(self.src, self.ctx.profile)[0]

    def test_enrich_validates_and_classifies(self) -> None:
        rows = self.run_enrich()
        r = rows[0]
        self.assertEqual(r["personalization"], "Nytt lager i Drammen.")
        self.assertEqual(r["personalization_source_url"], "https://acme.no/")
        self.assertEqual(r["own_warehouse"], "yes")
        self.assertEqual(r["lead_offer"], "WMS")   # 120 M NOK at 11/EUR = 10.9 M EUR
        self.assertEqual(r["enrich_error"], "")
        contacts, _ = csvio.read_rows(self.dir / "contacts-in.csv", self.ctx.profile,
                                      contacts=True)
        self.assertEqual({c["name"] for c in contacts}, {"Kari"})  # invented number dropped

    def test_dry_run_never_marks_a_row_done(self) -> None:
        rows = self.run_enrich(dry_run=True)
        self.assertTrue(all(r["enrich_quality"] == "" for r in rows))
        self.assertEqual(self.calls, 0)
        self.run_enrich()
        self.assertEqual(self.calls, 3)  # the real run did all three

    def test_resume_from_target_and_cache(self) -> None:
        self.run_enrich(limit=2)
        self.assertEqual(self.calls, 2)
        self.run_enrich(limit=2)          # next run continues, does not redo the first two
        self.assertEqual(self.calls, 3)
        self.run_enrich(force=True)       # --force redoes everything and rewrites the cache
        self.assertEqual(self.calls, 6)
        log = [json.loads(line) for line in
               (self.dir / "cost.jsonl").read_text(encoding="utf-8").splitlines()]
        extraction = [x for x in log if not str(x["note"] or "").startswith("verify")]
        self.assertEqual(len(extraction), 6)
        self.assertEqual(self.verify_calls, 3)  # audits are cached, --force does not redo them

    def test_cache_hit_costs_nothing(self) -> None:
        self.run_enrich(limit=1)
        # Same company, same pages, row marked unfinished -> the cache answers.
        rows, header = csvio.read_rows(self.src, self.ctx.profile)
        rows[0]["enrich_quality"] = ""
        csvio.write_rows(self.src, rows, header, self.ctx.profile)
        self.run_enrich(limit=1)
        self.assertEqual(self.calls, 1)
        last = json.loads((self.dir / "cost.jsonl").read_text(encoding="utf-8")
                          .splitlines()[-1])
        self.assertEqual((last["cost_usd"], last["note"]), (0.0, "cache"))

    def test_contacts_are_appended_not_replaced(self) -> None:
        self.run_enrich(limit=1)
        self.run_enrich(limit=1)
        contacts, _ = csvio.read_rows(self.dir / "contacts-in.csv", self.ctx.profile,
                                      contacts=True)
        self.assertEqual(len(contacts), 2)
        self.assertEqual({c["company_id"] for c in contacts}, {"0", "1"})

    def test_website_not_found_is_an_error_not_a_warning(self) -> None:
        rows, header = csvio.read_rows(self.src, self.ctx.profile)
        for r in rows:
            r["web"] = ""
        csvio.write_rows(self.src, rows, header, self.ctx.profile)
        with mock.patch("grizly.web.website_candidates", return_value=[]):
            rows = self.run_enrich(limit=1)
        self.assertEqual(rows[0]["enrich_error"], "website not found")
        self.assertEqual(rows[0]["enrich_quality"], "0.00")
        self.assertEqual(rows[0]["enrich_warnings"], "")


class SiteAudit(Pipeline):
    """The site audit runs before extraction; a namesake site never gets extracted."""

    def set_web(self, web: str, check: str = "") -> None:
        rows, header = csvio.read_rows(self.src, self.ctx.profile)
        for r in rows:
            r["web"], r["web_check"] = web, check
        csvio.write_rows(self.src, rows, csvio.merge_columns(header, ["web_check"]),
                         self.ctx.profile)

    def test_namesake_site_is_rejected_before_extraction(self) -> None:
        self.verdict = {"verdict": "different_company", "evidence": "Acme Lager AS",
                        "reason": "jiná firma, jiné město"}
        r = self.run_enrich(limit=1)[0]
        self.assertEqual(self.calls, 0)                      # no extraction spent
        self.assertEqual(r["enrich_error"], "website belongs to another company")
        self.assertEqual((r["web"], r["web_check"]), ("", "different_company"))
        self.assertIn("jiná firma", r["web_check_reason"])
        self.assertEqual(r["lead_offer"], "NONE")

    def test_match_without_quote_on_page_is_not_trusted(self) -> None:
        self.verdict = {"verdict": "match", "evidence": "Acme Holding v Brně", "reason": "x"}
        r = self.run_enrich(limit=1)[0]
        self.assertEqual(self.calls, 0)
        self.assertEqual(r["web_check"], "unsure")
        self.assertEqual(r["enrich_error"], "website not confirmed as the company's")

    def test_match_lets_extraction_run(self) -> None:
        r = self.run_enrich(limit=1)[0]
        self.assertEqual((self.verify_calls, self.calls), (1, 1))
        self.assertEqual(r["web_check"], "match")
        self.assertEqual(r["web_check_evidence"], "Acme Lager AS")

    def test_company_id_on_page_needs_no_model(self) -> None:
        PAGES_ID = {"https://acme.no/": PAGES["https://acme.no/"] + " Org.nr. 0 00 000 000"}
        rows, header = csvio.read_rows(self.src, self.ctx.profile)
        rows[0]["company_id"] = "000000000"
        csvio.write_rows(self.src, rows, header, self.ctx.profile)

        def gather(_f: Any, base: str, max_pages: int = 6,
                   chrome: dict[str, str] | None = None) -> dict[str, str]:
            if chrome is not None:
                chrome.update(PAGES_ID)
            return dict(PAGES)

        with mock.patch("grizly.web.gather_site", side_effect=gather):
            r = self.run_enrich(limit=1)[0]
        self.assertEqual(self.verify_calls, 0)
        self.assertEqual(r["web_check"], "id")
        self.assertEqual(self.calls, 1)

    def test_unnamed_search_hit_needs_the_id_on_the_site(self) -> None:
        """brand-x.no does not look like "Acme 0 AS": no paid audit, only the free id check."""
        self.set_web("")
        with mock.patch("grizly.web.website_candidates", return_value=["https://brand-x.no"]):
            r = self.run_enrich(limit=1)[0]
        self.assertEqual((self.verify_calls, self.calls), (0, 0))
        self.assertEqual(r["web"], "")

        rows, header = csvio.read_rows(self.src, self.ctx.profile)
        rows[0]["company_id"] = "000000000"
        csvio.write_rows(self.src, rows, header, self.ctx.profile)
        with_id = {"https://acme.no/": PAGES["https://acme.no/"] + " Org.nr. 0 00 000 000"}

        def gather(_f: Any, base: str, max_pages: int = 6,
                   chrome: dict[str, str] | None = None) -> dict[str, str]:
            if chrome is not None:
                chrome.update(with_id)
            return dict(PAGES)

        with mock.patch("grizly.web.website_candidates", return_value=["https://brand-x.no"]), \
                mock.patch("grizly.web.gather_site", side_effect=gather):
            r = self.run_enrich(limit=1, force=True)[0]
        self.assertEqual(r["web"], "https://brand-x.no")
        self.assertEqual(r["web_check"], "id")
        self.assertEqual(self.verify_calls, 0)

    def test_registry_website_is_trusted(self) -> None:
        self.set_web("https://acme.no", "registry")
        self.run_enrich(limit=1)
        self.assertEqual((self.verify_calls, self.calls), (0, 1))

    def test_next_candidate_when_first_is_a_namesake(self) -> None:
        self.set_web("")
        verdicts = [{"verdict": "different_company", "evidence": None, "reason": "jiná"},
                    {"verdict": "match", "evidence": "Acme Lager AS", "reason": "ok"}]

        def fake(*_: Any, schema: dict[str, Any], **__: Any) -> llm.ClaudeResult:
            if "verdict" in schema["properties"]:
                self.verify_calls += 1
                return llm.ClaudeResult(payload=verdicts.pop(0), cost_usd=0.02)
            self.calls += 1
            return llm.ClaudeResult(payload=json.loads(json.dumps(PAYLOAD)), cost_usd=0.03)

        with mock.patch("grizly.web.website_candidates",
                        return_value=["https://acme.com", "https://acme.no"]), \
                mock.patch("grizly.llm.call_claude", side_effect=fake):
            r = self.run_enrich(limit=1)[0]
        self.assertEqual(r["web"], "https://acme.no")
        self.assertEqual(r["web_check"], "match")
        self.assertEqual(self.calls, 1)

    def test_rows_from_before_the_audit_are_audited_on_resume(self) -> None:
        self.run_enrich(limit=1)
        rows, header = csvio.read_rows(self.src, self.ctx.profile)
        rows[0]["web_check"] = ""   # as if enriched by grizly before the audit existed
        csvio.write_rows(self.src, rows, header, self.ctx.profile)
        self.verify_calls = 0
        with mock.patch("grizly.verify._digest", return_value="fresh"):
            self.run_enrich(limit=1)
        self.assertEqual(self.verify_calls, 1)
        self.assertEqual(self.calls, 1)  # extraction answer came from the cache


class Import(unittest.TestCase):
    def test_any_csv_becomes_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "fair.csv", Path(d) / "out.csv"
            src.write_text("Firma;Website;Mitarbeiter\nMüller Logistik GmbH;mueller.de;120\n"
                           "Ohne Web GmbH;;5\n", encoding="utf-8")
            ctx = pipeline.Context(load_profile("example"), cache_dir=Path(d),
                                   log_file=Path(d) / "c.jsonl", log=lambda _: None)
            rc = pipeline.import_csv(ctx, src, out, {"name": "Firma", "web": "Website",
                                                     "employees_min": "Mitarbeiter"}, "de")
            self.assertEqual(rc, 0)
            rows, _ = csvio.read_rows(out, ctx.profile)
            self.assertEqual(rows[0]["company_id"], "x-mueller.de")
            self.assertEqual(rows[0]["country"], "DE")
            self.assertEqual(rows[1]["company_id"], "x-ohne-web-gmbh")
            self.assertEqual(rows[0]["lead_offer"], "NONE")

    def test_unknown_header_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "f.csv"
            src.write_text("Firma\nA\n", encoding="utf-8")
            ctx = pipeline.Context(load_profile("example"), cache_dir=Path(d),
                                   log_file=Path(d) / "c.jsonl", log=lambda _: None)
            self.assertEqual(pipeline.import_csv(ctx, src, Path(d) / "o.csv",
                                                 {"name": "Company"}, None), 1)


if __name__ == "__main__":
    unittest.main()
