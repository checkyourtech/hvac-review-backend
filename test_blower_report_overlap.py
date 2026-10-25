"""Offline regressions for the fresh live report's overlapping scope content."""
import unittest
from unittest.mock import patch

import electrical_controls as ec
from repair_diagnostics import normalize_blower_report_details
from test_blower_report_cleanup import live_fields
from test_blower_support_normalization import BAD, classified, finalize
from test_repair_diagnostics import upload
from test_technical_support_derivation import assessment


WARRANTY = "The proposal includes a 1-year warranty on parts and labor."
GENERIC_SCOPE = "The scope of work is clear and involves testing the system after installation."
DETAILED_SCOPE = ("The quoted repair scope includes motor removal, motor installation, "
                  "blower reassembly and planned system operation testing.")
INSTALLATION_SCOPE = ("The scope of work includes necessary steps to replace the blower motor "
                      "and verify system operation afterward, which is good practice.")
REDUNDANT_VERIFICATION = ("However, it would be beneficial to ensure confirmation of proper "
                          "operation post-repair.")


class BlowerReportOverlapTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def normalize(self, signs, installation="", source=BAD):
        incoming = live_fields()
        incoming.good_signs = list(signs)
        incoming.installation_concerns = installation
        normalize_blower_report_details(incoming, source)
        return incoming

    def test_exact_live_duplicates_keep_the_detailed_grounded_scope(self):
        for scopes in ([GENERIC_SCOPE, DETAILED_SCOPE], [DETAILED_SCOPE, GENERIC_SCOPE]):
            with self.subTest(scopes=scopes):
                result = self.normalize([WARRANTY, *scopes])
                self.assertEqual(result.good_signs, [WARRANTY, DETAILED_SCOPE])

    def test_generic_scope_alone_is_consolidated_with_source_recovery(self):
        for generic in (GENERIC_SCOPE, "The scope is clear and includes testing after installation."):
            with self.subTest(generic=generic):
                result = self.normalize([WARRANTY, generic])
                self.assertEqual(result.good_signs, [WARRANTY, DETAILED_SCOPE])

    def test_overlapping_specific_steps_choose_the_more_informative_description(self):
        partial = "The repair includes blower reassembly and planned system operation testing."
        for scopes in ([partial, DETAILED_SCOPE], [DETAILED_SCOPE, partial]):
            with self.subTest(scopes=scopes):
                self.assertEqual(self.normalize([WARRANTY, *scopes]).good_signs,
                                 [WARRANTY, DETAILED_SCOPE])

    def test_independent_facts_survive_scope_consolidation(self):
        extras = ["The quote includes a permit.",
                  "The repair includes checking blower airflow and verifying the required speed setting.",
                  "New mounting brackets are included."]
        source = BAD + "\n" + "\n".join(extras)
        result = self.normalize([WARRANTY, GENERIC_SCOPE, DETAILED_SCOPE, *extras], source=source)
        self.assertEqual(result.good_signs, [WARRANTY, DETAILED_SCOPE, *extras])

    def test_warranty_scope_and_empty_inputs_recover_independent_facts(self):
        for signs in ([WARRANTY], [DETAILED_SCOPE], []):
            with self.subTest(signs=signs):
                result = self.normalize(signs)
                self.assertEqual(len(result.good_signs), 2)
                self.assertIn(DETAILED_SCOPE, result.good_signs)
                self.assertEqual(sum("warranty" in sign for sign in result.good_signs), 1)

    def test_repeated_normalization_is_stable(self):
        first = self.normalize([WARRANTY, GENERIC_SCOPE, DETAILED_SCOPE],
                               INSTALLATION_SCOPE + " " + REDUNDANT_VERIFICATION)
        before = first.model_dump()
        for _ in range(3):
            normalize_blower_report_details(first, BAD)
            self.assertEqual(first.model_dump(), before)

    def test_exact_live_installation_text_acknowledges_the_documented_procedure(self):
        result = self.normalize([WARRANTY], INSTALLATION_SCOPE + " " + REDUNDANT_VERIFICATION)
        self.assertEqual(result.installation_concerns, DETAILED_SCOPE)

    def test_generic_followups_do_not_imply_missing_documented_testing(self):
        for followup in (REDUNDANT_VERIFICATION,
                         "Ensure proper system operation after replacement.",
                         "Confirm that the blower operates properly after the repair.",
                         "Post-repair operation testing is not described."):
            with self.subTest(followup=followup):
                result = self.normalize([WARRANTY], INSTALLATION_SCOPE + " " + followup)
                self.assertEqual(result.installation_concerns, DETAILED_SCOPE)

    def test_truly_missing_or_excluded_testing_preserves_the_followup(self):
        for replacement in ("", "System operation testing is not included.",
                            "- Test system operation\nDo not test system operation."):
            with self.subTest(replacement=replacement):
                source = BAD.replace("- Test system operation", replacement)
                result = self.normalize([], REDUNDANT_VERIFICATION, source)
                self.assertEqual(result.installation_concerns, REDUNDANT_VERIFICATION)
                self.assertNotIn(DETAILED_SCOPE, result.good_signs)

    def test_specific_and_mixed_installation_warnings_are_preserved(self):
        for concern in (
            "The required blower airflow setting is not specified.",
            "Confirm proper operation after repair and check the mounting bracket clearance.",
            "Confirm safe rotation after the repair.",
            "Post-repair operation must be verified with the required airflow readings.",
        ):
            with self.subTest(concern=concern):
                result = self.normalize([], concern + " " + REDUNDANT_VERIFICATION,
                                        BAD + "\n" + concern)
                self.assertEqual(result.installation_concerns, concern)

    def test_separate_material_startup_gap_preserves_verification_concern(self):
        incoming = live_fields()
        incoming.installation_concerns = REDUNDANT_VERIFICATION
        incoming.technical_assessments.append(assessment(
            "INCOMPLETE", "PARTIALLY_DEFINED", materiality="MATERIAL_SECONDARY",
            subject="Startup and commissioning plan",
            gaps=["The required startup checks and documentation are not described."]))
        normalize_blower_report_details(incoming, BAD)
        self.assertEqual(incoming.installation_concerns, REDUNDANT_VERIFICATION)

    def test_complementary_routine_scope_facts_are_retained_in_one_explanation(self):
        signs = [WARRANTY, "The work includes removing the existing blower motor.",
                 "The repair includes blower reassembly and planned system operation testing."]
        result = self.normalize(signs)
        self.assertEqual(result.good_signs, [WARRANTY, DETAILED_SCOPE])

    def test_unknown_scope_details_are_not_merged_with_routine_credit(self):
        independent = DETAILED_SCOPE[:-1] + ", with new mounting brackets."
        source = BAD + "\nNew mounting brackets included."
        result = self.normalize([WARRANTY, GENERIC_SCOPE, DETAILED_SCOPE, independent], source=source)
        self.assertEqual(result.good_signs, [WARRANTY, DETAILED_SCOPE, independent])

    def test_upload_and_repeat_finalization_preserve_all_accepted_boundaries(self):
        incoming = live_fields()
        incoming.good_signs = [WARRANTY, GENERIC_SCOPE, DETAILED_SCOPE]
        incoming.installation_concerns = INSTALLATION_SCOPE + " " + REDUNDANT_VERIFICATION
        incoming.quote_comparison = "No other quotes were provided."
        incoming.best_quote_recommendation = "No alternatives are available."
        result, report = upload(incoming, BAD, classified())
        self.assertEqual(result.good_signs, [WARRANTY, DETAILED_SCOPE])
        self.assertEqual(result.installation_concerns, DETAILED_SCOPE)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(len(result.red_flags), 1)
        self.assertEqual(len(result.contractor_questions), 2)
        self.assertEqual(ec.electrical_items(result), [])
        self.assertNotIn("Quote Comparison", report)
        self.assertNotIn("Best Quote Recommendation", report)
        self.assertNotIn(GENERIC_SCOPE, report)
        self.assertNotIn(REDUNDANT_VERIFICATION, report)
        self.assertEqual(finalize(result).model_dump(), result.model_dump())


if __name__ == "__main__":
    unittest.main()
