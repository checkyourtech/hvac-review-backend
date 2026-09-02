import html
import re
import unittest
from pathlib import Path

from main import (
    CONTRACTOR_QUESTION_RULES,
    HVACAnalysis,
    HVACDecision,
    PRICING_REQUIRED_ACTION,
    TechnicalEvidenceAssessment,
    apply_decision_policy,
    build_report_html as render_finalized_html,
    finalize_customer_analysis,
)


def make_analysis(
    *,
    technical_support="SUPPORTED",
    pricing_transparency="ADEQUATE",
    required_actions=None,
    optional_suggestions=None,
    verdict_reasons=None,
    red_flags=None,
    good_signs=None,
    contractor_questions=None,
    recommendation="AI-generated recommendation that Python must replace.",
    pricing_review="Pricing transparency was reviewed separately.",
    missing_information="No material information is missing.",
    installation_concerns="No material installation concerns were identified.",
    technical_assessments=None,
):
    return HVACAnalysis(
        project_overview="Test proposal",
        equipment_analysis="The documented technical scope was reviewed.",
        missing_information=missing_information,
        pricing_review=pricing_review,
        installation_concerns=installation_concerns,
        quote_comparison="",
        best_quote_recommendation="",
        contractor_vetting="",
        red_flags=red_flags or [],
        good_signs=good_signs or ["The proposal documents the proposed scope."],
        contractor_questions=contractor_questions or [],
        recommendation=recommendation,
        decision=HVACDecision(
            verdict="GET_A_SECOND_OPINION",
            technical_support=technical_support,
            pricing_transparency=pricing_transparency,
            required_actions=required_actions or [],
            optional_suggestions=optional_suggestions or [],
            verdict_reasons=verdict_reasons or [],
        ),
        technical_assessments=technical_assessments or [],
    )


def supported_assessment(*, materiality="PRIMARY", gaps=None):
    return TechnicalEvidenceAssessment(
        subject="Supported proposed work",
        materiality=materiality,
        diagnostic_evidence_status="CONFIRMED",
        scope_support="APPROPRIATE",
        documented_evidence=["Documented testing supports the proposed work."],
        material_gaps=gaps or [],
        contradictions=[],
    )


def report_paragraph(report, heading):
    match = re.search(
        rf"<h2>{re.escape(html.escape(heading))}</h2>\s*<p>(.*?)</p>",
        report,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"Report section not found: {heading}")
    return html.unescape(match.group(1).strip())


def build_report_html(analysis, quote_count=None, quote_text=""):
    finalized = finalize_customer_analysis(
        analysis,
        quote_text=quote_text,
        quote_count=quote_count,
    )
    return render_finalized_html(finalized, quote_count=quote_count)


def banner_paragraph(report):
    match = re.search(
        r'<div class="verdict-main">.*?</div>\s*<p>(.*?)</p>',
        report,
        re.DOTALL,
    )
    if not match:
        raise AssertionError("Recommendation banner paragraph not found")
    return html.unescape(match.group(1).strip())


