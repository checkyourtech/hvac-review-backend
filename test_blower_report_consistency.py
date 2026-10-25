"""Offline coverage for independent blower positives and section ownership."""
import unittest
from unittest.mock import patch

import main
from repair_diagnostics import normalize_blower_report_details
from test_blower_report_cleanup import live_fields, SCOPE_POSITIVE
from test_blower_support_normalization import BAD, POSITIVES, classified, finalize
from test_repair_diagnostics import upload


WARRANTY = POSITIVES[0]
SCOPE = POSITIVES[1]
DIAGNOSTIC_REPEAT = (
    "However, since the initial diagnosis lacks confirmation, the effectiveness "
    "of the proposed work remains uncertain until further evaluation is made "
    "regarding what evidence exists for the current failure."
)
SCOPE_DESCRIPTION = (
    "The scope of work includes removing the existing blower motor, installing "
    "a new ECM blower motor, reassembling the blower section, and testing system operation."
)


class BlowerReportConsistencyTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def normalize(self, signs, source=BAD):
        incoming = live_fields()
        incoming.good_signs = list(signs)
        normalize_blower_report_details(incoming, source)
        return incoming

    def assert_two_independent_positives(self, result):
        self.assertEqual(len(result.good_signs), 2)
        self.assertEqual(sum("warranty" in s.lower() for s in result.good_signs), 1)
        self.assertTrue(any("scope" in s.lower() for s in result.good_signs))

    def test_warranty_only_recovers_documented_scope(self):
        result = self.normalize([WARRANTY])
        self.assert_two_independent_positives(result)
        self.assertIn(WARRANTY, result.good_signs)
        scope = next(s for s in result.good_signs if s != WARRANTY)
        for fact in ("removal", "installation", "reassembly", "operation testing"):
            self.assertIn(fact, scope)
        self.assertIn("planned", scope)

    def test_scope_only_recovers_documented_warranty(self):
        result = self.normalize([SCOPE_POSITIVE])
        self.assert_two_independent_positives(result)
        self.assertIn(SCOPE_POSITIVE, result.good_signs)

    def test_both_omitted_are_recovered_from_source(self):
        self.assert_two_independent_positives(self.normalize([]))

    def test_both_existing_positives_keep_their_wording(self):
        self.assertEqual(self.normalize(POSITIVES).good_signs, POSITIVES)

    def test_semantically_equivalent_positives_are_deduplicated(self):
        for equivalent in ("The proposal includes a 1-year parts and labor warranty.",
                           "The quote includes a 1-year parts and labor warranty, "
                           "which is a positive sign for consumer protection."):
            with self.subTest(equivalent=equivalent):
                signs = [WARRANTY, equivalent, SCOPE, SCOPE_DESCRIPTION]
                result = self.normalize(signs)
                # Keep explicit documented steps rather than first-arriving
                # generic scope praise when both describe the same work.
                self.assertEqual(result.good_signs, [WARRANTY, SCOPE_DESCRIPTION])

    def test_equivalent_procedure_wording_does_not_add_a_scope_duplicate(self):
        for sign in (SCOPE_POSITIVE, SCOPE_DESCRIPTION,
                     "The repair includes blower reassembly and a planned system operation check."):
            with self.subTest(sign=sign):
                result = self.normalize([WARRANTY, sign])
                self.assertEqual(result.good_signs, [WARRANTY, sign])

    def test_distinct_warranty_coverage_is_preserved(self):
        source = BAD.replace("1-year parts and labor warranty.",
                             "5-year parts warranty.\n1-year labor warranty.")
        signs = ["The proposal includes a 5-year parts warranty.",
                 "Labor is covered by a 1-year warranty.", SCOPE]
        self.assertEqual(self.normalize(signs, source).good_signs, signs)

    def test_warranty_positive_with_an_independent_benefit_is_preserved(self):
        extra = "The 1-year parts and labor warranty also includes an annual inspection."
        source = BAD + "\nWarranty includes an annual inspection."
        signs = [WARRANTY, extra, SCOPE]
        self.assertEqual(self.normalize(signs, source).good_signs, signs)

    def test_independent_scope_detail_is_not_deduplicated_away(self):
        extra = "The repair scope includes checking blower airflow and verifying the required speed setting."
        source = BAD + "\nCheck blower airflow and verify the required speed setting."
        result = self.normalize([*POSITIVES, extra], source)
        self.assertEqual(result.good_signs, [*POSITIVES, extra])

    def test_scope_paraphrase_with_an_additional_source_fact_is_preserved(self):
        extra = SCOPE_DESCRIPTION[:-1] + ", with new mounting brackets."
        source = BAD + "\nNew mounting brackets included."
        signs = [WARRANTY, SCOPE_DESCRIPTION, extra]
        self.assertEqual(self.normalize(signs, source).good_signs, signs)

    def test_missing_source_facts_do_not_manufacture_positives(self):
        source = "Diagnosis: Blower motor is bad.\nRecommended repair: Replace ECM blower motor."
        self.assertEqual(self.normalize([], source).good_signs, [])

    def test_optional_or_excluded_scope_does_not_receive_recovery_credit(self):
        for scope in ("", "Optional: Reassemble blower section and test system operation.",
                      "Reassembly and system operation testing are not included.",
                      "- Reassemble blower section\n- Test system operation\n"
                      "System operation testing is not included.",
                      "- Reassemble blower section\n- Test system operation\n"
                      "Do not test system operation."):
            with self.subTest(scope=scope):
                source = BAD.replace("- Reassemble blower section\n- Test system operation", scope)
                result = self.normalize([], source)
                self.assertEqual(len(result.good_signs), 1)
                self.assertIn("warranty", result.good_signs[0])

    def test_optional_warranty_does_not_suppress_valid_scope_recovery(self):
        source = BAD.replace("1-year parts and labor warranty.",
                             "Optional 1-year parts and labor warranty.")
        result = self.normalize([], source)
        self.assertEqual(len(result.good_signs), 1)
        self.assertNotIn("warranty", result.good_signs[0])

    def test_recovery_is_idempotent_and_does_not_write_technical_evidence(self):
        incoming = live_fields()
        incoming.good_signs = []
        assessments = [a.model_dump() for a in incoming.technical_assessments]
        decision = incoming.decision.model_dump()
        normalize_blower_report_details(incoming, BAD)
        first = incoming.model_dump()
        normalize_blower_report_details(incoming, BAD)
        self.assertEqual(incoming.model_dump(), first)
        self.assertEqual([a.model_dump() for a in incoming.technical_assessments], assessments)
        self.assertEqual(incoming.decision.model_dump(), decision)

    def test_repeated_diagnostic_sentence_is_removed_after_factual_scope(self):
        incoming = live_fields()
        incoming.installation_concerns = SCOPE_DESCRIPTION + " " + DIAGNOSTIC_REPEAT
        result = finalize(incoming)
        self.assertEqual(result.installation_concerns, SCOPE_DESCRIPTION)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(len(result.red_flags), 1)
        self.assertEqual(len(result.contractor_questions), 2)

    def test_diagnostic_only_installation_text_gets_factual_scope(self):
        for text in (DIAGNOSTIC_REPEAT, "The blower motor diagnosis is not confirmed.",
                     "The quote does not document findings that establish blower motor failure."):
            with self.subTest(text=text):
                incoming = live_fields()
                incoming.installation_concerns = text
                result = finalize(incoming)
                self.assertTrue(result.installation_concerns.strip())
                self.assertNotIn(text, result.installation_concerns)
                self.assertIn("planned", result.installation_concerns)
                self.assertIn("operation testing", result.installation_concerns)

    def test_genuine_procedural_omissions_survive_even_with_diagnostic_uncertainty(self):
        for warning in (
            "The required blower speed setting is not specified.",
            "The diagnosis is uncertain, and the scope omits checking blower rotation.",
            "The diagnosis is unconfirmed, but the installation also omits a required safety check.",
            "The diagnosis is unsupported because the mounting bracket is missing.",
            "The diagnosis is uncertain and the mounting bracket needs adjustment.",
            "System operation testing is not included.",
        ):
            with self.subTest(warning=warning):
                incoming = live_fields()
                incoming.installation_concerns = warning
                result = finalize(incoming, BAD + "\n" + warning)
                self.assertIn(warning, result.installation_concerns)

    def test_cleanup_does_not_claim_missing_procedures_were_included(self):
        source = "Diagnosis: Blower motor is bad.\nRecommended repair: Replace ECM blower motor."
        incoming = live_fields()
        incoming.installation_concerns = DIAGNOSTIC_REPEAT
        normalize_blower_report_details(incoming, source)
        self.assertTrue(incoming.installation_concerns.strip())
        self.assertNotIn(DIAGNOSTIC_REPEAT, incoming.installation_concerns)
        self.assertNotIn("testing", incoming.installation_concerns)
        self.assertNotIn("reassembly", incoming.installation_concerns)

    def test_real_upload_preserves_accepted_boundaries_and_hides_comparison(self):
        incoming = live_fields()
        incoming.good_signs = [WARRANTY]
        incoming.installation_concerns = SCOPE_DESCRIPTION + " " + DIAGNOSTIC_REPEAT
        incoming.quote_comparison = "No other quotes were provided."
        incoming.best_quote_recommendation = "No alternative quotes to compare."
        result, report = upload(incoming, BAD, classified())
        self.assert_two_independent_positives(result)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(len(result.red_flags), 1)
        self.assertEqual(len(result.contractor_questions), 2)
        self.assertNotIn("Quote Comparison", report)
        self.assertNotIn("Best Quote Recommendation", report)
        self.assertNotIn(DIAGNOSTIC_REPEAT, report)
        self.assertEqual(report, main.build_report_html(result, quote_count=1))
        self.assertEqual(result.model_dump(), finalize(result).model_dump())


if __name__ == "__main__":
    unittest.main()
