import html
import unittest

from main import (
    HVACAnalysis,
    HVACDecision,
    PRICING_REQUIRED_ACTION,
    apply_decision_policy,
    build_report_html,
)


def make_analysis(
    *,
    technical_support="SUPPORTED",
    pricing_transparency="ADEQUATE",
    required_actions=None,
    optional_suggestions=None,
    verdict_reasons=None,
    red_flags=None,
    recommendation="AI-generated recommendation that Python must replace.",
):
    return HVACAnalysis(
        project_overview="Test proposal",
        equipment_analysis="The documented technical scope was reviewed.",
        missing_information="No material information is missing.",
        pricing_review="Pricing transparency was reviewed separately.",
        installation_concerns="No material installation concerns were identified.",
        quote_comparison="",
        best_quote_recommendation="",
        contractor_vetting="",
        red_flags=red_flags or [],
        good_signs=["The proposal documents the proposed scope."],
        recommendation=recommendation,
        decision=HVACDecision(
            verdict="GET_A_SECOND_OPINION",
            technical_support=technical_support,
            pricing_transparency=pricing_transparency,
            required_actions=required_actions or [],
            optional_suggestions=optional_suggestions or [],
            verdict_reasons=verdict_reasons or [],
        ),
    )


class StructuredVerdictPolicyTests(unittest.TestCase):
    def test_supported_and_itemized_proceeds(self):
        analysis = apply_decision_policy(make_analysis())
        self.assertEqual(analysis.decision.verdict, "PROCEED")
        self.assertTrue(analysis.recommendation.startswith("PROCEED —"))

    def test_supported_without_itemization_requires_review(self):
        analysis = apply_decision_policy(
            make_analysis(pricing_transparency="ABSENT")
        )
        self.assertEqual(
            analysis.decision.verdict,
            "REVIEW_BEFORE_APPROVING",
        )
        self.assertIn("technical proposal appears supported", analysis.recommendation)
        self.assertIn("itemization is absent", analysis.recommendation)
        self.assertIn(PRICING_REQUIRED_ACTION, analysis.decision.required_actions)
        self.assertEqual(analysis.red_flags, [])

    def test_limited_pricing_adds_required_itemization_action(self):
        analysis = apply_decision_policy(
            make_analysis(pricing_transparency="LIMITED")
        )
        self.assertIn(PRICING_REQUIRED_ACTION, analysis.decision.required_actions)
        self.assertIn("pricing transparency is limited", analysis.recommendation)
        self.assertIn("does not imply dishonesty", analysis.recommendation)

    def test_existing_equivalent_itemization_action_is_not_duplicated(self):
        existing_action = "Obtain a detailed cost breakdown before signing."
        analysis = apply_decision_policy(
            make_analysis(
                pricing_transparency="ABSENT",
                required_actions=[existing_action],
            )
        )
        self.assertEqual(analysis.decision.required_actions, [existing_action])

    def test_optional_suggestion_alone_does_not_downgrade(self):
        analysis = apply_decision_policy(
            make_analysis(optional_suggestions=["Consider a seasonal maintenance plan."])
        )
        self.assertEqual(analysis.decision.verdict, "PROCEED")

    def test_required_action_requires_review(self):
        analysis = apply_decision_policy(
            make_analysis(required_actions=["Confirm the included permit scope."])
        )
        self.assertEqual(
            analysis.decision.verdict,
            "REVIEW_BEFORE_APPROVING",
        )

    def test_partially_supported_requires_review(self):
        analysis = apply_decision_policy(
            make_analysis(technical_support="PARTIALLY_SUPPORTED")
        )
        self.assertEqual(
            analysis.decision.verdict,
            "REVIEW_BEFORE_APPROVING",
        )

    def test_unsupported_diagnosis_requires_second_opinion(self):
        analysis = apply_decision_policy(
            make_analysis(technical_support="UNSUPPORTED")
        )
        self.assertEqual(
            analysis.decision.verdict,
            "GET_A_SECOND_OPINION",
        )
        self.assertTrue(
            analysis.recommendation.startswith("GET A SECOND OPINION —")
        )

    def test_no_clarification_needed_phrase_does_not_cause_review(self):
        analysis = apply_decision_policy(
            make_analysis(recommendation="No clarification needed.")
        )
        self.assertEqual(analysis.decision.verdict, "PROCEED")

    def test_pricing_is_itemized_phrase_does_not_cause_review(self):
        analysis = apply_decision_policy(
            make_analysis(recommendation="Pricing is itemized.")
        )
        self.assertEqual(analysis.decision.verdict, "PROCEED")

    def test_red_flag_count_alone_does_not_set_verdict(self):
        analysis = apply_decision_policy(
            make_analysis(red_flags=["A nonmaterial item was noted."])
        )
        self.assertEqual(analysis.decision.verdict, "PROCEED")

    def test_report_customer_messages_use_one_canonical_summary(self):
        analysis = make_analysis(
            pricing_transparency="LIMITED",
            required_actions=["Request the major cost breakdown."],
            verdict_reasons=["Major cost components remain bundled."],
        )

        report = build_report_html(analysis)
        canonical = html.escape(analysis.recommendation)

        self.assertEqual(
            analysis.decision.verdict,
            "REVIEW_BEFORE_APPROVING",
        )
        self.assertEqual(report.count(canonical), 3)
        self.assertIn("REVIEW BEFORE APPROVING", report)
        self.assertNotIn("AI-generated recommendation", report)

    def test_single_quote_suppresses_comparison_sections_and_placeholders(self):
        analysis = make_analysis()
        analysis.quote_comparison = "Only one quote is present."
        analysis.best_quote_recommendation = "This is the only quote available."

        report = build_report_html(analysis, quote_count=1)

        self.assertNotIn("<h2>Quote Comparison</h2>", report)
        self.assertNotIn("<h2>Best Quote Recommendation</h2>", report)
        self.assertNotIn("Only one quote is present", report)
        self.assertNotIn("This is the only quote available", report)

    def test_multi_quote_preserves_comparison_sections(self):
        analysis = make_analysis()
        analysis.quote_comparison = "Quote A and Quote B were compared."
        analysis.best_quote_recommendation = "Quote B is the stronger proposal."

        report = build_report_html(analysis, quote_count=2)

        self.assertIn("<h2>Quote Comparison</h2>", report)
        self.assertIn("Quote A and Quote B were compared.", report)
        self.assertIn("<h2>Best Quote Recommendation</h2>", report)
        self.assertIn("Quote B is the stronger proposal.", report)


if __name__ == "__main__":
    unittest.main()