def report_list_items(report, heading):
    match = re.search(
        rf"<h2>{re.escape(html.escape(heading))}</h2>\s*<ul>(.*?)</ul>",
        report,
        re.DOTALL,
    )
    if not match:
        return []
    return [
        html.unescape(item.strip())
        for item in re.findall(r"<li>(.*?)</li>", match.group(1), re.DOTALL)
    ]


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
        self.assertNotIn(
            "Questions to Ask Your Contractor",
            build_report_html(analysis),
        )

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

    def test_limited_pricing_report_uses_concise_canonical_hierarchy(self):
        detailed_pricing = (
            "The quoted total is $725. Parts and labor are bundled, so individual "
            "component and labor charges cannot be evaluated. Request an itemized "
            "breakdown before approval."
        )
        analysis = make_analysis(
            pricing_transparency="LIMITED",
            verdict_reasons=["Major cost components remain bundled."],
            pricing_review=detailed_pricing,
            good_signs=[
                "Measured capacitance and visible contact damage support the repair."
            ],
        )
        analysis = finalize_customer_analysis(analysis)

        report = build_report_html(analysis)
        banner = banner_paragraph(report)
        takeaway = report_paragraph(report, "What This Means for You")
        pricing = report_paragraph(report, "Price & Value Review")
        bottom_line = report_paragraph(report, "Bottom Line")

        self.assertEqual(
            analysis.decision.verdict,
            "REVIEW_BEFORE_APPROVING",
        )
        self.assertIn(PRICING_REQUIRED_ACTION, analysis.decision.required_actions)
        self.assertIn("REVIEW BEFORE APPROVING", report)
        self.assertLessEqual(len(banner), 140)
        self.assertIn("technically supported", banner)
        self.assertIn("quoted price", banner)
        self.assertNotIn("Major cost components remain bundled", banner)
        self.assertNotIn(PRICING_REQUIRED_ACTION, banner)

        self.assertIn("technically supported", takeaway)
        self.assertIn("Measured capacitance and visible contact damage", takeaway)
        self.assertIn("No major technical red flags", takeaway)
        self.assertIn("clearer breakdown", takeaway)
        self.assertNotIn("individual component and labor charges", takeaway)

        self.assertEqual(pricing, detailed_pricing)
        self.assertIn("individual component and labor charges", pricing)

        self.assertLessEqual(len(bottom_line), 180)
        self.assertIn("technically supported", bottom_line)
        self.assertIn("requested price breakdown", bottom_line)
        self.assertNotIn("Major cost components remain bundled", bottom_line)
        self.assertNotIn(PRICING_REQUIRED_ACTION, bottom_line)
        self.assertNotIn(analysis.recommendation, report)
        self.assertNotIn("AI-generated recommendation", report)

    def test_unsupported_report_keeps_technical_issue_primary(self):
        analysis = make_analysis(
            technical_support="UNSUPPORTED",
            pricing_transparency="LIMITED",
            verdict_reasons=[
                "The compressor diagnosis is not supported by documented testing.",
                "Major cost components remain bundled.",
            ],
            pricing_review=(
                "The $4,800 total is bundled. Request separate parts, labor, and "
                "materials pricing."
            ),
        )
        analysis = finalize_customer_analysis(analysis)

        report = build_report_html(analysis)
        banner = banner_paragraph(report)
        takeaway = report_paragraph(report, "What This Means for You")
        bottom_line = report_paragraph(report, "Bottom Line")

        self.assertEqual(analysis.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertIn("GET A SECOND OPINION", report)
        self.assertIn("does not adequately support", banner)
        self.assertNotIn("quoted price", banner)
        self.assertIn("second professional opinion", takeaway)
        self.assertIn("technical concern is primary", takeaway)
        self.assertIn("not adequately supported", bottom_line)
        self.assertIn("second professional opinion", bottom_line)
        self.assertNotIn("price breakdown", bottom_line)

    def test_proceed_report_remains_concise_and_supported(self):
        analysis = finalize_customer_analysis(make_analysis())

        report = build_report_html(analysis)
        banner = banner_paragraph(report)
        takeaway = report_paragraph(report, "What This Means for You")
        bottom_line = report_paragraph(report, "Bottom Line")

        self.assertEqual(analysis.decision.verdict, "PROCEED")
        self.assertIn("PROCEED", report)
        self.assertIn("supported", banner)
        self.assertIn("technically supported", takeaway)
        self.assertIn("technically supported", bottom_line)

    def test_supported_capacitor_with_limited_pricing_has_only_pricing_question(self):
        analysis = make_analysis(pricing_transparency="LIMITED")

        report = build_report_html(analysis, quote_count=1)
        questions = report_list_items(report, "Questions to Ask Your Contractor")

        self.assertEqual(len(questions), 1)
        self.assertIn("itemized breakdown", questions[0])
        self.assertIn("parts/equipment", questions[0])
        self.assertNotIn("measurement", questions[0].lower())
        self.assertNotIn("model number", questions[0].lower())

    def test_supported_electrical_fixture_suppresses_generic_questions(self):
        quote_text = Path("electrical_test.txt").read_text(encoding="utf-8")
        analysis = make_analysis(
            pricing_transparency="LIMITED",
            missing_information=(
                "No important missing information was identified that appears likely "
                "to change the recommendation."
            ),
            installation_concerns=(
                "No significant installation or repair-scope concerns were identified "
                "in the submitted proposal."
            ),
            contractor_questions=[
                "Can you confirm the replacement parts' specifications and compatibility?",
                "Is there any warranty on the capacitor and contactor?",
                "Can you provide separate parts and labor pricing?",
            ],
            technical_assessments=[supported_assessment()],
        )

        finalized = finalize_customer_analysis(analysis, quote_text=quote_text)

        self.assertEqual(
            finalized.contractor_questions,
            [
                "Can you provide an itemized breakdown of the parts, labor, and "
                "other charges included in the $725 total?"
            ],
        )

    def test_supported_adequate_suppresses_generic_ai_questions(self):
        analysis = make_analysis(
            contractor_questions=[
                "What are the exact replacement part model numbers?",
                "What warranty covers the routine repair?",
            ],
            technical_assessments=[supported_assessment()],
        )

        finalized = finalize_customer_analysis(analysis)

        self.assertEqual(finalized.contractor_questions, [])

    def test_optional_suggestion_does_not_create_supported_question(self):
        analysis = make_analysis(
            optional_suggestions=["Consider asking for the exact replacement model."],
            contractor_questions=["What exact replacement model will be installed?"],
        )

        finalized = finalize_customer_analysis(analysis)

        self.assertEqual(finalized.contractor_questions, [])

    def test_material_compatibility_action_retains_compatibility_question(self):
        question = "Is the proposed replacement component compatible with the existing system?"
        analysis = make_analysis(
            required_actions=[
                "Confirm compatibility because the quoted component rating conflicts "
                "with the existing equipment rating."
            ],
            contractor_questions=[question],
        )

        finalized = finalize_customer_analysis(analysis)

        self.assertEqual(finalized.contractor_questions, [question])

    def test_material_warranty_action_retains_warranty_question(self):
        question = "What warranty coverage applies to this major repair?"
        analysis = make_analysis(
            required_actions=[
                "Clarify warranty coverage because it is material to the risk of this major repair."
            ],
            contractor_questions=[question],
        )

        finalized = finalize_customer_analysis(analysis)

        self.assertEqual(finalized.contractor_questions, [question])

    def test_supported_structured_secondary_gap_retains_applicable_question(self):
        question = "Is the secondary component compatible with the existing equipment?"
        analysis = make_analysis(
            technical_assessments=[
                supported_assessment(),
                supported_assessment(
                    materiality="MATERIAL_SECONDARY",
                    gaps=[
                        "Compatibility of the secondary component with the existing "
                        "equipment remains a material clarification."
                    ],
                ),
            ],
            contractor_questions=[
                "Will the wiring inspection look for any other useful information?",
                question,
            ],
        )

        finalized = finalize_customer_analysis(analysis)

        self.assertEqual(finalized.decision.technical_support, "SUPPORTED")
        self.assertEqual(finalized.contractor_questions, [question])

    def test_partial_low_refrigerant_questions_are_technical_first(self):
        technical_questions = [
            "What measurements showed that the system is low on refrigerant?",
            "Was the cause of the low charge evaluated, and was leak investigation performed or recommended?",
            "How will you verify the final refrigerant charge and system operation?",
        ]
        analysis = make_analysis(
            technical_support="PARTIALLY_SUPPORTED",
            pricing_transparency="LIMITED",
            pricing_review="The bundled recharge total is $950.",
            contractor_questions=technical_questions
            + [
                "Can you provide separate pricing for the refrigerant being added and the labor involved?",
                "Can you provide a breakdown of costs for refrigerant and labor?",
            ],
        )

        report = build_report_html(analysis)
        questions = report_list_items(report, "Questions to Ask Your Contractor")

        self.assertEqual(questions[:3], technical_questions)
        self.assertIn("measurements", questions[0])
        self.assertIn("leak investigation", questions[1])
        self.assertIn("final refrigerant charge", questions[2])
        self.assertEqual(
            questions[-1],
            "Can you provide an itemized breakdown of the refrigerant, labor, and other charges included in the $950 total?",
        )
        self.assertEqual(
            sum(
                any(
                    term in question.lower()
                    for term in ("price", "cost", "itemized", "breakdown", "charges")
                )
                for question in questions
            ),
            1,
        )
        self.assertNotIn("isn't lost again", " ".join(questions).lower())

    def test_refrigerant_verification_question_is_preserved_before_pricing(self):
        analysis = make_analysis(
            technical_support="PARTIALLY_SUPPORTED",
            pricing_transparency="LIMITED",
            pricing_review="The quoted recharge total is $950.",
            missing_information=(
                "The proposal does not explain how final refrigerant charge and "
                "system operation will be verified."
            ),
            contractor_questions=[
                "What testing confirmed that the system is low on refrigerant?",
                "Was the cause of the low charge evaluated?",
                "Can you provide separate pricing for refrigerant and labor?",
            ],
        )

        report = build_report_html(analysis)
        questions = report_list_items(report, "Questions to Ask Your Contractor")

        self.assertIn("refrigerant charge", questions[2])
        self.assertIn("verified", questions[2])
        self.assertIn("itemized breakdown", questions[-1])
        self.assertLess(
            questions.index(questions[2]),
            questions.index(questions[-1]),
        )

    def test_mandatory_leak_search_report_language_is_calibrated(self):
        analysis = make_analysis(
            technical_support="PARTIALLY_SUPPORTED",
            missing_information=(
                "No leak search included to determine the source of the low refrigerant charge."
            ),
            installation_concerns=(
                "The proposal lacks a critical step: checking for leaks before recharge."
            ),
            red_flags=[
                "No leak search included, which is essential for identifying the reason for the low refrigerant charge."
            ],
        )

        report = build_report_html(analysis)
        report_text = html.unescape(report).lower()

        self.assertNotIn("no leak search included", report_text)
        self.assertNotIn("leak search is essential", report_text)
        self.assertNotIn("critical step: checking for leaks", report_text)
        self.assertIn("whether the underlying cause was evaluated", report_text)
        self.assertIn("before recommending recharge-only work", report_text)

    def test_unsupported_questions_keep_diagnosis_and_scope_before_pricing(self):
        analysis = make_analysis(
            technical_support="UNSUPPORTED",
            pricing_transparency="LIMITED",
            contractor_questions=[
                "What test results support replacing the compressor?",
                "What part of the proposed scope addresses the documented failure?",
            ],
        )

        report = build_report_html(analysis)
        questions = report_list_items(report, "Questions to Ask Your Contractor")

        self.assertEqual(analysis.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertIn("test results", questions[0])
        self.assertIn("proposed scope", questions[1])
        self.assertIn("itemized breakdown", questions[-1])

    def test_supported_adequate_report_suppresses_contractor_questions(self):
        analysis = make_analysis()

        report = build_report_html(analysis)

        self.assertNotIn("Questions to Ask Your Contractor", report)

    def test_multi_quote_questions_identify_relevant_quote(self):
        analysis = make_analysis(
            technical_support="PARTIALLY_SUPPORTED",
            pricing_transparency="LIMITED",
            contractor_questions=[
                "For Quote 2, what testing supports the compressor replacement?"
            ],
        )

        report = build_report_html(analysis, quote_count=2)
        questions = report_list_items(report, "Questions to Ask Your Contractor")

        self.assertTrue(questions[0].startswith("For Quote 2"))
        self.assertTrue(questions[-1].startswith("For each quote with bundled pricing"))

    def test_duplicate_structured_questions_are_rendered_once(self):
        question = "What testing supports the proposed compressor replacement?"
        analysis = make_analysis(
            technical_support="PARTIALLY_SUPPORTED",
            contractor_questions=[question, question, f"  {question}  "],
        )

        report = build_report_html(analysis)
        questions = report_list_items(report, "Questions to Ask Your Contractor")

        self.assertEqual(questions, [question])

    def test_contractor_question_rules_do_not_assume_confirmed_leak(self):
        self.assertIn("must not assume an unproven diagnosis", CONTRACTOR_QUESTION_RULES)
        self.assertIn("whether leak investigation is appropriate", CONTRACTOR_QUESTION_RULES)
        self.assertIn(
            "do not ask how the contractor will prevent refrigerant from being lost again",
            CONTRACTOR_QUESTION_RULES,
        )
        self.assertIn(
            'Do not ask "Why was a leak search not included?"',
            CONTRACTOR_QUESTION_RULES,
        )

    def test_presumptive_leak_search_question_is_suppressed(self):
        neutral_question = (
            "Was leak investigation performed or recommended, and what evidence "
            "supports recharge-only work?"
        )
        analysis = make_analysis(
            technical_support="PARTIALLY_SUPPORTED",
            contractor_questions=[
                "Why was a leak search not included in this proposal?",
                neutral_question,
            ],
        )

        report = build_report_html(analysis)
        questions = report_list_items(report, "Questions to Ask Your Contractor")

        self.assertIn(neutral_question, questions)
        self.assertNotIn("Why was a leak search not included", report)

    def test_partial_support_removes_clear_diagnosis_good_sign(self):
        analysis = make_analysis(
            technical_support="PARTIALLY_SUPPORTED",
            good_signs=[
                "A clear diagnosis of low refrigerant charge.",
                "The proposal documents a 90-day repair warranty.",
            ],
        )

        analysis = finalize_customer_analysis(analysis)

        self.assertEqual(
            analysis.good_signs,
            ["The proposal documents a 90-day repair warranty."],
        )

    def test_unsupported_diagnosis_is_not_praised_as_good_sign(self):
        analysis = make_analysis(
            technical_support="UNSUPPORTED",
            good_signs=[
                "The compressor diagnosis is confirmed and well-supported.",
                "The proposal documents a one-year labor warranty.",
            ],
        )

        analysis = finalize_customer_analysis(analysis)

        self.assertEqual(
            analysis.good_signs,
            ["The proposal documents a one-year labor warranty."],
        )

    def test_limited_pricing_alone_does_not_create_red_flag(self):
        analysis = make_analysis(
            pricing_transparency="LIMITED",
            red_flags=["Pricing is bundled and no itemized breakdown is provided."],
        )

        analysis = finalize_customer_analysis(analysis)
        report = build_report_html(analysis)

        self.assertEqual(analysis.red_flags, [])
        self.assertEqual(analysis.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertIn(PRICING_REQUIRED_ACTION, analysis.decision.required_actions)
        self.assertIn("No major red flags were identified", report)

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
