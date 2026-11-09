"""Exact thermistor retest: routine verification detail is not a material gap."""
import html
from pathlib import Path
import unittest
from unittest.mock import patch

import main
import commissioning as cm
from test_mini_split_scope_routing import classification
from test_mini_split_startup_regression import SOURCE, finalize
from test_repair_diagnostics import upload
from test_technical_support_derivation import analysis_with, assessment


RESPONSE = Path(__file__).with_name("test_fixtures") / "mini_split_thermistor_retest_20261107_191701.json"


def retest_response():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


class MiniSplitVerificationMaterialityTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_component_boundary(self, result):
        self.assertFalse(cm.commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))
        report = main.build_report_html(result, 1)
        self.assertNotIn("How Will Startup Be Verified?", report)
        return report

    def test_exact_retest_upload_preserves_diagnosis_pricing_and_independent_positives(self):
        raw = retest_response()
        before = raw.model_dump()
        result, report = upload(raw, SOURCE, classification())
        self.assertEqual(result.red_flags, raw.red_flags[:1])
        self.assertEqual(result.contractor_questions, [raw.contractor_questions[0], raw.contractor_questions[2]])
        self.assertEqual(result.good_signs, raw.good_signs)
        self.assertEqual(result.technical_assessments, raw.technical_assessments)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(result.price_facts, raw.price_facts)
        self.assertEqual(result.analysis_integrity.status, "COMPLETE")
        self.assertEqual(result.decision.required_actions, [raw.decision.required_actions[0], raw.decision.required_actions[2]])
        self.assertEqual(report, self.assert_component_boundary(result))
        for value in (raw.red_flags[1], raw.contractor_questions[1], raw.decision.required_actions[1]):
            self.assertNotIn(value, result.model_dump_json())
            self.assertNotIn(value, html.unescape(report))
        self.assertEqual(raw.model_dump(), before)

    def test_live_wording_and_equivalent_generic_detail_requests(self):
        raw = retest_response()
        for value in (raw.red_flags[1], raw.contractor_questions[1], raw.decision.required_actions[1],
                      "The quote lacks specific details about how operational verification will be performed after repair.",
                      "Please explain the method for checking normal operation following the repair."):
            with self.subTest(value=value):
                self.assertTrue(cm.generic_component_verification(value))
                candidate = retest_response()
                candidate.red_flags.append(value)
                candidate.contractor_questions.append(value)
                candidate.decision.required_actions.append(value)
                candidate.decision.verdict_reasons.append(value)
                result = finalize(candidate)
                self.assertNotIn(value, result.model_dump_json())

    def test_without_planned_verification_retains_one_repair_followup(self):
        raw = retest_response()
        source = SOURCE.replace(" and verify operation after repair", "")
        result = finalize(raw, source)
        questions = [q for q in result.contractor_questions if main.contractor_question_category(q) == "verification"]
        self.assertEqual(questions, ["How will operation be verified after the repair?"])
        self.assertIn(raw.contractor_questions[0], result.contractor_questions)
        self.assertIn(raw.contractor_questions[-1], result.contractor_questions)
        self.assertEqual(result.good_signs, raw.good_signs[1:])
        self.assertIn("does not describe verification", result.missing_information)
        self.assert_component_boundary(result)

    def test_actual_documented_verification_conflict_survives_a_planned_promise(self):
        for conflict, question in (
                ("Operation was not verified after the repair.",
                 "Why was operation not verified after the repair?"),
                ("Verification of operation will not be conducted after the repair.",
                 "Will operation be verified despite the stated exclusion?"),
                ("The operation check after repair failed.",
                 "How will the failed operation check be corrected?")):
            with self.subTest(conflict=conflict):
                raw = retest_response()
                raw.technical_assessments.append(assessment("CONTRADICTORY", "UNSUPPORTED",
                    materiality="MATERIAL_SECONDARY", subject="Thermistor repair closeout",
                    evidence=[conflict], contradictions=[conflict]))
                raw.red_flags.append(conflict)
                raw.contractor_questions.append(question)
                result = finalize(raw, SOURCE + "\n" + conflict)
                self.assertIn(conflict, result.red_flags)
                self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))
                self.assertIn(question, result.contractor_questions)
                self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
                self.assertFalse(cm.generic_component_verification(conflict))
                self.assert_component_boundary(result)

    def test_required_specific_procedure_omission_is_preserved(self):
        requirement = "The submitted instructions require a thermistor response test after repair."
        gap = "The required thermistor response test is omitted."
        question = "Will the required thermistor response test be included after repair?"
        raw = retest_response()
        raw.technical_assessments[0].material_gaps.append(gap)
        raw.red_flags.append(gap)
        raw.contractor_questions.append(question)
        result = finalize(raw, SOURCE + "\n" + requirement + "\n" + gap)
        self.assertIn(gap, result.red_flags)
        self.assertIn(gap, result.technical_assessments[0].material_gaps)
        self.assertIn(question, result.contractor_questions)

    def test_independent_diagnostic_gap_and_question_are_preserved(self):
        raw = retest_response()
        gap = "The thermistor resistance and wiring isolation results are not documented."
        raw.technical_assessments[0].material_gaps.append(gap)
        raw.missing_information += " " + gap
        result = finalize(raw)
        self.assertEqual(result.technical_assessments, raw.technical_assessments)
        self.assertIn(gap, result.missing_information)
        self.assertEqual(result.contractor_questions, [raw.contractor_questions[0], raw.contractor_questions[-1]])

    def test_mixed_generic_and_specific_concern_is_not_discarded(self):
        raw = retest_response()
        concern = "The verification procedure is not detailed, and the thermistor resistance test conflicts with the supplied criteria."
        raw.red_flags.append(concern)
        raw.technical_assessments[0].contradictions.append(concern)
        result = finalize(raw, SOURCE + "\n" + concern)
        self.assertIn(concern, result.red_flags)
        self.assertIn(concern, result.technical_assessments[0].contradictions)

    def assert_alias_repair(self, component):
        raw = main.HVACAnalysis.model_validate_json(RESPONSE.read_text().replace("thermistor", component).replace("Thermistor", component))
        result = finalize(raw, SOURCE.replace("thermistor", component))
        self.assertNotIn(raw.red_flags[1], result.red_flags)
        self.assertNotIn(raw.contractor_questions[1], result.contractor_questions)
        self.assertNotIn(raw.decision.required_actions[1], result.decision.required_actions)
        for sign in raw.good_signs:
            self.assertIn(sign, result.good_signs)
        self.assert_component_boundary(result)

    def test_eev_and_lev_component_repairs(self):
        for component in ("EEV", "LEV"):
            with self.subTest(component=component):
                self.assert_alias_repair(component)

    def test_inverter_control_board_component_repair(self):
        self.assert_alias_repair("inverter drive board")

    def assert_system_scope(self, text):
        result = finalize(analysis_with([]), text)
        self.assertTrue(main.sizing_assessments(result))
        self.assertTrue(cm.commissioning_items(result))
        self.assertIn("How Will Startup Be Verified?", main.build_report_html(result, 1))
        self.assertTrue(any("startup" in q.lower() for q in result.contractor_questions))

    def test_new_installation_still_needs_system_commissioning(self):
        self.assert_system_scope("Install a new single-zone mini-split. Verify operation after installation.")

    def test_complete_replacement_still_needs_system_review(self):
        self.assert_system_scope("Replace the complete mini-split system. Verify operation after replacement.")

    def test_mixed_system_and_component_work_is_preserved(self):
        self.assert_system_scope("Replace indoor-unit thermistor and install a new multi-zone mini-split. Verify operation after repair.")

    def test_positives_are_not_limited_to_a_fixture_count(self):
        raw = retest_response()
        extra = "The proposal includes a 90-day labor warranty."
        raw.good_signs.append(extra)
        result = finalize(raw, SOURCE + "\n" + extra)
        self.assertEqual(result.good_signs, raw.good_signs)
        self.assertEqual(result.red_flags, raw.red_flags[:1])

    def test_question_deduplication_and_repeated_finalization(self):
        raw = retest_response()
        raw.contractor_questions += [*raw.contractor_questions,
            "Please explain the method for checking normal operation following the repair."]
        result = finalize(raw)
        self.assertEqual(result.contractor_questions, [raw.contractor_questions[0], raw.contractor_questions[2]])
        for _ in range(3):
            again = finalize(result)
            self.assertEqual(again.model_dump(), result.model_dump())
            self.assertEqual(main.build_report_html(again, 1), main.build_report_html(result, 1))
            result = again


if __name__ == "__main__":
    unittest.main()
