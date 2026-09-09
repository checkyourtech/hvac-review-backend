import inspect
import unittest
from pathlib import Path

import main
from main import (
    ANALYSIS_MODULES,
    AnalysisModule,
    HVACAnalysis,
    HVACDecision,
    ReplacementContext,
    TechnicalEvidenceAssessment,
    derive_technical_support,
    finalize_customer_analysis,
)


def assessment(
    status="ADEQUATE",
    scope="APPROPRIATE",
    *,
    subject="Basis for full-system replacement",
    evidence=None,
    gaps=None,
    contradictions=None,
):
    return TechnicalEvidenceAssessment(
        subject=subject,
        materiality="PRIMARY",
        diagnostic_evidence_status=status,
        scope_support=scope,
        documented_evidence=(
            ["The quote documents the replacement basis."]
            if evidence is None
            else evidence
        ),
        material_gaps=gaps or [],
        contradictions=contradictions or [],
    )


def analysis_with(
    assessments,
    *,
    context=ReplacementContext.UNKNOWN,
    pricing="ADEQUATE",
    questions=None,
):
    return HVACAnalysis(
        project_overview="A replacement proposal was submitted.",
        equipment_analysis="The submitted equipment and work were reviewed.",
        missing_information="No other important information is missing.",
        pricing_review="The quoted price was reviewed separately.",
        installation_concerns="No separate installation concern is included in this test.",
        quote_comparison="",
        best_quote_recommendation="",
        contractor_vetting="",
        red_flags=[],
        good_signs=[],
        contractor_questions=questions or [],
        recommendation="AI recommendation",
        replacement_context=context,
        decision=HVACDecision(
            verdict="PROCEED",
            technical_support="SUPPORTED",
            pricing_transparency=pricing,
            required_actions=[],
            optional_suggestions=[],
            verdict_reasons=[],
        ),
        technical_assessments=assessments,
    )


class ReplacementContextAndRoutingTests(unittest.TestCase):
    def test_replacement_context_is_backward_compatible_and_defaults_unknown(self):
        payload = analysis_with([assessment()]).model_dump()
        payload.pop("replacement_context")

        parsed = HVACAnalysis.model_validate(payload)

        self.assertEqual(parsed.replacement_context, ReplacementContext.UNKNOWN)

    def test_all_replacement_context_values_are_available(self):
        self.assertEqual(
            {item.value for item in ReplacementContext},
            {
                "failure_driven",
                "safety_driven",
                "economic_condition",
                "elective",
                "unknown",
            },
        )

    def test_full_replacements_route_repair_vs_replace(self):
        source = " ".join(inspect.getsource(main.classify_quotes).split())
        for phrase in (
            "complete HVAC system",
            "furnace",
            "air conditioner",
            "heat-pump/air-handler system",
            "homeowner-requested proactive replacement",
            "fuel conversion",
            "electrification",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)
        self.assertIn("Select repair_vs_replace whenever", source)
        self.assertIn("select BOTH repair_vs_replace and equipment_matching", source)

    def test_minor_repairs_are_excluded_unless_replacement_is_material(self):
        source = " ".join(inspect.getsource(main.classify_quotes).split())
        self.assertIn("Do not select repair_vs_replace for a routine capacitor", source)
        self.assertIn("minor control repair unless major equipment replacement", source)

    def test_registry_uses_one_consolidated_replacement_basis_prompt(self):
        knowledge = ANALYSIS_MODULES[AnalysisModule.REPAIR_VS_REPLACE]
        self.assertIn("REPLACEMENT BASIS / REPAIR VS REPLACEMENT", knowledge)
        self.assertEqual(knowledge.count("REPLACEMENT BASIS / REPAIR VS REPLACEMENT"), 1)
        self.assertIn('"Basis for full-system replacement."', knowledge)
        self.assertIn("Do not independently diagnose a compressor", knowledge)
        self.assertNotIn("repair appears economically reasonable", knowledge)
        self.assertIn("does not by itself prove", knowledge)
        self.assertIn("Do not say a compressor failure alone", knowledge)


