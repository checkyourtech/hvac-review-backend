from phase_regression_helpers import finalize_prior_module_analysis as finalize_customer_analysis
"""Offline presentation regressions for Phase 2B document contradictions."""
import unittest
from pathlib import Path

from main import TechnicalEvidenceAssessment, build_report_html
from test_structured_verdict import make_analysis


class EquipmentMatchingPresentationTests(unittest.TestCase):
    def bad_analysis(self, **overrides):
        values = dict(
            pricing_transparency="LIMITED",
            equipment_analysis="The quoted models are physically incompatible.",
            missing_information="More evidence is needed because the models cannot operate together.",
            installation_concerns="Operational viability is poor because the equipment combination is invalid.",
            red_flags=[
                "The AHRI certificate lists different models.",
                "The equipment combination is incompatible.",
                "The submitted match is invalid.",
            ],
            contractor_questions=[
                "What evidence supports compatibility?",
                "Can you provide another source?",
                "Can you verify these models work together?",
                "Which exact indoor and outdoor models will actually be installed?",
                "Can you provide an AHRI certificate for these models?",
            ],
            technical_assessments=[TechnicalEvidenceAssessment(
                subject="Quoted indoor/outdoor equipment compatibility",
                materiality="PRIMARY",
                diagnostic_evidence_status="CONTRADICTORY",
                scope_support="UNSUPPORTED",
                documented_evidence=["The quote lists NHP36-B and NAH36-B."],
                material_gaps=[],
                contradictions=["The submitted AHRI certificate lists different models: NHP36-A and NAH36-A."],
            )],
        )
        values.update(overrides)
        return make_analysis(**values)

    def test_bad_paperwork_is_one_conflict_with_specific_customer_language(self):
        analysis = self.bad_analysis()
        assessments = [item.model_dump() for item in analysis.technical_assessments]
        final = finalize_customer_analysis(
            analysis, quote_text=Path("equipment_matching_bad_test.txt").read_text(), quote_count=1,
        )
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual([item.model_dump() for item in final.technical_assessments], assessments)
        report = build_report_html(final, quote_count=1)
        self.assertIn("GET A SECOND OPINION", report)
        self.assertIn("The match paperwork", report)
        self.assertEqual(len(final.red_flags), 1)
        self.assertIn("does not match", final.red_flags[0])
        self.assertEqual(final.contractor_questions[:2], [
            "Which exact indoor and outdoor models will actually be installed?",
            "Can you provide the manufacturer or AHRI match documentation for those exact models?",
        ])
        self.assertEqual(len(final.contractor_questions), 3)
        self.assertIn("$16,400", final.contractor_questions[-1])
        self.assertIn("does not verify the quoted combination", final.equipment_analysis)
        self.assertIn("those exact models", final.missing_information)
        self.assertIn("before installation", final.installation_concerns)
        self.assertIn("match paperwork", final.bottom_line)
        text = " ".join([final.equipment_analysis, final.missing_information,
                         final.installation_concerns, final.bottom_line,
                         final.homeowner_takeaway, final.recommendation, *final.red_flags]).lower()
        for phrase in ("physically incompatible", "cannot operate", "invalid", "operational viability", "main diagnosis or repair"):
            self.assertNotIn(phrase, text)
        self.assertEqual(analysis.equipment_analysis, "The quoted models are physically incompatible.")

    def test_independent_flag_and_question_survive(self):
        analysis = self.bad_analysis()
        analysis.red_flags.append("The required disconnect is explicitly excluded.")
        analysis.contractor_questions.append("Will the required disconnect be included?")
        final = finalize_customer_analysis(analysis)
        self.assertEqual(len(final.red_flags), 2)
        self.assertIn("Will the required disconnect be included?", final.contractor_questions)

    def test_explicit_physical_conflict_is_not_rewritten_as_paperwork_only(self):
        analysis = self.bad_analysis()
        analysis.technical_assessments[0].contradictions.append(
            "The required components have incompatible voltage requirements."
        )
        final = finalize_customer_analysis(analysis)
        self.assertEqual(final.equipment_analysis, analysis.equipment_analysis)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_adequate_pricing_does_not_add_pricing_question(self):
        final = finalize_customer_analysis(self.bad_analysis(pricing_transparency="ADEQUATE"))
        self.assertEqual(len(final.contractor_questions), 2)

    def test_global_startup_positive_has_no_functionality_guarantee(self):
        for phrase in (
            "ensuring the new system's functionality",
            "ensures functionality",
            "guarantees correct operation",
            "ensures proper performance",
            "guarantees successful installation",
        ):
            with self.subTest(phrase=phrase):
                analysis = make_analysis(
                    good_signs=[f"The proposal includes startup and verification, {phrase}."],
                    installation_concerns=f"Startup verification {phrase}.",
                )
                final = finalize_customer_analysis(analysis)
                self.assertIn("The proposal includes startup verification.", final.good_signs)
                self.assertNotIn(phrase, " ".join(final.good_signs) + final.installation_concerns)
                self.assertEqual(final.decision.technical_support, "SUPPORTED")
                self.assertEqual(final.decision.verdict, "PROCEED")


if __name__ == "__main__":
    unittest.main()
