"""Deduplicate the same valve diagnosis, not independent technical questions."""
import contextlib
import html
import io
from pathlib import Path
import unittest

import main
from commissioning import commissioning_items
from refrigerant_system import CAUSE
from test_mini_split_scope_routing import classification
from test_repair_diagnostics import upload
from test_technical_support_derivation import assessment
from test_mini_split_question_materiality import (
    saved_response as thermistor_response, SOURCE as THERMISTOR_SOURCE,
)


RESPONSE = Path(__file__).with_name("test_fixtures") / "mini_split_eev_final_20261108_181117.json"
SOURCE = """Existing ductless mini-split.
Complaint: System is not cooling properly.
Contractor recommends replacing the electronic expansion valve (EEV) only.
No valve-specific measurements, manufacturer diagnostic results, or component isolation findings documented.
Scope includes EEV replacement and verification of system operation after repair.
No new installation, complete system replacement, or capacity change proposed.
Total repair price: $850 lump sum, parts and labor included."""


def saved_response():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


def finalize(raw=None, source=SOURCE):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw or saved_response(), source, 1,
                                              classification(), fresh_analysis=True)


class EEVDiagnosticQuestionDeduplicationTests(unittest.TestCase):
    def assert_accepted(self, result):
        raw = saved_response()
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(result.red_flags, raw.red_flags)
        self.assertEqual(result.good_signs, raw.good_signs)
        self.assertEqual(result.price_facts, raw.price_facts)
        self.assertEqual(result.contractor_questions, [raw.contractor_questions[0], raw.contractor_questions[2]])
        self.assertFalse(main.sizing_assessments(result))
        self.assertFalse(commissioning_items(result))
        self.assertFalse(any(main.replacement_basis_question(q) for q in result.contractor_questions))

    def test_exact_saved_three_question_response_through_production_upload(self):
        raw = saved_response()
        before = raw.model_dump()
        self.assertEqual(len(raw.contractor_questions), 3)
        result, report = upload(raw, SOURCE, classification())
        self.assert_accepted(result)
        self.assertEqual([a.model_dump() for a in result.technical_assessments],
                         [a.model_dump() for a in raw.technical_assessments])
        self.assertEqual(report, main.build_report_html(result, 1))
        self.assertNotIn(raw.contractor_questions[1], html.unescape(report))
        self.assertNotIn(raw.contractor_questions[1], result.model_dump_json())
        for question in result.contractor_questions:
            self.assertEqual(html.unescape(report).count(question), 1)
        self.assertNotIn("How Will Startup Be Verified?", report)
        self.assertEqual(raw.model_dump(), before)

    def test_equivalent_diagnostic_wording_keeps_the_specific_valve_question(self):
        for question in (
            "Can you detail what diagnostic tests were performed to isolate the issue with the cooling of the system?",
            "Which diagnostic findings isolate the cause of the cooling problem?",
            "What tests were performed to determine why the system is not cooling properly?",
            "Can you explain which diagnostic measurements identify the cause of poor cooling?",
            "What evidence isolates the cause of the system cooling issue?",
        ):
            for secondary_label in (True, False):
                with self.subTest(question=question, secondary_label=secondary_label):
                    raw = saved_response()
                    raw.contractor_questions[1] = question
                    if not secondary_label:
                        raw.technical_assessments = raw.technical_assessments[:1]
                    self.assert_accepted(finalize(raw))

    def test_distinct_source_supported_component_gap_keeps_its_question(self):
        raw = saved_response()
        source = SOURCE + "\nThe indoor blower motor is noisy. Replace the blower motor; isolation findings are not documented."
        raw.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            subject="Blower motor diagnosis", evidence=["The indoor blower motor is noisy."],
            gaps=["The blower noise has not been isolated to motor failure."]))
        raw.contractor_questions.insert(0, "What diagnostic evidence supports replacing the blower motor?")
        result = finalize(raw, source)
        self.assertTrue(any("blower" in q.lower() for q in result.contractor_questions))
        self.assertIn(raw.contractor_questions[1], result.contractor_questions)
        self.assertTrue(any("noisy" in e for a in result.technical_assessments for e in a.documented_evidence))

    def test_independent_generic_assessment_is_not_assumed_to_mean_valve_failure(self):
        raw = saved_response()
        # The same generic question can be material when another cooling issue
        # is actually documented. Its assessment must not inherit valve ownership.
        source = SOURCE + "\nThe upstairs room remains warm while the downstairs room cools normally."
        raw.technical_assessments[1] = assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            subject="Uneven room cooling", evidence=[source.splitlines()[-1]],
            gaps=["The cause of uneven room cooling has not been isolated."])
        self.assertIn(raw.contractor_questions[1], finalize(raw, source).contractor_questions)

    def test_separate_refrigerant_concern_preserves_generic_and_specific_questions(self):
        raw = saved_response()
        concern = "The documented service-valve leak remains unrepaired."
        raw.technical_assessments.append(assessment("CONTRADICTORY", "UNSUPPORTED",
            subject=CAUSE, materiality="MATERIAL_SECONDARY", evidence=[concern], contradictions=[concern]))
        result = finalize(raw, SOURCE + "\n" + concern)
        self.assertIn(raw.contractor_questions[0], result.contractor_questions)
        self.assertIn(raw.contractor_questions[1], result.contractor_questions)
        self.assertTrue(any("leak" in q.lower() for q in result.contractor_questions))

    def test_separately_documented_valve_issue_does_not_inherit_one_diagnosis(self):
        raw = saved_response()
        source = SOURCE + "\nA separate outdoor LEV fault is reported. Replace the outdoor LEV."
        raw.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            subject="Outdoor LEV diagnosis", evidence=["A separate outdoor LEV fault is reported."],
            gaps=["No outdoor LEV isolation findings are documented."]))
        result = finalize(raw, source)
        self.assertIn(raw.contractor_questions[1], result.contractor_questions)
        self.assertTrue(any("outdoor LEV" in e for a in result.technical_assessments for e in a.documented_evidence))

    def test_documented_conflicting_result_is_not_treated_as_duplicate_absence(self):
        raw = saved_response()
        conflict = "The EEV responded normally during testing but the system still did not cool."
        raw.technical_assessments[1] = assessment("CONTRADICTORY", "UNSUPPORTED",
            subject="System cooling issue", evidence=[conflict],
            gaps=["No findings isolate the EEV as the cause."], contradictions=[conflict])
        result = finalize(raw, SOURCE + "\n" + conflict)
        self.assertIn(raw.contractor_questions[1], result.contractor_questions)
        self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))

    def test_question_with_distinct_source_detail_survives(self):
        raw = saved_response()
        question = "What diagnostic tests explain the rattling noise at the outdoor unit?"
        raw.contractor_questions.insert(1, question)
        result = finalize(raw, SOURCE + "\nA separate rattling noise at the outdoor unit is reported.")
        self.assertIn(question, result.contractor_questions)
        self.assertIn(raw.contractor_questions[0], result.contractor_questions)

    def test_verification_question_is_a_separate_purpose_when_missing(self):
        raw = saved_response()
        raw.good_signs = []
        result = finalize(raw, SOURCE.replace("Scope includes EEV replacement and verification of system operation after repair.\n", ""))
        self.assertIn("How will operation be verified after the repair?", result.contractor_questions)
        self.assertIn(raw.contractor_questions[0], result.contractor_questions)
        self.assertNotIn(raw.contractor_questions[1], result.contractor_questions)

    def test_thermistor_acceptance_is_unchanged(self):
        raw = thermistor_response()
        result = finalize(raw, THERMISTOR_SOURCE)
        self.assertEqual(result.contractor_questions, [raw.contractor_questions[0], raw.contractor_questions[3]])
        self.assertEqual(result.red_flags, raw.red_flags)
        self.assertEqual(result.good_signs, raw.good_signs)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")

    def test_eev_and_lev_component_routing_remains_intact(self):
        for source, raw in (
            (SOURCE, saved_response()),
            (SOURCE.replace("EEV", "LEV").replace("electronic expansion", "linear expansion"),
             main.HVACAnalysis.model_validate_json(RESPONSE.read_text().replace("EEV", "LEV").replace("electronic expansion", "linear expansion"))),
        ):
            with self.subTest(source=source):
                result = finalize(raw, source)
                self.assertFalse(main.sizing_assessments(result))
                self.assertFalse(commissioning_items(result))
                self.assertEqual(len(result.contractor_questions), 2)
                self.assertIn("EEV/LEV", result.contractor_questions[0])

    def test_pricing_order_and_limit_are_preserved(self):
        raw = saved_response()
        raw.contractor_questions += raw.contractor_questions * 3
        result = finalize(raw)
        self.assert_accepted(result)
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]), "pricing")
        self.assertIn("$850", result.contractor_questions[-1])
        self.assertLessEqual(len(result.contractor_questions), 6)

    def test_repeated_finalization_and_html_json_consistency(self):
        result = finalize()
        self.assert_accepted(result)
        for _ in range(3):
            again = finalize(main.HVACAnalysis.model_validate_json(result.model_dump_json()))
            self.assertEqual(result.model_dump(), again.model_dump())
            self.assertEqual(main.build_report_html(result, 1), main.build_report_html(again, 1))
            result = again


if __name__ == "__main__":
    unittest.main()
