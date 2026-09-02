import html
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
    GLOBAL_ANALYSIS_RULES,
    QuoteClassification,
    TechnicalEvidenceAssessment,
    build_report_html,
    derive_technical_support,
    finalize_customer_analysis,
)


def assessment(
    status="ADEQUATE",
    scope="APPROPRIATE",
    materiality="PRIMARY",
    subject="Proposed work",
    evidence=None,
    gaps=None,
    contradictions=None,
):
    return TechnicalEvidenceAssessment(
        subject=subject,
        materiality=materiality,
        diagnostic_evidence_status=status,
        scope_support=scope,
        documented_evidence=evidence or [],
        material_gaps=gaps or [],
        contradictions=contradictions or [],
    )


def analysis_with(
    assessments,
    ai_support="PARTIALLY_SUPPORTED",
    pricing="ADEQUATE",
    required_actions=None,
    optional_suggestions=None,
):
    return HVACAnalysis(
        project_overview="Test proposal",
        equipment_analysis="Technical evidence was reviewed.",
        missing_information="No material technical information is missing.",
        pricing_review="Pricing was reviewed separately.",
        installation_concerns="No material scope concern was identified.",
        quote_comparison="",
        best_quote_recommendation="",
        contractor_vetting="",
        red_flags=[],
        good_signs=[],
        contractor_questions=[],
        recommendation="Raw AI recommendation",
        decision=HVACDecision(
            verdict="REVIEW_BEFORE_APPROVING",
            technical_support=ai_support,
            pricing_transparency=pricing,
            required_actions=required_actions or [],
            optional_suggestions=optional_suggestions or [],
            verdict_reasons=[],
        ),
        technical_assessments=assessments,
    )


