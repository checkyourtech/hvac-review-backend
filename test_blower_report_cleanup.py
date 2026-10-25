"""Source-backed report cleanup using the live blower fixture; no live model calls."""
import contextlib
import io
import unittest
from unittest.mock import patch

import main
import electrical_controls as ec
from commissioning import commissioning_items
from test_blower_support_normalization import BAD, GOOD, POSITIVES, classified, finalize, raw
from test_repair_diagnostics import upload
from test_technical_support_derivation import assessment


PRICING_FLAG = "Limited pricing transparency as the proposal does not break down parts and labor costs."
SCOPE_POSITIVE = ("The scope of work includes reassembling the blower section and testing system operation "
                  "after the repair, which are important for ensuring proper functionality.")
POST_REPAIR_QUESTION = ("What procedures will be taken to test the system after the replacement "
                        "to ensure it operates correctly?")


def live_fields():
    incoming = raw()
    incoming.red_flags = [PRICING_FLAG,
        "The quote recommends blower motor work without findings that establish the claimed failure."]
    incoming.good_signs = [SCOPE_POSITIVE]
    incoming.contractor_questions = [POST_REPAIR_QUESTION]
    return incoming


class BlowerReportCleanupTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_live_report_fields_finalize_to_one_flag_two_positives_two_questions(self):
        incoming = live_fields()
        before = incoming.model_dump()
        result, report = upload(incoming, BAD, classified())
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(len(result.red_flags), 1)
        self.assertIn("without findings", result.red_flags[0])
        self.assertEqual(len(result.good_signs), 2)
        self.assertIn(SCOPE_POSITIVE, result.good_signs)
        self.assertTrue(any("1-year parts and labor warranty" in sign for sign in result.good_signs))
        self.assertEqual(len(result.contractor_questions), 2)
        self.assertEqual(main.repair_diagnostic_question_owner(result.contractor_questions[0]), "motor_blower")
        self.assertEqual(main.contractor_question_category(result.contractor_questions[1]), "pricing")
        self.assertIn("$1,650", result.contractor_questions[1])
        self.assertNotIn(PRICING_FLAG, report)
        self.assertNotIn(POST_REPAIR_QUESTION, report)
        self.assertEqual(ec.electrical_items(result), [])
        self.assertEqual(report, main.build_report_html(result, 1))
        self.assertEqual(incoming.model_dump(), before)

    def test_routine_pricing_phrases_stay_out_of_major_flags(self):
        for flag in (PRICING_FLAG, "Pricing transparency is limited.",
                     "Parts and labor are bundled without an itemized breakdown.",
                     "The diagnostic fee and parts costs lack an itemized breakdown.",
                     "The proposal does not break down the repair costs."):
            with self.subTest(flag=flag):
                incoming = live_fields()
                incoming.red_flags = [flag]
                result = finalize(incoming)
                self.assertNotIn(flag, result.red_flags)
                self.assertEqual(len(result.red_flags), 1)
                self.assertEqual(result.decision.pricing_transparency, "LIMITED")

    def test_independent_documented_pricing_concerns_survive(self):
        for flag in ("The itemized charges total $1,850, contradicting the quoted $1,650 total.",
                     "The itemized invoice charges twice for the same blower removal labor.",
                     "The price includes an unauthorized charge for work the customer declined."):
            with self.subTest(flag=flag):
                incoming = live_fields()
                incoming.red_flags.append(flag)
                result = finalize(incoming, BAD + "\n" + flag)
                self.assertIn(flag, result.red_flags)
                self.assertNotIn(PRICING_FLAG, result.red_flags)

    def test_mixed_pricing_flag_does_not_lose_independent_conflict(self):
        flag = "Pricing transparency is limited, and the itemized invoice bills the same labor twice."
        incoming = live_fields()
        incoming.red_flags = [flag]
        result = finalize(incoming, BAD + "\n" + flag)
        self.assertIn(flag, result.red_flags)

    def test_existing_warranty_and_scope_positives_are_not_duplicated(self):
        incoming = live_fields()
        incoming.good_signs = POSITIVES.copy()
        first = finalize(incoming)
        self.assertEqual(first.good_signs, POSITIVES)
        again = finalize(first)
        self.assertEqual(first.model_dump(), again.model_dump())

    def test_warranty_recovery_uses_submitted_duration_and_coverage(self):
        for warranty in ("90-day labor warranty", "2-year parts and labor warranty"):
            with self.subTest(warranty=warranty):
                source = BAD.replace("1-year parts and labor warranty", warranty)
                result = finalize(live_fields(), source)
                self.assertTrue(any(warranty in sign for sign in result.good_signs))
                self.assertFalse(any("1-year" in sign for sign in result.good_signs))
                self.assertIn(SCOPE_POSITIVE, result.good_signs)

    def test_absent_negated_or_optional_warranty_is_not_invented(self):
        for replacement in ("", "No 1-year parts and labor warranty.",
                            "Optional 1-year parts and labor warranty available for an additional charge."):
            with self.subTest(replacement=replacement):
                source = BAD.replace("1-year parts and labor warranty.", replacement)
                result = finalize(live_fields(), source)
                self.assertEqual(result.good_signs, [SCOPE_POSITIVE])

    def test_separate_parts_and_labor_terms_are_not_merged_or_invented(self):
        source = BAD.replace("1-year parts and labor warranty.", "5-year parts warranty.\n1-year labor warranty.")
        result = finalize(live_fields(), source)
        warranty = " ".join(sign for sign in result.good_signs if "warranty" in sign)
        self.assertIn("5-year parts warranty", warranty)
        self.assertIn("1-year labor warranty", warranty)
        self.assertNotIn("5-year parts and labor", warranty)

    def test_generic_post_repair_questions_are_removed_when_scope_includes_testing(self):
        for question in (POST_REPAIR_QUESTION, "How will blower operation be verified after the repair?",
                         "Will you test system operation after replacement?"):
            with self.subTest(question=question):
                incoming = live_fields()
                incoming.contractor_questions = [question]
                result = finalize(incoming)
                self.assertNotIn(question, result.contractor_questions)
                self.assertEqual(len(result.contractor_questions), 2)

    def test_missing_or_excluded_testing_scope_retains_material_question(self):
        for scope in ("", "System operation testing is not included.",
                      "- Test system operation\nSystem operation testing is not included."):
            with self.subTest(scope=scope):
                source = BAD.replace("- Test system operation", scope)
                result = finalize(live_fields(), source)
                self.assertIn(POST_REPAIR_QUESTION, result.contractor_questions)
                self.assertLessEqual(len(result.contractor_questions), 6)

    def test_specific_unresolved_verification_question_is_not_a_generic_duplicate(self):
        question = "How will the required blower airflow setting be verified after the repair?"
        incoming = live_fields()
        incoming.contractor_questions = [question]
        incoming.decision.required_actions = ["Document the required blower airflow setting after repair."]
        result = finalize(incoming, BAD + "\nThe required blower airflow setting is not specified.")
        self.assertIn(question, result.contractor_questions)

    def test_independent_missing_commissioning_plan_still_generates_question(self):
        incoming = live_fields()
        incoming.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            materiality="MATERIAL_SECONDARY", subject="Startup and commissioning plan",
            gaps=["The required startup checks and documentation are not described."]))
        result = finalize(incoming, BAD + "\nRequired startup checks and documentation are not described.")
        self.assertTrue(commissioning_items(result))
        self.assertTrue(any(main.contractor_question_category(q) == "commissioning" for q in result.contractor_questions))
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertLessEqual(len(result.contractor_questions), 6)

    def test_capacitor_firewall_survives_report_cleanup(self):
        for reference in ("Capacitor was not tested.", "Capacitor tested normal."):
            with self.subTest(reference=reference):
                result = finalize(live_fields(), BAD + "\n" + reference)
                self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
                self.assertEqual(ec.electrical_items(result), [])
                self.assertNotIn("capacitor", " ".join(result.red_flags + result.contractor_questions).lower())

    def test_partial_and_supported_blower_boundaries_are_unchanged(self):
        findings = [s.strip(" -\t") for s in GOOD.split("Diagnostic findings:")[1].split("Recommended repair:")[0].splitlines() if s.strip()]
        for status, scope, evidence, expected in (
                ("INCOMPLETE", "PARTIALLY_DEFINED", findings[:1], "PARTIALLY_SUPPORTED"),
                ("CONFIRMED", "APPROPRIATE", findings, "SUPPORTED")):
            with self.subTest(status=status):
                result = finalize(raw(evidence, status, scope), GOOD)
                self.assertEqual(result.decision.technical_support, expected)
                self.assertEqual(result.technical_assessments[0].documented_evidence, evidence)

    def test_cleanup_is_presentation_only_and_idempotent(self):
        first = finalize(live_fields())
        with contextlib.redirect_stdout(io.StringIO()):
            again = main.finalize_customer_analysis(first, BAD, 1, classified(), fresh_analysis=True)
        self.assertEqual(first.model_dump(), again.model_dump())


if __name__ == "__main__":
    unittest.main()
