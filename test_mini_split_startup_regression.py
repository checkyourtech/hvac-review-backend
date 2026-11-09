"""Replay the saved thermistor response through offline production finalization."""
import contextlib
import html
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import main
from commissioning import commissioning_items, commissioning_required, isolated_component_repair
from evidence_integrity import component_repair_work
from system_sizing import sizing_required
from test_mini_split_scope_routing import classification, classify
from test_repair_diagnostics import upload
from test_technical_support_derivation import analysis_with


RESPONSE = Path(__file__).with_name("test_fixtures") / "mini_split_thermistor_live_response_20261107.json"
SOURCE = """Existing ductless mini-split.
Indoor temperature sensor fault reported.
Proposed scope: Replace indoor-unit thermistor only and verify operation after repair.
No system replacement, installation, or capacity change.
No diagnostic measurements documented.
Total repair price: $300 lump sum, parts and labor included."""


def saved_response():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


def finalize(raw=None, source=SOURCE):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw or saved_response(), source, 1,
                                              classification(), fresh_analysis=True)


class MiniSplitStartupRegressionTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_no_system_startup(self, result):
        self.assertFalse(commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))
        self.assertNotIn("startup", result.model_dump_json().lower())
        self.assertNotIn("commissioning", result.model_dump_json().lower())
        report = main.build_report_html(result, 1)
        self.assertNotIn("How Will Startup Be Verified?", report)
        self.assertNotIn("Quote Comparison", report)
        self.assertNotIn("Best Quote Recommendation", report)
        return report

    def test_exact_live_response_through_upload_preserves_accepted_results(self):
        incoming = saved_response()
        before = incoming.model_dump()
        result, report = upload(incoming, SOURCE, classification())
        self.assertEqual(report, self.assert_no_system_startup(result))
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(result.red_flags, incoming.red_flags)
        self.assertEqual(result.good_signs, incoming.good_signs)
        self.assertEqual(result.technical_assessments, incoming.technical_assessments[:1])
        self.assertEqual(result.price_facts, incoming.price_facts)
        self.assertEqual(result.contractor_questions,
                         [incoming.contractor_questions[0], incoming.contractor_questions[-1]])
        self.assertEqual(result.analysis_integrity.status, "COMPLETE")
        self.assertEqual(incoming.model_dump(), before)
        for question in result.contractor_questions:
            self.assertIn(question, html.unescape(report))

    def test_unit_modifier_is_component_scope_not_system_installation(self):
        for scope in ("Replace indoor-unit thermistor.", "Replace indoor unit thermistor.",
                      "Replace mini-split indoor-unit thermistor.",
                      "Replace outdoor-unit EEV.", "Replace outdoor unit inverter drive board."):
            with self.subTest(scope=scope):
                self.assertTrue(component_repair_work(scope))
                self.assertTrue(isolated_component_repair(scope))
                self.assertFalse(sizing_required(scope))
                self.assertFalse(commissioning_required(scope))
                self.assertNotIn(main.AnalysisModule.COMMISSIONING, classify(scope).modules_required)

    def test_thermistor_without_verification_keeps_one_repair_question(self):
        source = SOURCE.replace(" and verify operation after repair", "")
        incoming = saved_response()
        for item in incoming.technical_assessments:
            item.documented_evidence = [s.replace(" and verify operation after repair", "")
                                        for s in item.documented_evidence]
        result = finalize(incoming, source)
        report = self.assert_no_system_startup(result)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertFalse(result.good_signs)
        verification = [q for q in result.contractor_questions if main.contractor_question_category(q) == "verification"]
        self.assertEqual(len(verification), 1)
        self.assertIn("after the repair", verification[0])
        self.assertIn(verification[0], html.unescape(report))
        self.assertEqual(len(result.red_flags), 1)

    def test_eev_only_repair_with_operation_verification(self):
        raw = main.HVACAnalysis.model_validate_json(RESPONSE.read_text().replace("thermistor", "EEV").replace("Thermistor", "EEV"))
        result = finalize(raw, SOURCE.replace("thermistor", "EEV"))
        self.assert_no_system_startup(result)
        self.assertTrue(any("verification step" in sign for sign in result.good_signs))
        self.assertFalse(any("how will we confirm" in q for q in result.contractor_questions))

    def test_inverter_board_only_repair_with_operation_verification(self):
        raw = main.HVACAnalysis.model_validate_json(RESPONSE.read_text().replace("thermistor", "inverter drive board").replace("Thermistor", "Inverter Drive Board"))
        result = finalize(raw, SOURCE.replace("thermistor", "inverter drive board"))
        self.assert_no_system_startup(result)
        self.assertTrue(any("verification step" in sign for sign in result.good_signs))
        self.assertFalse(any("how will we confirm" in q for q in result.contractor_questions))

    def assert_system_commissioning(self, scope):
        source = scope + " Verify operation after repair."
        result = finalize(analysis_with([]), source)
        self.assertTrue(commissioning_required(source))
        self.assertTrue(main.sizing_assessments(result))
        self.assertEqual(len(commissioning_items(result)), 1)
        self.assertIn("How Will Startup Be Verified?", main.build_report_html(result, 1))
        self.assertTrue(any("startup" in q.lower() for q in result.contractor_questions))

    def test_new_single_zone_installation_retains_commissioning(self):
        self.assert_system_commissioning("Install a new single-zone mini-split.")

    def test_new_multi_zone_installation_retains_commissioning(self):
        self.assert_system_commissioning("Install a new multi-zone mini-split with indoor-unit thermistors.")

    def test_full_system_replacement_retains_commissioning(self):
        self.assert_system_commissioning("Replace the complete mini-split system.")

    def test_major_retrofit_retains_commissioning(self):
        self.assert_system_commissioning("Install an additional indoor head. Increase system capacity for the addition.")

    def test_mixed_system_and_component_work_retains_commissioning(self):
        self.assert_system_commissioning("Replace indoor-unit thermistor and install a new mini-split.")

    def test_repeated_finalization_and_question_deduplication(self):
        incoming = saved_response()
        incoming.contractor_questions += [incoming.contractor_questions[1],
            "How will operation be verified after the repair?",
            "What checks will confirm normal operation following the repair?"]
        result = finalize(incoming)
        self.assertEqual(len(result.contractor_questions), 2)
        for _ in range(3):
            again = finalize(result)
            self.assertEqual(again.model_dump(), result.model_dump())
            self.assertEqual(main.build_report_html(again, 1), self.assert_no_system_startup(result))
            result = again

    def test_legacy_startup_fields_removed_without_startup_assessment(self):
        raw = saved_response()
        raw.technical_assessments = raw.technical_assessments[:1]
        result = finalize(raw)
        self.assert_no_system_startup(result)
        self.assertEqual(len(result.contractor_questions), 2)

    def test_specific_repair_requirement_is_not_answered_by_generic_operation_promise(self):
        requirement = "Required post-repair safety verification is excluded."
        raw = saved_response()
        raw.technical_assessments[1].documented_evidence = [requirement]
        raw.technical_assessments[1].contradictions = [requirement]
        raw.technical_assessments[1].diagnostic_evidence_status = "CONTRADICTORY"
        raw.technical_assessments[1].scope_support = "UNSUPPORTED"
        result = finalize(raw, SOURCE + "\n" + requirement)
        self.assertTrue(any(requirement in a.contradictions for a in result.technical_assessments))
        self.assertTrue(commissioning_items(result))
        self.assertIn("How Will Startup Be Verified?", main.build_report_html(result, 1))

    def test_separately_documented_startup_plan_is_preserved(self):
        source = SOURCE + "\nManufacturer startup procedure and documented final readings are included."
        result = finalize(saved_response(), source)
        self.assertEqual(len(commissioning_items(result)), 1)
        self.assertEqual(commissioning_items(result)[0].scope_support, "APPROPRIATE")
        self.assertIn("How Will Startup Be Verified?", main.build_report_html(result, 1))

    def test_specific_verification_question_and_evidence_gap_are_preserved(self):
        raw = saved_response()
        gap = "The thermistor resistance check after repair is not described."
        question = "How will thermistor resistance be checked after repair?"
        raw.technical_assessments[0].material_gaps.append(gap)
        raw.contractor_questions.append(question)
        result = finalize(raw)
        self.assertIn(gap, result.technical_assessments[0].material_gaps)
        self.assertIn(question, result.contractor_questions)
        self.assert_no_system_startup(result)

    def test_negated_or_conditional_verification_is_not_a_commitment(self):
        for promise in ("Operation will not be verified after repair.",
                        "Verify operation after repair if needed."):
            source = SOURCE.replace(" and verify operation after repair", "") + "\n" + promise
            raw = saved_response()
            raw.technical_assessments[0].documented_evidence = []
            result = finalize(raw, source)
            self.assertFalse(result.good_signs)
            self.assertTrue(any("after the repair" in q for q in result.contractor_questions))
            self.assert_no_system_startup(result)

    def test_pre_repair_operation_check_is_not_post_repair_verification(self):
        source = SOURCE.replace(" and verify operation after repair", "") + "\nVerify operation before repair."
        raw = saved_response()
        raw.technical_assessments[0].documented_evidence = []
        result = finalize(raw, source)
        self.assertFalse(result.good_signs)
        self.assertTrue(any("after the repair" in q for q in result.contractor_questions))
        self.assert_no_system_startup(result)

    def test_reasonable_operational_plan_does_not_need_individual_procedures(self):
        source = SOURCE.replace("verify operation after repair",
                                "verify operation after repair using the supplied service instructions")
        result = finalize(saved_response(), source)
        self.assert_no_system_startup(result)
        self.assertEqual(len(result.contractor_questions), 2)
        self.assertEqual(result.good_signs, saved_response().good_signs)


if __name__ == "__main__":
    unittest.main()