class TechnicalSupportDerivationTests(unittest.TestCase):
    def test_strong_measurements_and_appropriate_scope_are_supported(self):
        self.assertEqual(
            derive_technical_support([assessment("CONFIRMED")]),
            "SUPPORTED",
        )

    def test_incomplete_isolation_is_partially_supported(self):
        self.assertEqual(
            derive_technical_support([assessment("INCOMPLETE")]),
            "PARTIALLY_SUPPORTED",
        )

    def test_symptom_or_assertion_only_is_unsupported(self):
        self.assertEqual(
            derive_technical_support([assessment("ABSENT")]),
            "UNSUPPORTED",
        )

    def test_contradictory_primary_evidence_is_unsupported(self):
        self.assertEqual(
            derive_technical_support([assessment("CONTRADICTORY")]),
            "UNSUPPORTED",
        )

    def test_partially_defined_primary_scope_is_partial(self):
        self.assertEqual(
            derive_technical_support([assessment(scope="PARTIALLY_DEFINED")]),
            "PARTIALLY_SUPPORTED",
        )

    def test_supported_primary_and_unsupported_secondary_is_partial(self):
        self.assertEqual(
            derive_technical_support(
                [
                    assessment("CONFIRMED", subject="Primary repair"),
                    assessment(
                        "ABSENT",
                        "UNSUPPORTED",
                        "MATERIAL_SECONDARY",
                        "Separable accessory",
                    ),
                ]
            ),
            "PARTIALLY_SUPPORTED",
        )

    def test_unsupported_primary_is_not_rescued_by_supported_minor_work(self):
        self.assertEqual(
            derive_technical_support(
                [
                    assessment("ABSENT", subject="Primary compressor"),
                    assessment(
                        "CONFIRMED",
                        materiality="MINOR",
                        subject="Minor contactor",
                    ),
                ]
            ),
            "UNSUPPORTED",
        )

    def test_minor_unsupported_item_does_not_reduce_support(self):
        self.assertEqual(
            derive_technical_support(
                [
                    assessment("ADEQUATE"),
                    assessment(
                        "ABSENT",
                        "UNSUPPORTED",
                        "MINOR",
                        "Nonmaterial detail",
                    ),
                ]
            ),
            "SUPPORTED",
        )

    def test_pricing_optional_suggestions_and_warranty_do_not_affect_derivation(self):
        technical_facts = [assessment("ADEQUATE")]
        for pricing in ("ADEQUATE", "LIMITED", "ABSENT"):
            with self.subTest(pricing=pricing):
                finalized = finalize_customer_analysis(
                    analysis_with(
                        technical_facts,
                        ai_support="UNSUPPORTED",
                        pricing=pricing,
                        optional_suggestions=[
                            "Consider asking about warranty coverage and price."
                        ],
                    )
                )
                self.assertEqual(
                    finalized.decision.technical_support,
                    "SUPPORTED",
                )

    def test_missing_part_number_alone_does_not_reduce_support(self):
        item = assessment(
            "ADEQUATE",
            evidence=["The failed component was isolated by documented testing."],
            gaps=["Exact replacement part number is not listed."],
        )
        self.assertEqual(derive_technical_support([item]), "SUPPORTED")

    def test_legacy_empty_assessments_preserve_ai_support(self):
        raw = analysis_with([], ai_support="PARTIALLY_SUPPORTED")
        finalized = finalize_customer_analysis(raw)
        self.assertEqual(
            finalized.decision.technical_support,
            "PARTIALLY_SUPPORTED",
        )

    def test_finalization_overrides_ai_support_both_directions(self):
        absent = finalize_customer_analysis(
            analysis_with([assessment("ABSENT")], ai_support="PARTIALLY_SUPPORTED")
        )
        supported = finalize_customer_analysis(
            analysis_with([assessment("CONFIRMED")], ai_support="UNSUPPORTED")
        )

        self.assertEqual(absent.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(absent.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(supported.decision.technical_support, "SUPPORTED")
        self.assertEqual(supported.decision.verdict, "PROCEED")

    def test_finalization_is_idempotent_and_preserves_assessments(self):
        raw = analysis_with(
            [assessment("INCOMPLETE")],
            ai_support="SUPPORTED",
            pricing="LIMITED",
        )
        once = finalize_customer_analysis(raw)
        twice = finalize_customer_analysis(once)

        self.assertEqual(once.model_dump(), twice.model_dump())
        self.assertEqual(
            once.technical_assessments,
            raw.technical_assessments,
        )


class FixtureCalibrationTests(unittest.TestCase):
    CASES = {
        "electrical_test.txt": [assessment("CONFIRMED")],
        "refrigerant_low_charge_bad_test.txt": [assessment("ABSENT")],
        "refrigerant_low_charge_good_test.txt": [assessment("CONFIRMED")],
        "electrical_condenser_fan_good_test.txt": [assessment("CONFIRMED")],
        "electrical_condenser_fan_bad_test.txt": [
            assessment("INCOMPLETE", "PARTIALLY_DEFINED")
        ],
        "electrical_igniter_good_test.txt": [assessment("CONFIRMED")],
        "electrical_igniter_bad_test.txt": [
            assessment("ABSENT", "UNSUPPORTED")
        ],
        "electrical_pressure_switch_good_test.txt": [assessment("CONFIRMED")],
        "electrical_pressure_switch_bad_test.txt": [
            assessment("ABSENT", "UNSUPPORTED")
        ],
        "electrical_flame_sensor_good_test.txt": [assessment("CONFIRMED")],
        "electrical_flame_sensor_bad_test.txt": [
            assessment("INCOMPLETE", "PARTIALLY_DEFINED")
        ],
        "repair_replace_sizing_good_test.txt": [assessment("ADEQUATE")],
        "repair_replace_sizing_bad_test.txt": [
            assessment("ABSENT", "UNSUPPORTED")
        ],
        "combustion_heat_exchanger_good_test.txt": [
            assessment(
                "CONFIRMED",
                "APPROPRIATE",
                subject="Heat-exchanger failure and furnace replacement",
            )
        ],
        "combustion_heat_exchanger_partial_test.txt": [
            assessment(
                "INCOMPLETE",
                "PARTIALLY_DEFINED",
                subject="Suspected heat-exchanger failure and furnace replacement",
            )
        ],
        "combustion_heat_exchanger_bad_test.txt": [
            assessment(
                "ABSENT",
                "UNSUPPORTED",
                subject="Heat-exchanger condemnation and furnace replacement",
            )
        ],
    }
    EXPECTED = {
        "electrical_test.txt": "SUPPORTED",
        "refrigerant_low_charge_bad_test.txt": "UNSUPPORTED",
        "refrigerant_low_charge_good_test.txt": "SUPPORTED",
        "electrical_condenser_fan_good_test.txt": "SUPPORTED",
        "electrical_condenser_fan_bad_test.txt": "PARTIALLY_SUPPORTED",
        "electrical_igniter_good_test.txt": "SUPPORTED",
        "electrical_igniter_bad_test.txt": "UNSUPPORTED",
        "electrical_pressure_switch_good_test.txt": "SUPPORTED",
        "electrical_pressure_switch_bad_test.txt": "UNSUPPORTED",
        "electrical_flame_sensor_good_test.txt": "SUPPORTED",
        "electrical_flame_sensor_bad_test.txt": "PARTIALLY_SUPPORTED",
        "repair_replace_sizing_good_test.txt": "SUPPORTED",
        "repair_replace_sizing_bad_test.txt": "UNSUPPORTED",
        "combustion_heat_exchanger_good_test.txt": "SUPPORTED",
        "combustion_heat_exchanger_partial_test.txt": "PARTIALLY_SUPPORTED",
        "combustion_heat_exchanger_bad_test.txt": "UNSUPPORTED",
    }

    def test_fixture_calibration_matrix(self):
        for fixture, assessments in self.CASES.items():
            with self.subTest(fixture=fixture):
                self.assertTrue(Path(fixture).read_text(encoding="utf-8").strip())
                self.assertEqual(
                    derive_technical_support(assessments),
                    self.EXPECTED[fixture],
                )

    def test_incomplete_component_isolation_uses_partially_defined_scope(self):
        for fixture in (
            "electrical_condenser_fan_bad_test.txt",
            "electrical_flame_sensor_bad_test.txt",
        ):
            with self.subTest(fixture=fixture):
                item = self.CASES[fixture][0]
                self.assertEqual(item.diagnostic_evidence_status, "INCOMPLETE")
                self.assertEqual(item.scope_support, "PARTIALLY_DEFINED")
                self.assertEqual(
                    derive_technical_support([item]),
                    "PARTIALLY_SUPPORTED",
                )

    def test_scope_prompt_reserves_unsupported_for_unjustified_work(self):
        self.assertIn(
            "INCOMPLETE diagnostic evidence should normally pair with "
            "PARTIALLY_DEFINED scope",
            GLOBAL_ANALYSIS_RULES,
        )
        self.assertIn(
            "Do not classify scope as UNSUPPORTED merely because additional "
            "diagnostic testing is needed",
            GLOBAL_ANALYSIS_RULES,
        )
        self.assertIn(
            "classify the motor evidence as INCOMPLETE and the plausible replacement "
            "scope as PARTIALLY_DEFINED, not UNSUPPORTED",
            GLOBAL_ANALYSIS_RULES,
        )

    def test_heat_exchanger_fixture_assessment_calibration(self):
        expected = {
            "combustion_heat_exchanger_good_test.txt": (
                "CONFIRMED",
                "APPROPRIATE",
                "SUPPORTED",
            ),
            "combustion_heat_exchanger_partial_test.txt": (
                "INCOMPLETE",
                "PARTIALLY_DEFINED",
                "PARTIALLY_SUPPORTED",
            ),
            "combustion_heat_exchanger_bad_test.txt": (
                "ABSENT",
                "UNSUPPORTED",
                "UNSUPPORTED",
            ),
        }

        for fixture, (evidence_status, scope_status, support) in expected.items():
            with self.subTest(fixture=fixture):
                item = self.CASES[fixture][0]
                self.assertEqual(item.materiality, "PRIMARY")
                self.assertEqual(item.diagnostic_evidence_status, evidence_status)
                self.assertEqual(item.scope_support, scope_status)
                self.assertEqual(derive_technical_support([item]), support)


class TechnicalSupportEndToEndTests(unittest.TestCase):
    def test_analyze_email_and_html_use_derived_support(self):
        raw = analysis_with(
            [assessment("ABSENT", subject="Low refrigerant diagnosis")],
            ai_support="PARTIALLY_SUPPORTED",
        )
        classification = QuoteClassification(
            quote_type="repair",
            system_type="split air conditioner",
            primary_scope="Add refrigerant",
            repair_components=["refrigerant recharge"],
            modules_required=[AnalysisModule.REFRIGERANT_SYSTEM],
        )
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=raw))]
        )
        captured = {}

        def capture_email(**kwargs):
            captured["analysis"] = kwargs["analysis"].model_copy(deep=True)
            captured["html"] = build_report_html(kwargs["analysis"], quote_count=1)

        with (
            patch.object(main, "classify_quotes", return_value=classification),
            patch.object(
                main.client.beta.chat.completions,
                "parse",
                return_value=completion,
            ),
            patch.object(main, "send_review_email", side_effect=capture_email),
        ):
            response = TestClient(main.app).post(
                "/analyze",
                json={
                    "packageName": "tier1",
                    "customerName": "Technical Support Test",
                    "files": [
                        {
                            "fileName": "refrigerant_low_charge_bad_test.txt",
                            "extractedText": Path(
                                "refrigerant_low_charge_bad_test.txt"
                            ).read_text(encoding="utf-8"),
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["decision"]["technical_support"],
            "UNSUPPORTED",
        )
        self.assertEqual(
            captured["analysis"].decision.technical_support,
            "UNSUPPORTED",
        )
        self.assertIn("Unsupported", html.unescape(captured["html"]))
        self.assertIn("GET A SECOND OPINION", html.unescape(captured["html"]))


if __name__ == "__main__":
    unittest.main()
