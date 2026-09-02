import html
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from main import (
    AnalysisModule,
    HVACAnalysis,
    HVACDecision,
    QuoteClassification,
    TechnicalEvidenceAssessment,
    build_report_html,
    finalize_customer_analysis,
)


FIXTURE_PATH = Path("refrigerant_low_charge_bad_test.txt")


def raw_refrigerant_analysis():
    return HVACAnalysis(
        project_overview=(
            "The customer reported that the air conditioner is running but not "
            "cooling well, leading to a diagnosis of low refrigerant."
        ),
        equipment_analysis=(
            "The contractor proposes adding three pounds of refrigerant, but no "
            "measurements are documented."
        ),
        missing_information=(
            "The proposal does not include a leak search to confirm the source of "
            "the low refrigerant charge, which is essential before proceeding."
        ),
        pricing_review=(
            "The quoted $950 total bundles refrigerant, labor, and other charges."
        ),
        installation_concerns=(
            "The proposal lacks a critical step: confirming why the system is low. "
            "A proper diagnostic process usually involves checking for leaks."
        ),
        quote_comparison="Only one quote is present.",
        best_quote_recommendation="This is the only quote available.",
        contractor_vetting="",
        red_flags=[
            "The absence of a leak search raises concerns about potentially repeating "
            "the issue after refrigerant is added.",
            "The bundled price is not itemized.",
        ],
        good_signs=[
            "A clear diagnosis of low refrigerant charge.",
            "The proposal includes a 90-day repair warranty.",
        ],
        contractor_questions=[
            "What testing confirmed that the system is low on refrigerant?",
            "Why was a leak search not included?",
            "How will the proper refrigerant charge be verified after adding refrigerant?",
            "How will you verify the refrigerant charge and cooling performance after the work?",
            "Can you provide separate pricing for refrigerant and labor?",
            "Can you provide an itemized breakdown of the quoted total?",
        ],
        recommendation="Raw AI recommendation.",
        decision=HVACDecision(
            verdict="PROCEED",
            technical_support="PARTIALLY_SUPPORTED",
            pricing_transparency="LIMITED",
            required_actions=[],
            optional_suggestions=[],
            verdict_reasons=["Low-charge diagnostic evidence is incomplete."],
        ),
    )


def refrigerant_classification():
    return QuoteClassification(
        quote_type="repair",
        system_type="split air conditioner",
        primary_scope="Add refrigerant",
        repair_components=["refrigerant recharge"],
        diagnostic_evidence=[],
        modules_required=[AnalysisModule.REFRIGERANT_SYSTEM, AnalysisModule.PRICING],
    )


def raw_heat_exchanger_analysis(evidence_status, scope_support, evidence=None):
    return HVACAnalysis(
        project_overview="The contractor recommends replacing the furnace.",
        equipment_analysis=(
            "The proposal claims the heat exchanger is cracked. No combustion "
            "analysis was documented and no flue-gas CO measurements were provided."
        ),
        missing_information=(
            "No combustion analysis documented. No flue-gas CO measurements. "
            "No results from a borescope inspection."
        ),
        pricing_review="The $9,850 total is not itemized.",
        installation_concerns="The replacement scope includes startup checks.",
        quote_comparison="",
        best_quote_recommendation="",
        contractor_vetting="",
        red_flags=[
            "Absence of necessary combustion and safety testing raises concerns.",
            "No borescope inspection was documented for the heat exchanger.",
        ],
        good_signs=[],
        contractor_questions=[
            "Why were no CO measurements conducted during the diagnosis?",
            "Why wasn't combustion analysis performed?",
            "Can you provide an itemized price breakdown?",
        ],
        recommendation="Raw AI recommendation.",
        decision=HVACDecision(
            verdict="PROCEED",
            technical_support="SUPPORTED",
            pricing_transparency="LIMITED",
            required_actions=[],
            optional_suggestions=[],
            verdict_reasons=[],
        ),
        technical_assessments=[
            TechnicalEvidenceAssessment(
                subject="Heat-exchanger failure and furnace replacement",
                materiality="PRIMARY",
                diagnostic_evidence_status=evidence_status,
                scope_support=scope_support,
                documented_evidence=evidence or [],
                material_gaps=[],
                contradictions=[],
            )
        ],
    )


def parsed_completion(analysis):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=analysis))]
    )


def question_items(report):
    marker = "<h2>Questions to Ask Your Contractor</h2>"
    section = report.split(marker, 1)[1].split("</ul>", 1)[0]
    items = []
    for fragment in section.split("<li>")[1:]:
        items.append(html.unescape(fragment.split("</li>", 1)[0].strip()))
    return items


class CustomerAnalysisFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.quote_text = FIXTURE_PATH.read_text(encoding="utf-8")

    def test_finalizer_is_deep_copy_and_idempotent(self):
        raw = raw_refrigerant_analysis()
        raw_before = raw.model_dump()

        finalized = finalize_customer_analysis(raw, self.quote_text, 1)
        finalized_again = finalize_customer_analysis(finalized, self.quote_text, 1)

        self.assertEqual(raw.model_dump(), raw_before)
        self.assertIsNot(finalized, raw)
        self.assertEqual(finalized_again.model_dump(), finalized.model_dump())

    def test_live_phrase_variants_are_normalized_on_final_model(self):
        finalized = finalize_customer_analysis(
            raw_refrigerant_analysis(), self.quote_text, 1
        )
        combined = " ".join(
            [
                finalized.project_overview,
                finalized.missing_information,
                finalized.installation_concerns,
                *finalized.red_flags,
            ]
        ).lower()

        self.assertIn("customer reports poor cooling", finalized.project_overview.lower())
        self.assertIn("contractor attributes", finalized.project_overview.lower())
        self.assertNotIn("leading to a diagnosis", finalized.project_overview.lower())
        self.assertNotIn("does not include a leak search", combined)
        self.assertNotIn("absence of a leak search", combined)
        self.assertNotIn("leak search is essential", combined)
        self.assertIn("underlying cause was evaluated", combined)
        self.assertEqual(
            sum("verif" in question.lower() for question in finalized.contractor_questions),
            1,
        )
        self.assertEqual(
            sum(
                any(term in question.lower() for term in ("price", "cost", "itemiz", "breakdown", "charges"))
                for question in finalized.contractor_questions
            ),
            1,
        )

    def test_neutral_cause_and_verification_fallbacks_are_canonical(self):
        raw = raw_refrigerant_analysis()
        raw.contractor_questions = [
            "Why was a leak search not included?",
            "Can you provide separate pricing for refrigerant and labor?",
        ]

        finalized = finalize_customer_analysis(raw, self.quote_text, 1)
        questions = finalized.contractor_questions

        self.assertTrue(any("cause of the low charge" in q.lower() for q in questions))
        self.assertTrue(any("leak investigation" in q.lower() for q in questions))
        self.assertTrue(any("verif" in q.lower() for q in questions))
        self.assertNotIn("Why was a leak search not included?", questions)
        self.assertIn("$950", questions[-1])

    def test_unsupported_heat_exchanger_fields_use_evidence_category(self):
        raw = raw_heat_exchanger_analysis("ABSENT", "UNSUPPORTED")
        finalized = finalize_customer_analysis(raw, quote_count=1)

        self.assertEqual(finalized.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(finalized.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertIn("doesn't show the evidence", finalized.equipment_analysis)
        self.assertIn("failed area", finalized.missing_information)
        self.assertIn("meaningful inspection or test evidence", finalized.missing_information)
        self.assertIn("should not be approved until", finalized.installation_concerns)
        self.assertEqual(
            finalized.red_flags[0],
            "The furnace is being recommended for replacement without clear "
            "documentation showing that the heat exchanger has failed.",
        )

        customer_text = " ".join(
            (
                finalized.equipment_analysis,
                finalized.missing_information,
                *finalized.red_flags,
                *finalized.contractor_questions,
            )
        ).lower()
        for forbidden in (
            "no combustion analysis",
            "no flue-gas co",
            "no borescope",
            "why were no co",
            "why wasn't combustion",
        ):
            self.assertNotIn(forbidden, customer_text)

        self.assertTrue(
            any("evidence confirms" in q.lower() for q in finalized.contractor_questions)
        )
        self.assertEqual(
            sum(
                any(term in q.lower() for term in ("price", "itemiz", "breakdown"))
                for q in finalized.contractor_questions
            ),
            1,
        )

        finalized_again = finalize_customer_analysis(finalized, quote_count=1)
        self.assertEqual(finalized_again.model_dump(), finalized.model_dump())
        before_render = finalized.model_dump()
        report = html.unescape(build_report_html(finalized, quote_count=1))
        self.assertEqual(finalized.model_dump(), before_render)
        self.assertIn(finalized.red_flags[0], report)
        for question in finalized.contractor_questions:
            self.assertIn(question, report)
        self.assertNotIn("Why were no CO measurements", report)

    def test_partial_heat_exchanger_preserves_documented_findings(self):
        evidence = [
            "Flame changed when the blower started",
            "Flue-gas CO measured 165 ppm",
            "Borescope showed a questionable area near a seam",
        ]
        raw = raw_heat_exchanger_analysis(
            "INCOMPLETE",
            "PARTIALLY_DEFINED",
            evidence=evidence,
        )
        finalized = finalize_customer_analysis(raw, quote_count=1)

        self.assertEqual(finalized.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "REVIEW_BEFORE_APPROVING")
        for finding in evidence:
            self.assertIn(finding, finalized.equipment_analysis)
        self.assertIn("worth taking seriously", finalized.equipment_analysis)
        self.assertIn("doesn't clearly show", finalized.equipment_analysis)
        self.assertIn("may make sense", finalized.installation_concerns)
        self.assertNotIn("No combustion analysis", finalized.missing_information)
        self.assertFalse(
            any(q.lower().startswith("why") for q in finalized.contractor_questions)
        )

    def test_confirmed_heat_exchanger_does_not_require_optional_tests(self):
        raw = raw_heat_exchanger_analysis(
            "CONFIRMED",
            "APPROPRIATE",
            evidence=["A photo documents a crack in the heat exchanger"],
        )
        raw.decision.pricing_transparency = "ADEQUATE"
        finalized = finalize_customer_analysis(raw, quote_count=1)

        self.assertEqual(finalized.decision.technical_support, "SUPPORTED")
        self.assertEqual(finalized.decision.verdict, "PROCEED")
        self.assertEqual(finalized.red_flags, [])
        self.assertEqual(finalized.contractor_questions, [])
        self.assertIn("photo documents a crack", finalized.equipment_analysis)
        self.assertNotIn("No combustion analysis", finalized.equipment_analysis)
        self.assertIn("No important heat-exchanger evidence", finalized.missing_information)
        combined = " ".join(
            (
                finalized.equipment_analysis,
                finalized.missing_information,
                finalized.installation_concerns,
                *finalized.red_flags,
                *finalized.contractor_questions,
            )
        ).lower()
        self.assertNotIn("combustion analysis", combined)
        self.assertNotIn("co measurement", combined)
        self.assertNotIn("borescope inspection", combined)

    def test_renderer_is_pure_and_repeatable(self):
        finalized = finalize_customer_analysis(
            raw_refrigerant_analysis(), self.quote_text, 1
        )
        before = finalized.model_dump()

        first = build_report_html(finalized, quote_count=1)
        second = build_report_html(finalized, quote_count=1)

        self.assertEqual(first, second)
        self.assertEqual(finalized.model_dump(), before)
        for question in finalized.contractor_questions:
            self.assertIn(html.escape(question), first)

    def test_upload_endpoint_uses_finalized_refrigerant_values(self):
        captured = {}

        def capture_email(**kwargs):
            analysis = kwargs["analysis"]
            captured["analysis"] = analysis.model_copy(deep=True)
            captured["html"] = build_report_html(analysis, quote_count=len(kwargs["file_names"]))

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(main, "UPLOAD_DIR", Path(temp_dir)),
                patch.object(main, "classify_quotes", return_value=refrigerant_classification()),
                patch.object(
                    main.client.beta.chat.completions,
                    "parse",
                    return_value=parsed_completion(raw_refrigerant_analysis()),
                ),
                patch.object(main, "send_review_email", side_effect=capture_email),
            ):
                client = TestClient(main.app)
                response = client.post(
                    "/upload",
                    data={
                        "package": "basic",
                        "customer_name": "Regression Customer",
                        "customer_email": "customer@example.com",
                    },
                    files={
                        "files": (
                            FIXTURE_PATH.name,
                            self.quote_text.encode("utf-8"),
                            "text/plain",
                        )
                    },
                )

        self.assertEqual(response.status_code, 200)
        report = response.text
        report_lower = html.unescape(report).lower()
        self.assertIn("contractor attributes", report_lower)
        self.assertNotIn("leading to a diagnosis", report_lower)
        self.assertNotIn("does not include a leak search", report_lower)
        self.assertNotIn("absence of a leak search", report_lower)
        self.assertIn("underlying cause was evaluated", report_lower)

        questions = question_items(report)
        self.assertEqual(sum("verif" in q.lower() for q in questions), 1)
        self.assertEqual(sum("itemized breakdown" in q.lower() for q in questions), 1)
        self.assertLess(
            next(i for i, q in enumerate(questions) if "cause of the low charge" in q.lower()),
            next(i for i, q in enumerate(questions) if "verif" in q.lower()),
        )
        self.assertLess(
            next(i for i, q in enumerate(questions) if "verif" in q.lower()),
            next(i for i, q in enumerate(questions) if "itemized breakdown" in q.lower()),
        )
        self.assertEqual(captured["html"], report)
        self.assertEqual(
            captured["analysis"].contractor_questions,
            questions,
        )

    def test_analyze_json_email_and_html_share_finalized_values(self):
        captured = {}

        def capture_email(**kwargs):
            analysis = kwargs["analysis"]
            captured["analysis"] = analysis.model_copy(deep=True)
            captured["html"] = build_report_html(analysis, quote_count=len(kwargs["file_names"]))

        with (
            patch.object(main, "classify_quotes", return_value=refrigerant_classification()),
            patch.object(
                main.client.beta.chat.completions,
                "parse",
                return_value=parsed_completion(raw_refrigerant_analysis()),
            ),
            patch.object(main, "send_review_email", side_effect=capture_email),
        ):
            client = TestClient(main.app)
            response = client.post(
                "/analyze",
                json={
                    "packageName": "tier1",
                    "customerName": "Regression Customer",
                    "customerEmail": "customer@example.com",
                    "files": [
                        {
                            "fileName": FIXTURE_PATH.name,
                            "extractedText": self.quote_text,
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        finalized_payload = captured["analysis"].model_dump(mode="json")
        for field in (
            "project_overview",
            "missing_information",
            "installation_concerns",
            "red_flags",
            "good_signs",
            "contractor_questions",
            "recommendation",
            "decision",
        ):
            with self.subTest(field=field):
                self.assertEqual(payload[field], finalized_payload[field])

        for question in payload["contractor_questions"]:
            self.assertIn(html.escape(question), captured["html"])


if __name__ == "__main__":
    unittest.main()
