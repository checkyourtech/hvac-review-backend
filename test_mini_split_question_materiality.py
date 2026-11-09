"""Replay the final four-question thermistor response without live model calls."""
import contextlib
import html
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import main
from commissioning import commissioning_items
from test_mini_split_scope_routing import classification
from test_repair_diagnostics import upload
from test_technical_support_derivation import analysis_with, assessment


RESPONSE = Path(__file__).with_name("test_fixtures") / "mini_split_thermistor_final_20261108_102153.json"
SOURCE = """Existing ductless mini-split.
Reported indoor temperature sensor fault.
Replace indoor-unit thermistor only.
Verify operation after repair.
No system replacement or capacity change.
No documented diagnostic measurements.
Total repair price: $300 lump sum, parts and labor included."""


def saved_response():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


def finalize(raw=None, source=SOURCE):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw or saved_response(), source, 1,
                                              classification(), fresh_analysis=True)


class MiniSplitQuestionMaterialityTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_component_boundary(self, result):
        self.assertFalse(commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))
        report = main.build_report_html(result, 1)
        self.assertNotIn("How Will Startup Be Verified?", report)
        self.assertNotIn("Quote Comparison", report)
        return report

    def test_exact_saved_four_question_response_through_production_upload(self):
        raw = saved_response()
        before = raw.model_dump()
        self.assertEqual(len(raw.contractor_questions), 4)
        result, report = upload(raw, SOURCE, classification())
        self.assertEqual(result.contractor_questions, [raw.contractor_questions[0], raw.contractor_questions[3]])
        self.assertEqual(result.red_flags, raw.red_flags)
        self.assertEqual(result.good_signs, raw.good_signs)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(result.price_facts, raw.price_facts)
        self.assertEqual(result.technical_assessments[0].diagnostic_evidence_status, "ABSENT")
        self.assertEqual(result.technical_assessments[0].scope_support, "UNSUPPORTED")
        self.assertEqual(report, self.assert_component_boundary(result))
        for question in raw.contractor_questions[1:3]:
            self.assertNotIn(question, result.model_dump_json())
            self.assertNotIn(question, html.unescape(report))
        for question in result.contractor_questions:
            self.assertIn(question, html.unescape(report))
        self.assertEqual(raw.model_dump(), before)

    def test_planned_operation_check_answers_generic_procedure_requests(self):
        for question in (saved_response().contractor_questions[2],
                         "What will you do to confirm normal operation after the repair?",
                         "How will operation be verified following the repair?"):
            with self.subTest(question=question):
                raw = saved_response()
                raw.contractor_questions[2] = question
                result = finalize(raw)
                self.assertNotIn(question, result.contractor_questions)
                self.assertEqual(result.contractor_questions, [raw.contractor_questions[0], raw.contractor_questions[3]])

    def test_genuinely_missing_verification_retains_a_material_followup(self):
        raw = saved_response()
        raw.good_signs = []
        result = finalize(raw, SOURCE.replace("Verify operation after repair.\n", ""))
        self.assertEqual(result.contractor_questions, [raw.contractor_questions[0],
            "How will operation be verified after the repair?", raw.contractor_questions[3]])
        self.assert_component_boundary(result)

    def test_documented_verification_contradiction_retains_its_question(self):
        raw = saved_response()
        conflict = "Operation was not verified after the repair."
        question = "Why was operation not verified after the repair?"
        raw.technical_assessments.append(assessment("CONTRADICTORY", "UNSUPPORTED",
            materiality="MATERIAL_SECONDARY", subject="Thermistor repair closeout",
            evidence=[conflict], contradictions=[conflict]))
        raw.contractor_questions.insert(1, question)
        result = finalize(raw, SOURCE + "\n" + conflict)
        self.assertIn(question, result.contractor_questions)
        self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")

    def test_source_supported_independent_component_concern_is_preserved(self):
        for concern in ("The control board has intermittent communication faults.",
                        "A damaged control board connector is documented."):
            with self.subTest(concern=concern):
                raw = saved_response()
                result = finalize(raw, SOURCE + "\n" + concern)
                self.assertIn(raw.contractor_questions[1], result.contractor_questions)
                self.assertIn(raw.contractor_questions[0], result.contractor_questions)
                self.assertIn(raw.contractor_questions[3], result.contractor_questions)
                self.assertNotIn(raw.contractor_questions[2], result.contractor_questions)

    def test_unsupported_other_component_speculation_is_removed(self):
        for question in (saved_response().contractor_questions[1],
                         "Could another component fail after the repair?",
                         "Might other parts cause future issues after this repair?"):
            with self.subTest(question=question):
                raw = saved_response()
                raw.contractor_questions[1] = question
                self.assertNotIn(question, finalize(raw).contractor_questions)

    def test_healthy_or_merely_mentioned_parts_do_not_justify_speculation(self):
        for text in ("The system contains a control board.",
                     "The control board tested normal.",
                     "No control board fault was reported.",
                     "No other component failures were reported."):
            with self.subTest(text=text):
                raw = saved_response()
                result = finalize(raw, SOURCE + "\n" + text)
                self.assertNotIn(raw.contractor_questions[1], result.contractor_questions)

    def test_model_only_independent_failure_claim_is_not_source_evidence(self):
        raw = saved_response()
        raw.technical_assessments.append(assessment("CONTRADICTORY", "UNSUPPORTED",
            subject="Control board diagnosis", materiality="MATERIAL_SECONDARY",
            evidence=["The control board has intermittent faults."],
            contradictions=["The control board has intermittent faults."]))
        self.assertNotIn(raw.contractor_questions[1], finalize(raw).contractor_questions)

    def test_alternative_cause_diagnostic_question_is_not_future_speculation(self):
        raw = saved_response()
        question = "Have other components been ruled out as the cause of the reported sensor fault?"
        raw.contractor_questions[1] = question
        self.assertIn(question, finalize(raw).contractor_questions)

    def test_eev_and_lev_component_only_questions(self):
        for component in ("EEV", "LEV"):
            with self.subTest(component=component):
                raw = main.HVACAnalysis.model_validate_json(RESPONSE.read_text().replace("thermistor", component).replace("Thermistor", component))
                source = SOURCE.replace("thermistor", component).replace(
                    "Reported indoor temperature sensor fault.", "Reported metering-device fault.")
                result = finalize(raw, source)
                self.assertNotIn(raw.contractor_questions[1], result.contractor_questions)
                self.assertNotIn(raw.contractor_questions[2], result.contractor_questions)
                self.assertTrue(any(main.contractor_question_category(q) == "pricing" for q in result.contractor_questions))
                self.assert_component_boundary(result)

    def test_valve_repair_with_separate_sensor_fault_preserves_component_concern(self):
        raw = main.HVACAnalysis.model_validate_json(RESPONSE.read_text().replace("thermistor", "EEV").replace("Thermistor", "EEV"))
        # The original source's separately reported sensor fault is independent
        # of the valve repair; a valve-only source must not silently replace it.
        result = finalize(raw, SOURCE.replace("thermistor", "EEV"))
        self.assertIn(raw.contractor_questions[1], result.contractor_questions)
        self.assert_component_boundary(result)

    def test_new_installations_preserve_startup_questions(self):
        for text in ("Install a new single-zone mini-split.", "Install a new multi-zone mini-split."):
            with self.subTest(text=text):
                result = finalize(analysis_with([]), text)
                self.assertTrue(main.sizing_assessments(result))
                self.assertTrue(commissioning_items(result))
                self.assertTrue(any("startup" in q.lower() for q in result.contractor_questions))

    def test_full_replacement_and_mixed_work_keep_system_review(self):
        for text in ("Replace the complete mini-split system.",
                     "Replace indoor-unit thermistor and install a new mini-split."):
            with self.subTest(text=text):
                result = finalize(analysis_with([]), text)
                self.assertTrue(main.sizing_assessments(result))
                self.assertTrue(commissioning_items(result))
                self.assertTrue(any("startup" in q.lower() for q in result.contractor_questions))

    def test_question_order_deduplication_and_six_question_limit(self):
        raw = saved_response()
        raw.contractor_questions += [raw.contractor_questions[0], raw.contractor_questions[3],
            "Which part specification confirms compatibility?",
            "What warranty coverage applies?", "What repair scope is included?",
            "Why was the fault reported?", "What exact indoor model is proposed?"]
        questions = main.build_contractor_questions(raw, 1, SOURCE)
        self.assertEqual(questions[0], raw.contractor_questions[0])
        self.assertEqual(questions[-1], raw.contractor_questions[3])
        self.assertLessEqual(len(questions), 6)
        self.assertEqual(len(questions), len(set(questions)))
        self.assertNotIn(raw.contractor_questions[1], questions)

    def test_repeated_finalization_json_roundtrip_and_html_parity(self):
        raw = saved_response()
        raw.contractor_questions += raw.contractor_questions[:]
        result = finalize(raw)
        self.assertEqual(len(result.contractor_questions), 2)
        for _ in range(3):
            again = finalize(main.HVACAnalysis.model_validate_json(result.model_dump_json()))
            self.assertEqual(again.model_dump(), result.model_dump())
            self.assertEqual(main.build_report_html(again, 1), main.build_report_html(result, 1))
            result = again


if __name__ == "__main__":
    unittest.main()
