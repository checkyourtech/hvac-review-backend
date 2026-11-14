"""Saved final report cleanup; full replay reuses the documented scenario helper.

The response is the exact saved JSON. SOURCE combines the previously embedded
excerpt and user-supplied scenario; it is not a recovered verbatim submission.
"""
import html
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import main
from test_mini_split_multizone_live import SOURCE, installed_classification, finalized_live
from test_mini_split_installation import item
from test_repair_diagnostics import upload

RESPONSE = Path(__file__).with_name("test_fixtures") / "mini_split_multizone_final_20261112_210814.json"
GENERIC = "Is there documented low-outdoor-temperature heating capacity information for this system?"
COMBINED = "What manufacturer heating capacity data at the winter design conditions supports the heating load, and is supplemental heat needed?"


def saved():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


class MultiZoneReportCleanupTests(unittest.TestCase):
    def test_exact_saved_questions_remove_only_overlap_without_filler(self):
        raw = saved()
        before = raw.model_dump()
        self.assertEqual(len(raw.contractor_questions), 6)
        self.assertEqual(raw.contractor_questions[0], GENERIC)
        self.assertIn(COMBINED, raw.contractor_questions)
        questions = main.build_contractor_questions(raw, 1, SOURCE)
        self.assertEqual(questions, raw.contractor_questions[1:])
        self.assertEqual(raw.model_dump(), before)

    def test_equivalent_capacity_requests_prefer_combined_question_in_either_order(self):
        for generic in (GENERIC,
                        "Can you provide cold-weather heating output data for the proposed system?",
                        "Is low-temperature heating capacity documentation available?",
                        "What heating capacity information is available at winter design conditions?"):
            for pair in ([generic, COMBINED], [COMBINED, generic]):
                with self.subTest(pair=pair):
                    raw = saved()
                    price = raw.contractor_questions[-1]
                    raw.contractor_questions = [*pair, price]
                    self.assertEqual(main.build_contractor_questions(raw, 1, SOURCE), [COMBINED, price])

    def test_lone_capacity_question_is_kept_without_inventing_another(self):
        raw = saved()
        price = raw.contractor_questions[-1]
        raw.contractor_questions = [GENERIC, price]
        self.assertEqual(main.build_contractor_questions(raw, 1, SOURCE), [GENERIC, price])

    def test_distinct_specific_capacity_or_defect_questions_are_not_generic_duplicates(self):
        for independent in (
            "How will the documented heating capacity shortfall at winter design conditions be addressed?",
            "Can you provide cold-weather heating capacity data for the separate upstairs room?",
            "Can you provide cold-weather heating capacity data for the upstairs zones?",
            "Can you provide heating capacity data at the winter design temperature of 5 F?",
        ):
            with self.subTest(independent=independent):
                raw = saved()
                raw.contractor_questions = [GENERIC, COMBINED, independent]
                questions = main.build_contractor_questions(raw, 1, SOURCE)
                self.assertNotIn(GENERIC, questions)
                self.assertIn(COMBINED, questions)
                self.assertIn(independent, questions)

    def test_saved_numbered_fragments_lose_only_labels(self):
        raw = saved()
        self.assertIn("1. 2. 3.", raw.missing_information)
        expected = re.sub(r"(?<!\S)\d\.(?=\s)", "", raw.missing_information)
        expected = " ".join(expected.split())
        normalized = main.normalize_missing_information_formatting(raw.missing_information)
        self.assertEqual(normalized, expected)
        self.assertEqual(main.normalize_missing_information_formatting(normalized), normalized)

    def test_numbered_content_preserves_measurements_and_document_references(self):
        text = "Missing details: 1. Confirm the 1.5-ton selection. 2. Clarify 24,000 and 30,000 BTU/h ratings. 3. Supply section 1.2.3 of the instructions."
        self.assertEqual(main.normalize_missing_information_formatting(text),
            "Missing details: Confirm the 1.5-ton selection. Clarify 24,000 and 30,000 BTU/h ratings. Supply section 1.2.3 of the instructions.")
        for ordinary in ("A 1.5-ton selection needs support.", "See document 1. Page 2. Has the referenced requirement been supplied?", "No material information is missing."):
            self.assertEqual(main.normalize_missing_information_formatting(ordinary), ordinary)

    def test_numbered_missing_items_are_cleaned_before_ownership_filters(self):
        raw = saved()
        raw.missing_information = (
            "Missing details: 1. Manufacturer and equipment model numbers. "
            "2. Approved combination documentation. 3. Documentation for connected-zone capacity. "
            "4. Drainage discharge approval is not documented. 5. Roof access remains unspecified.")
        result = finalized_live(raw, SOURCE + "\nDrainage discharge approval is not documented. Roof access remains unspecified.")
        for gap in ("connected-zone capacity", "Drainage discharge approval is not documented.", "Roof access remains unspecified."):
            self.assertIn(gap, result.missing_information)
        self.assertIn("approved combination", result.missing_information)
        self.assertNotRegex(result.missing_information, r"(?<!\S)\d\.(?=\s)")

    def test_saved_support_pricing_flags_and_installation_credit_remain(self):
        raw = saved()
        result = finalized_live(raw)
        self.assertEqual(result.support_heading, "Installation Support")
        self.assertEqual(result.decision.technical_support, raw.decision.technical_support)
        self.assertEqual(result.decision.verdict, raw.decision.verdict)
        self.assertEqual(result.decision.pricing_transparency, raw.decision.pricing_transparency)
        self.assertEqual(result.red_flags, raw.red_flags)
        self.assertEqual(result.good_signs, raw.good_signs)
        self.assertEqual(result.price_facts, raw.price_facts)
        self.assertEqual(main.derive_technical_support(result.technical_assessments), result.decision.technical_support)
        for gap in ("approved combination", "connected-zone capacity", "heating and cooling loads", "winter design", "supplemental", "startup results or records", "Itemized breakdown"):
            self.assertIn(gap, result.missing_information)
        self.assertLessEqual(len(result.contractor_questions), 6)
        self.assertIn(COMBINED, result.contractor_questions)
        self.assertNotIn(GENERIC, result.contractor_questions)

    def test_independent_documented_red_flag_and_question_survive(self):
        raw = saved()
        concern = "The proposed outdoor mounting location has documented structural damage and cannot support the equipment."
        question = "How will the documented structural damage at the mounting location be corrected?"
        raw.technical_assessments.append(item("Mounting structure safety", [concern], "CONTRADICTORY", "UNSUPPORTED", conflicts=[concern]))
        raw.red_flags.append(concern)
        raw.contractor_questions.insert(0, question)
        result = finalized_live(raw, SOURCE + "\n" + concern)
        self.assertIn(concern, result.red_flags)
        self.assertIn(question, result.contractor_questions)
        self.assertIn(COMBINED, result.contractor_questions)
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")

    def test_upload_json_html_parity_and_repeated_finalization(self):
        with patch.object(main, "finalize_customer_analysis", wraps=main.finalize_customer_analysis) as finalizer:
            result, report = upload(saved(), SOURCE, installed_classification())
        submitted_source = finalizer.call_args.kwargs["quote_text"]
        self.assertEqual(report, main.build_report_html(result, 1))
        self.assertNotIn("1. 2. 3.", report)
        self.assertIn(result.missing_information, html.unescape(report))
        self.assertNotIn(GENERIC, html.unescape(report))
        for question in result.contractor_questions:
            self.assertEqual(html.unescape(report).count(question), 1)
        for _ in range(3):
            again = finalized_live(main.HVACAnalysis.model_validate_json(result.model_dump_json()), submitted_source)
            self.assertEqual(result.model_dump(), again.model_dump())
            self.assertEqual(report, main.build_report_html(again, 1))
            result = again


if __name__ == "__main__":
    unittest.main()
