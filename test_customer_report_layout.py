"""Offline layout regressions against the production renderer and saved blower response."""
import contextlib
import html
import io
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
from test_blower_live_response import saved_response
from test_blower_support_normalization import BAD, classified, finalize
from test_structured_verdict import report_list_items


SECTION_ORDER = [
    "HVAC Quote Review",
    "Review Summary",
    "Check Your Tech Recommendation",
    "Red Flags",
    "Good Signs",
    "What This Means for You",
    "Does the Diagnosis Make Sense?",
    "What Evidence Should Support This Diagnosis?",
    "Important Missing Information",
    "Price & Value Review",
    "Installation / Repair Concerns",
    "Questions to Ask Your Contractor",
    "Bottom Line",
    "Review Complete",
]


def report_sections(report):
    return [html.unescape(first or second) for first, second in re.findall(
        r'<h[12]>(.*?)</h[12]>|<div class="verdict-title">(.*?)</div>', report)]


class CustomerReportLayoutTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_findings(self, report, analysis):
        for heading, label, values in (
            ("Red Flags", "Major Red Flags", analysis.red_flags),
            ("Good Signs", "Positive Findings", analysis.good_signs),
        ):
            expected = [value.strip() for value in values if value.strip()]
            self.assertEqual(report_list_items(report, heading), expected)
            self.assertEqual(report_sections(report).count(heading), int(bool(expected)))
            count = re.search(rf"<strong>{label}</strong>\s*(\d+)\s*</div>", report)
            self.assertIsNotNone(count)
            self.assertEqual(int(count.group(1)), len(expected))

    def test_saved_blower_response_has_exact_section_order_and_unchanged_findings(self):
        for name, analysis in (("saved", saved_response()), ("finalized", finalize(saved_response()))):
            with self.subTest(response=name):
                before = analysis.model_dump()
                report = main.build_report_html(analysis, quote_count=1)
                # Exact sequence also rejects duplicate and unrelated sections.
                self.assertEqual(report_sections(report), SECTION_ORDER)
                self.assert_findings(report, analysis)
                self.assertEqual((len(analysis.red_flags), len(analysis.good_signs)), (1, 2))
                self.assertEqual(report_list_items(report, "Questions to Ask Your Contractor"),
                                 analysis.contractor_questions)
                self.assertEqual(analysis.model_dump(), before)

    def test_empty_and_whitespace_findings_keep_existing_visibility_and_counts(self):
        for flags in ([], ["", "  "], ["  Check <motor> & controls.  "]):
            for signs in ([], ["", "\n"], ["  Parts & labor warranty.  "]):
                with self.subTest(flags=flags, signs=signs):
                    analysis = saved_response()
                    analysis.red_flags, analysis.good_signs = flags, signs
                    before = analysis.model_dump()
                    report = main.build_report_html(analysis, 1)
                    expected = [title for title in SECTION_ORDER if not (
                        (title == "Red Flags" and not any(s.strip() for s in flags)) or
                        (title == "Good Signs" and not any(s.strip() for s in signs)))]
                    self.assertEqual(report_sections(report), expected)
                    self.assert_findings(report, analysis)
                    self.assertEqual(analysis.model_dump(), before)

    def test_empty_optional_sections_are_not_resurrected(self):
        analysis = saved_response()
        analysis.homeowner_takeaway = ""
        analysis.equipment_analysis = " "
        analysis.repair_evidence_sections = []
        analysis.missing_information = "No material missing information."
        analysis.pricing_review = ""
        analysis.installation_concerns = ""
        analysis.contractor_questions = []
        report = main.build_report_html(analysis, 1)
        self.assertEqual(report_sections(report), SECTION_ORDER[:5] + SECTION_ORDER[-2:])
        self.assert_findings(report, analysis)

    def test_single_quote_hides_stale_comparison_and_multi_quote_preserves_it(self):
        analysis = saved_response()
        analysis.quote_comparison = "Quote A and quote B have different scopes."
        analysis.best_quote_recommendation = "Quote B has the clearer scope."
        before = analysis.model_dump()
        for count in (1, 2, 3, None):
            with self.subTest(quote_count=count):
                report = main.build_report_html(analysis, count)
                extras = [] if count == 1 else ["Quote Comparison", "Best Quote Recommendation"]
                self.assertEqual(report_sections(report), SECTION_ORDER[:-3] + extras + SECTION_ORDER[-3:])
                for value in (analysis.quote_comparison, analysis.best_quote_recommendation):
                    self.assertEqual(value in report, count != 1)
                self.assertNotIn(analysis.contractor_vetting, html.unescape(report))
                self.assert_findings(report, analysis)
                self.assertEqual(analysis.model_dump(), before)

    def test_upload_renders_all_three_tiers_at_supported_quote_counts(self):
        for tier, counts in (("tier1", (1,)), ("tier2", (1, 2, 3)), ("tier3", (1, 2, 3))):
            for count in counts:
                with self.subTest(tier=tier, quote_count=count):
                    incoming = saved_response()
                    incoming.quote_comparison = "Quote A and quote B have different scopes."
                    incoming.best_quote_recommendation = "Quote B has the clearer scope."
                    before = incoming.model_dump()
                    responses = [SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(parsed=value))]) for value in (classified(), incoming)]
                    with TemporaryDirectory(prefix="cyt-layout-") as directory, \
                            patch.object(main, "UPLOAD_DIR", Path(directory)), \
                            contextlib.redirect_stdout(io.StringIO()), \
                            patch.object(main.client.beta.chat.completions, "parse", side_effect=responses) as api, \
                            patch.object(main, "send_review_email") as email:
                        response = TestClient(main.app).post("/upload", data={
                            "package": tier, "customer_name": "Offline", "customer_email": "offline@example.com",
                        }, files=[("files", (f"quote-{i}.txt", BAD.encode(), "text/plain")) for i in range(count)])
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(api.call_count, 2)
                    sent = email.call_args.kwargs
                    self.assertEqual(sent["package_key"], tier)
                    self.assertEqual(len(sent["file_names"]), count)
                    analysis = sent["analysis"]
                    report = response.text
                    self.assertEqual(report, main.build_report_html(analysis, count))
                    extras = [] if count == 1 else ["Quote Comparison", "Best Quote Recommendation"]
                    self.assertEqual(report_sections(report), SECTION_ORDER[:-3] + extras + SECTION_ORDER[-3:])
                    self.assert_findings(report, analysis)
                    self.assertEqual((len(analysis.red_flags), len(analysis.good_signs)), (1, 2))
                    messages = api.call_args.kwargs["messages"]
                    self.assertEqual("Contractor vetting search results:" in messages[1]["content"], tier == "tier3")
                    self.assertEqual(incoming.model_dump(), before)

    def test_tier_quote_limits_still_reject_before_model_calls(self):
        for tier, count in (("tier1", 2), ("tier2", 4), ("tier3", 4)):
            with self.subTest(tier=tier), patch.object(main.client.beta.chat.completions, "parse") as api, \
                    patch.object(main, "send_review_email") as email:
                response = TestClient(main.app).post("/analyze", json={"packageName": tier, "files": [
                    {"fileName": f"quote-{i}.txt", "extractedText": BAD} for i in range(count)]})
                self.assertEqual(response.status_code, 400)
                api.assert_not_called()
                email.assert_not_called()

    def test_pdf_print_control_and_finding_card_styles_are_preserved(self):
        report = main.build_report_html(finalize(saved_response()), 1)
        self.assertEqual(report.count('onclick="window.print()"'), 1)
        self.assertRegex(report, r'<button class="download-btn" onclick="window.print\(\)">\s*Download / Save as PDF\s*</button>')
        self.assertRegex(report, r'@media print\s*\{\s*\.download-btn\s*\{\s*display: none;\s*\}')
        self.assertRegex(report, r'@media print[\s\S]*body\s*\{\s*background: white;\s*\}')
        self.assertRegex(report, r'@media print[\s\S]*\.cyt-report\s*\{\s*box-shadow: none;\s*margin: 0;\s*\}')
        for heading in ("Red Flags", "Good Signs"):
            self.assertRegex(report, rf'<div class="card">\s*<h2>{heading}</h2>')
        self.assertLess(report.index("<h2>Review Complete</h2>"), report.index('onclick="window.print()"'))


if __name__ == "__main__":
    unittest.main()