class ReplacementBasisCalibrationTests(unittest.TestCase):
    def test_context_calibration_is_explicit_in_module(self):
        knowledge = ANALYSIS_MODULES[AnalysisModule.REPAIR_VS_REPLACE]
        expected = {
            ReplacementContext.FAILURE_DRIVEN: "a diagnosed major failure",
            ReplacementContext.SAFETY_DRIVEN: "a supported safety condition",
            ReplacementContext.ECONOMIC_CONDITION: "documented age, condition, repair history",
            ReplacementContext.ELECTIVE: "the homeowner explicitly requested",
            ReplacementContext.UNKNOWN: "does not make the reason clear",
        }
        for context, phrase in expected.items():
            with self.subTest(context=context):
                self.assertIn(context.name, knowledge)
                self.assertIn(phrase, knowledge)

    def test_fixture_calibration_matrix(self):
        cases = {
            "replacement_basis_good_test.txt": (
                ReplacementContext.FAILURE_DRIVEN,
                assessment("ADEQUATE", "APPROPRIATE"),
                "SUPPORTED",
            ),
            "replacement_basis_partial_test.txt": (
                ReplacementContext.ECONOMIC_CONDITION,
                assessment("INCOMPLETE", "PARTIALLY_DEFINED"),
                "PARTIALLY_SUPPORTED",
            ),
            "replacement_basis_bad_test.txt": (
                ReplacementContext.UNKNOWN,
                assessment("ABSENT", "UNSUPPORTED", evidence=[]),
                "UNSUPPORTED",
            ),
            "replacement_basis_elective_test.txt": (
                ReplacementContext.ELECTIVE,
                assessment("ADEQUATE", "APPROPRIATE"),
                "SUPPORTED",
            ),
        }
        for fixture, (context, item, expected_support) in cases.items():
            with self.subTest(fixture=fixture):
                quote = Path(fixture).read_text(encoding="utf-8")
                self.assertTrue(quote.strip())
                self.assertIn("replacement", quote.lower())
                self.assertIsInstance(context, ReplacementContext)
                self.assertEqual(derive_technical_support([item]), expected_support)

    def test_safety_driven_supported_basis(self):
        item = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            evidence=["The heat-exchanger assessment documents a confirmed safety failure."],
        )
        finalized = finalize_customer_analysis(
            analysis_with([item], context=ReplacementContext.SAFETY_DRIVEN)
        )
        self.assertEqual(finalized.decision.technical_support, "SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "PROCEED")

    def test_partial_basis_customer_presentation_and_question_order(self):
        item = assessment(
            "INCOMPLETE",
            "PARTIALLY_DEFINED",
            gaps=["The quote does not explain why replacement is preferred."],
        )
        raw = analysis_with(
            [item],
            context=ReplacementContext.ECONOMIC_CONDITION,
            pricing="LIMITED",
            questions=[
                "Why should this system be replaced instead of repaired?",
                "What makes replacement the better option than repairing it?",
                "Can you itemize the total price?",
            ],
        )

        finalized = finalize_customer_analysis(raw, quote_text="$16,500 total", quote_count=1)

        self.assertEqual(finalized.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertIn("doesn't clearly explain why", finalized.equipment_analysis)
        self.assertEqual(
            sum(
                main.contractor_question_category(question) == "replacement_basis"
                for question in finalized.contractor_questions
            ),
            1,
        )
        self.assertEqual(
            main.contractor_question_category(finalized.contractor_questions[0]),
            "replacement_basis",
        )
        self.assertEqual(
            main.contractor_question_category(finalized.contractor_questions[-1]),
            "pricing",
        )

    def test_condition_history_partial_fixture_corrects_oversevere_ai_assessment(self):
        quote = Path("replacement_basis_partial_test.txt").read_text(encoding="utf-8")
        raw_basis = assessment(
            "ABSENT",
            "UNSUPPORTED",
            evidence=[
                "The system is 16 years old, described as in poor condition, and has "
                "needed repeated service."
            ],
            gaps=["The current problem and repair alternative are not explained."],
        )
        raw = analysis_with(
            [raw_basis],
            context=ReplacementContext.ECONOMIC_CONDITION,
            pricing="LIMITED",
            questions=[
                "What specifically is wrong with the existing equipment that makes "
                "replacement necessary?",
                "What specific problems were identified with the existing HVAC system?",
                "How does the contractor ensure the new installation will be reliable "
                "and avoid similar issues in the future?",
                "Can you provide a price breakdown?",
            ],
        )
        raw.red_flags = [
            "No specific diagnostic findings about the existing system are documented.",
            "No repair option or replacement rationale is provided.",
        ]
        raw.installation_concerns = (
            "Without documentation of the existing failures, the effectiveness of the "
            "replacement may be uncertain."
        )
        before = raw.technical_assessments[0].model_dump()

        finalized = finalize_customer_analysis(raw, quote_text=quote, quote_count=1)
        basis = next(
            item
            for item in finalized.technical_assessments
            if item.subject == "Basis for full-system replacement"
        )

        self.assertEqual(raw.technical_assessments[0].model_dump(), before)
        self.assertEqual(basis.diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(basis.scope_support, "PARTIALLY_DEFINED")
        self.assertEqual(finalized.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(finalized.red_flags, [])
        normalized_quote = " ".join(quote.lower().split())
        self.assertIn("16 years old", normalized_quote)
        self.assertIn("poor condition", normalized_quote)
        self.assertIn("repeated service", normalized_quote)
        self.assertIn("doesn't clearly explain what is wrong now", finalized.equipment_analysis)
        self.assertIn("system's age", finalized.equipment_analysis)
        self.assertIn("reported condition", finalized.equipment_analysis)
        self.assertIn("repeated service history", finalized.equipment_analysis)
        self.assertNotIn("solely on its age", finalized.equipment_analysis.lower())
        self.assertNotIn("age alone", finalized.equipment_analysis.lower())
        self.assertIn("current problem", finalized.missing_information)
        self.assertIn("why replacement is recommended instead of repair", finalized.missing_information)
        self.assertNotIn("repair option", finalized.missing_information.lower())
        self.assertNotIn("repair estimate", finalized.missing_information.lower())
        self.assertIn("whether replacement is the best course", finalized.installation_concerns)
        self.assertNotIn("effectiveness", finalized.installation_concerns.lower())
        self.assertNotIn("scope seems adequate", finalized.installation_concerns.lower())
        self.assertIn("age, reported condition, and repeated service", finalized.homeowner_takeaway)
        self.assertIn("Replacement may make sense", finalized.bottom_line)
        self.assertEqual(len(finalized.contractor_questions), 2)
        self.assertEqual(
            main.contractor_question_category(finalized.contractor_questions[0]),
            "replacement_basis",
        )
        self.assertEqual(
            main.contractor_question_category(finalized.contractor_questions[1]),
            "pricing",
        )
        self.assertFalse(
            any(
                term in question.lower()
                for question in finalized.contractor_questions
                for term in ("reliable", "reliability", "avoid similar issues")
            )
        )

    def test_age_alone_does_not_upgrade_unsupported_replacement_basis(self):
        quote = Path("replacement_basis_bad_test.txt").read_text(encoding="utf-8")
        raw = analysis_with(
            [assessment("ABSENT", "UNSUPPORTED", evidence=[])],
            context=ReplacementContext.UNKNOWN,
        )

        finalized = finalize_customer_analysis(raw, quote_text=quote, quote_count=1)
        basis = finalized.technical_assessments[0]

        self.assertEqual(basis.diagnostic_evidence_status, "ABSENT")
        self.assertEqual(basis.scope_support, "UNSUPPORTED")
        self.assertEqual(finalized.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(finalized.decision.verdict, "GET_A_SECOND_OPINION")

    def test_bad_fixture_collapses_basis_gaps_and_uses_homeowner_language(self):
        quote = Path("replacement_basis_bad_test.txt").read_text(encoding="utf-8")
        raw = analysis_with(
            [assessment("ABSENT", "UNSUPPORTED", evidence=[])],
            context=ReplacementContext.UNKNOWN,
            pricing="LIMITED",
            questions=[
                "Can you provide diagnostic measurements or tests that support this recommendation?",
                "What specific findings led you to conclude that replacement is necessary?",
                "Can you provide an itemized price breakdown?",
            ],
        )
        raw.red_flags = [
            "No failed component is identified.",
            "No diagnostic measurements are documented.",
            "No repair option explains why replacement is necessary.",
        ]
        raw.installation_concerns = (
            "Replacement may not be the most efficient or appropriate solution."
        )
        raw.project_overview = "Pricing is secondary to the technical concern."

        finalized = finalize_customer_analysis(raw, quote_text=quote, quote_count=1)

        self.assertEqual(finalized.replacement_context, ReplacementContext.UNKNOWN)
        self.assertEqual(
            finalized.technical_assessments[0].diagnostic_evidence_status,
            "ABSENT",
        )
        self.assertEqual(finalized.technical_assessments[0].scope_support, "UNSUPPORTED")
        self.assertEqual(finalized.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(finalized.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(len(finalized.red_flags), 1)
        self.assertIn("clear technical, condition, economic, or elective reason", finalized.red_flags[0])
        self.assertNotIn("failed component", finalized.red_flags[0].lower())
        self.assertNotIn("diagnostic measurements", finalized.red_flags[0].lower())
        self.assertNotIn("repair option", finalized.red_flags[0].lower())
        self.assertIn("doesn't show enough", finalized.equipment_analysis)
        self.assertIn("age and a poor-cooling complaint alone", finalized.equipment_analysis)
        self.assertIn("right course", finalized.installation_concerns)
        self.assertNotIn("efficient or appropriate solution", finalized.installation_concerns)
        self.assertEqual(len(finalized.contractor_questions), 2)
        self.assertEqual(
            [
                main.contractor_question_category(question)
                for question in finalized.contractor_questions
            ],
            ["replacement_basis", "pricing"],
        )
        self.assertFalse(
            any("diagnostic measurements" in question.lower() for question in finalized.contractor_questions)
        )
        self.assertIn("full replacement", finalized.homeowner_takeaway)
        self.assertNotIn("main diagnosis or repair", finalized.homeowner_takeaway.lower())
        self.assertIn("$15,900 replacement", finalized.bottom_line)
        self.assertNotIn("main diagnosis or repair", finalized.bottom_line.lower())
        customer_text = " ".join(
            [
                finalized.project_overview,
                finalized.equipment_analysis,
                finalized.missing_information,
                finalized.pricing_review,
                finalized.installation_concerns,
                finalized.recommendation,
                finalized.banner_explanation,
                finalized.homeowner_takeaway,
                finalized.bottom_line,
                *finalized.red_flags,
                *finalized.good_signs,
                *finalized.contractor_questions,
            ]
        ).lower()
        for formal_phrase in (
            "technical concern is primary",
            "primary concern is technical",
            "principal deficiency",
            "dominant concern",
            "pricing is secondary to the technical concern",
            "insufficient to substantiate",
            "rationale is insufficient",
            "material information is absent",
        ):
            with self.subTest(formal_phrase=formal_phrase):
                self.assertNotIn(formal_phrase, customer_text)
        self.assertIn("clear up the technical issue first", finalized.project_overview.lower())

    def test_unsupported_basis_is_neutral_not_absolute(self):
        item = assessment("ABSENT", "UNSUPPORTED", evidence=[])
        finalized = finalize_customer_analysis(
            analysis_with([item], context=ReplacementContext.UNKNOWN)
        )

        self.assertEqual(finalized.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(finalized.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertIn("doesn't show enough", finalized.equipment_analysis)
        self.assertIn("do not establish that replacement is the right next step", finalized.equipment_analysis)
        self.assertEqual(
            sum(
                main.contractor_question_category(question) == "replacement_basis"
                for question in finalized.contractor_questions
            ),
            1,
        )

    def test_supported_elective_basis_does_not_interrogate_failure(self):
        item = assessment("ADEQUATE", "APPROPRIATE")
        finalized = finalize_customer_analysis(
            analysis_with(
                [item],
                context=ReplacementContext.ELECTIVE,
                questions=[
                    "What failed in the existing system?",
                    "Why can't the existing system be repaired?",
                ],
            )
        )

        self.assertEqual(finalized.decision.technical_support, "SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "PROCEED")
        self.assertIn("planned upgrade", finalized.equipment_analysis)
        self.assertIn("not a replacement caused by", finalized.equipment_analysis)
        self.assertEqual(finalized.contractor_questions, [])

    def test_elective_fixture_with_missing_equipment_match_is_partial_not_unsupported(self):
        quote = Path("replacement_basis_elective_test.txt").read_text(encoding="utf-8")
        basis = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            evidence=["The homeowner requested a proactive replacement during a remodel."],
        )
        matching = assessment(
            "ABSENT",
            "UNSUPPORTED",
            subject="Quoted indoor/outdoor equipment compatibility",
            evidence=[],
            gaps=["Exact proposed equipment models and matching documentation are missing."],
        )
        raw = analysis_with(
            [basis, matching],
            context=ReplacementContext.ELECTIVE,
            pricing="LIMITED",
            questions=[
                "What failed in the existing system?",
                "Why can't the existing system be repaired?",
                "Is the proposed heat pump compatible with the existing ductwork?",
                "How will you confirm broad system performance?",
                "Can you provide finer labor and component itemization?",
            ],
        )
        raw.project_overview = "The new heat pump will improve efficiency."
        raw.installation_concerns = (
            "These steps show a comprehensive approach. Confirm local code requirements "
            "and obtain permits if needed."
        )
        raw.red_flags = [
            "Exact equipment models and matching documentation are not provided."
        ]
        raw.good_signs = [
            "The proposal clearly identifies this as a proactive homeowner-requested replacement."
        ]
        before = [item.model_dump() for item in raw.technical_assessments]

        finalized = finalize_customer_analysis(raw, quote_text=quote, quote_count=1)
        after = [item.model_dump() for item in finalized.technical_assessments]
        finalized_basis = after[0]
        finalized_matching = after[1]

        self.assertEqual([item.model_dump() for item in raw.technical_assessments], before)
        self.assertEqual(finalized_basis, before[0])
        self.assertEqual(finalized_matching["diagnostic_evidence_status"], "INCOMPLETE")
        self.assertEqual(finalized_matching["scope_support"], "PARTIALLY_DEFINED")
        self.assertEqual(finalized_matching["contradictions"], [])
        self.assertEqual(finalized.replacement_context, ReplacementContext.ELECTIVE)
        self.assertEqual(finalized.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(finalized.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(finalized.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(finalized.red_flags, [])
        self.assertTrue(finalized.recommendation.startswith("REVIEW BEFORE APPROVING"))
        self.assertIn("planned upgrade", finalized.equipment_analysis)
        self.assertIn("exact proposed equipment", finalized.equipment_analysis)
        self.assertIn("higher efficiency", finalized.project_overview)
        self.assertNotIn("will improve efficiency", finalized.project_overview.lower())
        self.assertIn("exact indoor and outdoor equipment models", finalized.missing_information.lower())
        self.assertNotIn("failure", finalized.missing_information.lower())
        self.assertNotIn("comprehensive approach", finalized.installation_concerns.lower())
        self.assertNotIn("local code", finalized.installation_concerns.lower())
        self.assertTrue(
            any("homeowner-requested" in sign.lower() for sign in finalized.good_signs)
        )
        self.assertNotIn("main diagnosis or repair", finalized.homeowner_takeaway.lower())
        self.assertIn("lack of a failure diagnosis is not a concern", finalized.homeowner_takeaway)
        self.assertIn("exact equipment", finalized.bottom_line)
        self.assertEqual(finalized.contractor_questions, [
            "What exact indoor and outdoor equipment models are being installed, and can "
            "you provide the manufacturer or AHRI match for that combination?",
        ])
        self.assertFalse(
            any("duct" in question.lower() for question in finalized.contractor_questions)
        )
        self.assertFalse(
            any(
                main.contractor_question_category(question) in {"replacement_basis", "pricing"}
                for question in finalized.contractor_questions
            )
        )

    def test_elective_generic_equipment_cannot_preserve_false_supported_match(self):
        quote = Path("replacement_basis_elective_test.txt").read_text(encoding="utf-8")
        basis = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            evidence=["The homeowner requested a proactive replacement during a remodel."],
        )
        falsely_supported_matching = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            subject="Quoted indoor/outdoor equipment compatibility",
            evidence=["The proposal identifies a replacement heat-pump system."],
        )
        raw = analysis_with(
            [basis, falsely_supported_matching],
            context=ReplacementContext.ELECTIVE,
            pricing="ADEQUATE",
            questions=[
                "Why is replacement necessary?",
                "Is the proposed system compatible with the ductwork?",
            ],
        )
        raw.missing_information = (
            "No critical missing information was found because the equipment type is listed."
        )
        raw.installation_concerns = (
            "Startup is necessary to ensure the new system functions correctly."
        )

        finalized = finalize_customer_analysis(raw, quote_text=quote, quote_count=1)
        matching = main.primary_equipment_matching_assessment(finalized)

        self.assertIsNotNone(matching)
        self.assertEqual(matching.diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(matching.scope_support, "PARTIALLY_DEFINED")
        self.assertEqual(finalized.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(finalized.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(finalized.red_flags, [])
        self.assertIn(
            "exact indoor and outdoor equipment models",
            finalized.missing_information.lower(),
        )
        self.assertNotIn(
            "ensure the new system functions correctly",
            finalized.installation_concerns.lower(),
        )
        self.assertEqual(
            finalized.contractor_questions,
            [
                "What exact indoor and outdoor equipment models are being installed, and can "
                "you provide the manufacturer or AHRI match for that combination?",
            ],
        )
        self.assertFalse(
            any(
                main.contractor_question_category(question)
                in {"replacement_basis", "pricing", "compatibility"}
                for question in finalized.contractor_questions
            )
        )

    def test_live_elective_noncanonical_absent_match_is_finalized_as_partial(self):
        quote = Path("replacement_basis_elective_test.txt").read_text(encoding="utf-8")
        matching = assessment(
            "ABSENT",
            "UNSUPPORTED",
            subject="Heat pump model selection",
            evidence=["The homeowner is voluntarily replacing the system with a heat pump."],
            gaps=["Exact model number of the heat pump being proposed."],
        )
        installation_scope = assessment(
            "CONFIRMED",
            "APPROPRIATE",
            subject="Replacement scope and verification",
            evidence=[
                "The proposal includes removal, installation, startup, and operational verification."
            ],
        )
        raw = analysis_with(
            [matching, installation_scope],
            context=ReplacementContext.ELECTIVE,
            pricing="LIMITED",
            questions=[
                "What heat pump model is proposed, and how does it compare with the old system?",
                "How will startup ensure the heat pump operates effectively?",
                "Is the proposed heat pump compatible with the existing ductwork?",
                "Can you provide an itemized price breakdown?",
            ],
        )
        raw.missing_information = "No critical missing information was found."
        raw.pricing_review = (
            "The category breakdown is adequate, but finer itemization would enhance transparency."
        )
        raw.installation_concerns = "The scope seems adequate for a full system replacement."
        before = [item.model_dump() for item in raw.technical_assessments]

        finalized = finalize_customer_analysis(raw, quote_text=quote, quote_count=1)
        after = [item.model_dump() for item in finalized.technical_assessments]
        final_matching = main.primary_equipment_matching_assessment(finalized)
        final_basis = main.replacement_basis_assessment(finalized)

        self.assertEqual([item.model_dump() for item in raw.technical_assessments], before)
        self.assertEqual(before[0]["diagnostic_evidence_status"], "ABSENT")
        self.assertEqual(before[0]["scope_support"], "UNSUPPORTED")
        self.assertEqual(final_matching.subject, "Heat pump model selection")
        self.assertEqual(final_matching.diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(final_matching.scope_support, "PARTIALLY_DEFINED")
        self.assertIsNotNone(final_basis)
        self.assertEqual(final_basis.diagnostic_evidence_status, "ADEQUATE")
        self.assertEqual(final_basis.scope_support, "APPROPRIATE")
        self.assertEqual(len(after), 3)
        self.assertEqual(finalized.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(finalized.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(finalized.red_flags, [])
        self.assertIn("exact indoor and outdoor equipment models", finalized.missing_information.lower())
        self.assertNotIn("diagnosis or repair", finalized.homeowner_takeaway.lower())
        self.assertIn("planned homeowner-requested upgrade", finalized.homeowner_takeaway.lower())
        self.assertIn("exact equipment", finalized.bottom_line.lower())
        self.assertEqual(
            finalized.contractor_questions,
            [
                "What exact indoor and outdoor equipment models are being installed, and can "
                "you provide the manufacturer or AHRI match for that combination?",
            ],
        )
        self.assertNotIn("itemiz", finalized.pricing_review.lower())
        self.assertNotIn("seems adequate", finalized.installation_concerns.lower())

    def test_elective_noncanonical_real_match_contradiction_remains_unsupported(self):
        quote = Path("replacement_basis_elective_test.txt").read_text(encoding="utf-8")
        basis = assessment("ADEQUATE", "APPROPRIATE")
        conflicting_match = assessment(
            "CONTRADICTORY",
            "UNSUPPORTED",
            subject="Heat pump model selection",
            evidence=["The quoted and submitted models differ."],
            gaps=["The exact indoor and outdoor model combination is unresolved."],
            contradictions=["The submitted match document lists different equipment."],
        )

        finalized = finalize_customer_analysis(
            analysis_with(
                [basis, conflicting_match],
                context=ReplacementContext.ELECTIVE,
            ),
            quote_text=quote,
            quote_count=1,
        )

        final_match = main.primary_equipment_matching_assessment(finalized)
        self.assertEqual(final_match.diagnostic_evidence_status, "CONTRADICTORY")
        self.assertEqual(final_match.scope_support, "UNSUPPORTED")
        self.assertEqual(finalized.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(finalized.decision.verdict, "GET_A_SECOND_OPINION")

    def test_elective_missing_matching_assessment_is_added_before_rendering(self):
        quote = Path("replacement_basis_elective_test.txt").read_text(encoding="utf-8")
        basis = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            evidence=["The homeowner requested a proactive replacement during a remodel."],
        )
        raw = analysis_with(
            [basis],
            context=ReplacementContext.ELECTIVE,
            pricing="LIMITED",
            questions=[
                "What specific model of heat pump will be installed?",
                "How will startup ensure the heat pump operates as intended?",
            ],
        )
        raw.good_signs = [
            "The proposal includes startup and verification, ensuring the new system operates as intended.",
            "Doing the work during a remodel can facilitate streamlined installation.",
            "The proposal clearly identifies a planned homeowner-requested upgrade.",
        ]
        raw.installation_concerns = (
            "The scope covers the fundamental steps required for a successful replacement."
        )

        finalized = finalize_customer_analysis(raw, quote_text=quote, quote_count=1)
        matching = main.primary_equipment_matching_assessment(finalized)
        rendered = main.build_report_html(finalized, quote_count=1)

        self.assertIsNotNone(matching)
        self.assertEqual(matching.diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(matching.scope_support, "PARTIALLY_DEFINED")
        self.assertEqual(finalized.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(finalized.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(finalized.red_flags, [])
        self.assertIn("Diagnostic Support</strong>\n                Partially Supported", rendered)
        self.assertNotIn("Diagnostic Support</strong>\n                Supported", rendered)
        self.assertEqual(
            finalized.contractor_questions,
            [
                "What exact indoor and outdoor equipment models are being installed, and can "
                "you provide the manufacturer or AHRI match for that combination?"
            ],
        )
        self.assertNotIn("diagnosis and planned work", finalized.homeowner_takeaway.lower())
        self.assertNotIn("diagnosis and planned work", finalized.bottom_line.lower())
        self.assertFalse(any("streamlin" in sign.lower() for sign in finalized.good_signs))
        self.assertFalse(any("ensure" in sign.lower() for sign in finalized.good_signs))
        self.assertIn("The proposal includes startup verification.", finalized.good_signs)
        self.assertNotIn("successful replacement", finalized.installation_concerns.lower())

    def test_elective_exact_models_with_submitted_match_documentation_stay_supported(self):
        quote = (
            "The homeowner requested a proactive replacement during a planned remodel.\n"
            "Replacement heat-pump system\n"
            "Outdoor heat pump: Northstar NHP36-24V\n"
            "Indoor air handler: Northstar NAH36-24V\n"
            "AHRI Certificate Reference 214567890 ties those exact models together.\n"
        )
        basis = assessment("ADEQUATE", "APPROPRIATE")
        matching = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            subject="Quoted indoor/outdoor equipment compatibility",
        )

        finalized = finalize_customer_analysis(
            analysis_with([basis, matching], context=ReplacementContext.ELECTIVE),
            quote_text=quote,
            quote_count=1,
        )

        final_matching = main.primary_equipment_matching_assessment(finalized)
        self.assertEqual(final_matching.diagnostic_evidence_status, "ADEQUATE")
        self.assertEqual(final_matching.scope_support, "APPROPRIATE")
        self.assertEqual(finalized.decision.technical_support, "SUPPORTED")

    def test_replacement_finalizer_does_not_mutate_structured_assessment(self):
        item = assessment(
            "INCOMPLETE",
            "PARTIALLY_DEFINED",
            evidence=["The system has needed repeated service."],
            gaps=["The current condition is not explained."],
        )
        raw = analysis_with([item], context=ReplacementContext.ECONOMIC_CONDITION)
        before = raw.technical_assessments[0].model_dump()

        finalized = finalize_customer_analysis(raw)

        self.assertEqual(raw.technical_assessments[0].model_dump(), before)
        self.assertEqual(finalized.technical_assessments[0].model_dump(), before)

    def test_pricing_remains_independent(self):
        item = assessment("ADEQUATE", "APPROPRIATE")
        finalized = finalize_customer_analysis(
            analysis_with([item], pricing="LIMITED"),
            quote_text="$17,800 total",
            quote_count=1,
        )
        self.assertEqual(finalized.decision.technical_support, "SUPPORTED")
        self.assertEqual(finalized.decision.pricing_transparency, "LIMITED")
        self.assertEqual(finalized.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(
            sum(
                main.contractor_question_category(question) == "pricing"
                for question in finalized.contractor_questions
            ),
            1,
        )

    def test_good_fixture_category_pricing_and_both_primary_assessments_proceed(self):
        quote = Path("replacement_basis_good_test.txt").read_text(encoding="utf-8")
        basis = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            evidence=[
                "The grounded compressor, system age, repair option, and repair history "
                "support replacement."
            ],
        )
        matching = assessment(
            "CONFIRMED",
            "APPROPRIATE",
            subject="Quoted indoor/outdoor equipment compatibility",
            evidence=["Submitted AHRI documentation ties the exact models together."],
        )
        finalized = finalize_customer_analysis(
            (raw := analysis_with(
                [basis, matching],
                context=ReplacementContext.FAILURE_DRIVEN,
                pricing="LIMITED",
                questions=[
                    "Why should the existing system be replaced?",
                    "Can you provide finer equipment and labor itemization?",
                ],
            )),
            quote_text=quote,
            quote_count=1,
        )

        self.assertEqual(finalized.decision.technical_support, "SUPPORTED")
        self.assertEqual(finalized.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(finalized.decision.verdict, "PROCEED")
        self.assertEqual(finalized.red_flags, [])
        self.assertEqual(finalized.contractor_questions, [])
        self.assertIn("compressor is grounded", finalized.equipment_analysis)
        self.assertIn("$6,900 compressor repair option", finalized.equipment_analysis)
        self.assertIn("17-year-old system", finalized.equipment_analysis)
        self.assertIn("major-repair history", finalized.equipment_analysis)
        self.assertIn("matching documentation", finalized.equipment_analysis)
        self.assertLess(
            finalized.equipment_analysis.index("compressor is grounded"),
            finalized.equipment_analysis.index("replacement is a reasonable option"),
        )
        self.assertIn("$17,800 total", finalized.pricing_review)
        self.assertIn("$10,800 for equipment", finalized.pricing_review)
        self.assertIn("$7,000 for labor and installation materials", finalized.pricing_review)

        raw.installation_concerns = (
            "The scope includes all necessary installation steps. This ensures no aspect "
            "of the system's operation is overlooked after installation. Confirm local "
            "code requirements and obtain permits if needed."
        )
        raw.missing_information = (
            "Warranty terms are not specified. Confirm local code requirements and obtain "
            "permits if they are not included."
        )
        raw.good_signs = [
            "The quote includes a 17-year-old system and a documented repair option.",
            "The comprehensive replacement scope ensures removal of the old system and "
            "installation of the new equipment.",
            "All crucial steps to ensure proper installation are in place.",
            "Startup contributes to the overall effectiveness of the new system.",
        ]
        polished = finalize_customer_analysis(raw, quote_text=quote, quote_count=1)
        combined = " ".join(
            (
                polished.installation_concerns,
                polished.missing_information,
                *polished.good_signs,
            )
        ).lower()
        for prohibited in (
            "all necessary installation steps",
            "no aspect of the system's operation is overlooked",
            "confirm local code requirements",
            "obtain permits if",
            "warranty terms are not specified",
            "comprehensive replacement scope ensures",
            "all crucial steps",
            "overall effectiveness",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, combined)
        self.assertIn("removal of the existing equipment", polished.installation_concerns)
        self.assertTrue(
            any(
                "removal of the existing equipment" in sign.lower()
                and "startup verification" in sign.lower()
                for sign in polished.good_signs
            )
        )
        self.assertTrue(
            any("documented repair option" in sign.lower() for sign in polished.good_signs)
        )
        self.assertIn("No important missing information", polished.missing_information)
        self.assertEqual(polished.decision.verdict, "PROCEED")
        self.assertEqual(polished.contractor_questions, [])
        self.assertIn("$6,900 compressor repair option", polished.equipment_analysis)
        self.assertIn("17-year-old system", polished.equipment_analysis)
        self.assertIn("major-repair history", polished.equipment_analysis)

    def test_repair_option_amount_is_not_used_as_replacement_pricing_total(self):
        quote = """
        Compressor repair option: $6,900
        Replacement scope: remove the old system and install replacement equipment.
        Total replacement price: $17,800
        Parts and labor are included in one total.
        """
        finalized = finalize_customer_analysis(
            analysis_with(
                [assessment("ADEQUATE", "APPROPRIATE")],
                context=ReplacementContext.FAILURE_DRIVEN,
                pricing="LIMITED",
            ),
            quote_text=quote,
            quote_count=1,
        )

        pricing_questions = [
            question
            for question in finalized.contractor_questions
            if main.contractor_question_category(question) == "pricing"
        ]
        self.assertEqual(len(pricing_questions), 1)
        self.assertIn("$17,800", pricing_questions[0])
        self.assertNotIn("$6,900", pricing_questions[0])


class ReplacementBasisCrossModuleTests(unittest.TestCase):
    def test_unsupported_upstream_diagnosis_cannot_be_rescued(self):
        replacement = assessment("ADEQUATE", "APPROPRIATE")
        for subject in (
            "Heat-exchanger condemnation and furnace replacement",
            "Compressor failure",
        ):
            with self.subTest(subject=subject):
                upstream = assessment("ABSENT", "UNSUPPORTED", subject=subject, evidence=[])
                self.assertEqual(
                    derive_technical_support([replacement, upstream]),
                    "UNSUPPORTED",
                )

    def test_supported_match_cannot_rescue_unsupported_basis(self):
        basis = assessment("ABSENT", "UNSUPPORTED", evidence=[])
        matching = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            subject="Quoted indoor/outdoor equipment compatibility",
        )
        self.assertEqual(derive_technical_support([basis, matching]), "UNSUPPORTED")

    def test_supported_basis_and_incomplete_match_are_partial(self):
        basis = assessment("ADEQUATE", "APPROPRIATE")
        matching = assessment(
            "INCOMPLETE",
            "PARTIALLY_DEFINED",
            subject="Quoted indoor/outdoor equipment compatibility",
        )
        self.assertEqual(
            derive_technical_support([basis, matching]),
            "PARTIALLY_SUPPORTED",
        )

    def test_supported_basis_and_match_are_supported(self):
        basis = assessment("ADEQUATE", "APPROPRIATE")
        matching = assessment(
            "ADEQUATE",
            "APPROPRIATE",
            subject="Quoted indoor/outdoor equipment compatibility",
        )
        self.assertEqual(derive_technical_support([basis, matching]), "SUPPORTED")

    def test_phase_2a_and_2b_calibrations_remain_available(self):
        heat_exchanger = ANALYSIS_MODULES[AnalysisModule.HEAT_EXCHANGER]
        equipment_matching = ANALYSIS_MODULES[AnalysisModule.EQUIPMENT_MATCHING]
        self.assertIn("HEAT EXCHANGER INTEGRITY AND CONDEMNATION", heat_exchanger)
        self.assertIn("clearly documented crack, hole, split, separation", heat_exchanger)
        self.assertIn("EQUIPMENT MATCHING AND COMPATIBILITY", equipment_matching)
        self.assertIn("INCOMPLETE + PARTIALLY_DEFINED", equipment_matching)


if __name__ == "__main__":
    unittest.main()
