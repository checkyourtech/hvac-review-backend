import unittest
from pathlib import Path

from pydantic import ValidationError

from main import (
    ANALYSIS_MODULES,
    AnalysisModule,
    QuoteClassification,
    SECTION_QUALITY_RULES,
    get_analysis_knowledge,
)


def classification_for(*modules):
    return QuoteClassification(
        quote_type="repair",
        system_type="unknown",
        primary_scope="Routing test",
        modules_required=list(modules),
    )


class AnalysisModuleRoutingTests(unittest.TestCase):
    def test_registry_exactly_matches_canonical_module_type(self):
        self.assertEqual(set(ANALYSIS_MODULES), set(AnalysisModule))

    def test_every_allowed_module_resolves_independently(self):
        for module in AnalysisModule:
            with self.subTest(module=module.value):
                knowledge = get_analysis_knowledge(classification_for(module))
                self.assertTrue(knowledge.strip())

    def test_structured_classification_rejects_unknown_module(self):
        with self.assertRaises(ValidationError):
            classification_for("heat_exchanger")

    def test_invalid_constructed_module_fails_loudly(self):
        invalid = QuoteClassification.model_construct(
            quote_type="repair",
            system_type="unknown",
            primary_scope="Invalid routing test",
            repair_components=[],
            replacement_components=[],
            proposed_equipment=[],
            diagnostic_evidence=[],
            missing_information=[],
            modules_required=["not_a_module"],
            confidence="moderate",
        )

        with self.assertRaisesRegex(ValueError, "Unknown or unregistered"):
            get_analysis_knowledge(invalid)

    def test_capacitor_fixture_receives_electrical_controls_knowledge(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.ELECTRICAL_CONTROLS)
        )
        self.assertIn("measured capacitance", knowledge)
        self.assertIn("CONTROL BOARD REPAIRS", knowledge)

    def test_blower_and_condenser_fan_fixtures_receive_motors_knowledge(self):
        for fixture in (
            "electrical_blower_good_test.txt",
            "electrical_condenser_fan_good_test.txt",
        ):
            with self.subTest(fixture=fixture):
                knowledge = get_analysis_knowledge(
                    classification_for(AnalysisModule.MOTORS)
                )
                self.assertIn("BLOWER MOTOR AND ECM REPAIRS", knowledge)
                self.assertIn("motor failure", knowledge)

    def test_furnace_fixtures_receive_combustion_knowledge(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.FURNACE_COMBUSTION)
        )
        self.assertIn("INDUCER MOTOR / COMBUSTION DRAFT REPAIRS", knowledge)
        self.assertIn("PRESSURE SWITCH / DRAFT PROVING REPAIRS", knowledge)
        self.assertIn("HOT SURFACE IGNITER / IGNITION REPAIRS", knowledge)
        self.assertIn("FLAME SENSOR / FLAME PROVING REPAIRS", knowledge)

    def test_refrigerant_fixture_does_not_require_electrical_controls(self):
        classification = classification_for(AnalysisModule.REFRIGERANT_SYSTEM)
        knowledge = get_analysis_knowledge(classification)
        self.assertEqual(
            classification.modules_required,
            [AnalysisModule.REFRIGERANT_SYSTEM],
        )
        self.assertIn("REFRIGERANT SYSTEM AND COIL REPAIR", knowledge)
        self.assertIn("METERING DEVICE / TXV / PISTON REPAIRS", knowledge)

    def test_airflow_fixture_does_not_require_electrical_controls(self):
        classification = classification_for(AnalysisModule.DUCT_AIRFLOW)
        knowledge = get_analysis_knowledge(classification)
        self.assertEqual(
            classification.modules_required,
            [AnalysisModule.DUCT_AIRFLOW],
        )
        self.assertIn("AIRFLOW / STATIC PRESSURE DIAGNOSTIC REVIEW", knowledge)
        self.assertIn("DUCTWORK", knowledge)

    def test_cross_cutting_modules_resolve_independently(self):
        warranty = get_analysis_knowledge(
            classification_for(AnalysisModule.WARRANTY)
        )
        commissioning = get_analysis_knowledge(
            classification_for(AnalysisModule.COMMISSIONING)
        )
        self.assertIn("WARRANTY", warranty)
        self.assertIn("PRESSURE TESTING", commissioning)
        self.assertIn("EVACUATION", commissioning)

    def test_cross_cutting_blocks_are_not_nested_in_domain_modules(self):
        compressor = ANALYSIS_MODULES[AnalysisModule.COMPRESSOR]
        refrigerant = ANALYSIS_MODULES[AnalysisModule.REFRIGERANT_SYSTEM]

        self.assertNotIn("WARRANTY\n\nFor a compressor repair", compressor)
        self.assertNotIn("REPAIR VS REPLACEMENT", compressor)
        self.assertNotIn(
            "WARRANTY\n\nFor coil and refrigerant-system repairs",
            refrigerant,
        )
        self.assertNotIn("PRICING AND TRANSPARENCY", refrigerant)

    def test_customer_prompt_modules_exclude_known_corruption(self):
        combined = "\n".join(ANALYSIS_MODULES.values())
        for artifact in (
            "incoming and outgoing r motor",
            "draft-rvoltage",
            "IGNITER_REPAIR_LOGIC =",
            'EMAIL_APP_PASSWORD="" python',
        ):
            with self.subTest(artifact=artifact):
                self.assertNotIn(artifact, combined)

    def test_section_quality_rules_are_always_on(self):
        knowledge = get_analysis_knowledge(classification_for())

        self.assertIn(SECTION_QUALITY_RULES.strip(), knowledge)
        for field_name in (
            "good_signs",
            "red_flags",
            "installation_concerns",
            "missing_information",
            "pricing_review",
        ):
            self.assertIn(field_name, knowledge)
        self.assertIn("GENERIC SCOPE QUALITY", knowledge)

    def test_electrical_fixture_restores_positive_evidence_guidance(self):
        quote_text = Path("electrical_test.txt").read_text(encoding="utf-8")
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.ELECTRICAL_CONTROLS),
            quote_text,
        )

        for expected in (
            "measured capacitance compared with rated capacitance",
            "visibly burned or pitted contacts",
            "documented voltage checks",
            "post-repair operational verification",
            "PRICING REVIEW (pricing_review)",
            "GENERIC SCOPE QUALITY",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, knowledge)

        for unrelated_header in (
            "REFRIGERANT SYSTEM AND COIL REPAIR ANALYSIS RULES",
            "INDUCER MOTOR / COMBUSTION DRAFT REPAIRS",
            "AIRFLOW / STATIC PRESSURE DIAGNOSTIC REVIEW",
        ):
            with self.subTest(unrelated_header=unrelated_header):
                self.assertNotIn(unrelated_header, knowledge)

    def test_low_charge_fixture_prioritizes_diagnostic_and_cause_evidence(self):
        quote_text = Path("refrigerant_low_charge_bad_test.txt").read_text(
            encoding="utf-8"
        )
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.REFRIGERANT_SYSTEM),
            quote_text,
        )

        for expected in (
            "Proof of low refrigerant charge is not the same as proof of a refrigerant leak",
            "whether airflow was reasonably considered",
            "whether the proposal explains why charge is low",
            "whether recharge-only work addresses the unresolved cause",
            "how final charge or system operation will be verified",
            "PRICING AND TRANSPARENCY",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, knowledge)

        self.assertNotIn("ELECTRICAL POSITIVE EVIDENCE", knowledge)
        self.assertIn(
            "Do not use generic praise, brand reputation, or manufactured positives",
            knowledge,
        )

    def test_refrigerant_calibration_keeps_symptoms_evidence_neutral(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.REFRIGERANT_SYSTEM)
        )

        self.assertIn("symptoms, not diagnostic evidence", knowledge)
        self.assertIn("attribute the proposed diagnosis to the contractor", knowledge)
        self.assertIn("Do not say the symptom indicates or proves the diagnosis", knowledge)

    def test_refrigerant_calibration_does_not_make_leak_search_universal(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.REFRIGERANT_SYSTEM)
        )

        self.assertIn("Do not describe a leak search", knowledge)
        self.assertIn("universally mandatory or categorically critical", knowledge)
        self.assertIn("whether leak investigation was performed or recommended when appropriate", knowledge)
        self.assertNotIn("A leak search is a critical required step", knowledge)

    def test_refrigerant_calibration_deduplicates_related_red_flag_guidance(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.REFRIGERANT_SYSTEM)
        )

        self.assertIn(
            "combine insufficient evidence establishing low charge and unresolved cause",
            knowledge,
        )

    def test_refrigerant_consistency_keeps_missing_information_evidence_neutral(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.REFRIGERANT_SYSTEM)
        )

        self.assertIn(
            'Do not reduce the missing-information explanation to "leak search results."',
            knowledge,
        )
        self.assertIn(
            "whether leak investigation was performed or recommended when appropriate",
            knowledge,
        )
        self.assertNotIn("leak search is essential", knowledge.lower())

    def test_refrigerant_consistency_does_not_praise_unresolved_low_charge(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.REFRIGERANT_SYSTEM)
        )

        self.assertIn(
            "do not list the low-refrigerant diagnosis itself as a good_sign",
            knowledge,
        )
        self.assertIn(
            "Credit only independently documented favorable facts",
            knowledge,
        )
        self.assertIn("Pricing transparency must remain outside red_flags", knowledge)
        self.assertIn(
            "Do not automatically characterize an absent leak search as a critical installation failure",
            knowledge,
        )

    def test_pricing_guidance_is_injected_for_price_without_pricing_route(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.ELECTRICAL_CONTROLS),
            "Replace capacitor. Total repair price: $725",
        )

        self.assertIn("PRICING AND TRANSPARENCY", knowledge)
        self.assertEqual(knowledge.count("PRICING AND TRANSPARENCY"), 1)

    def test_selected_pricing_guidance_is_not_duplicated(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.PRICING),
            "Quoted total: $950",
        )

        self.assertEqual(knowledge.count("PRICING AND TRANSPARENCY"), 1)

    def test_warranty_guidance_is_always_available_and_materiality_gated(self):
        knowledge = get_analysis_knowledge(classification_for())

        self.assertIn("WARRANTY REVIEW", knowledge)
        self.assertIn("proportional to the scope and value", knowledge)
        self.assertIn(
            "Do not penalize a routine minor repair merely because warranty language is absent",
            knowledge,
        )

    def test_selected_warranty_guidance_is_not_duplicated(self):
        knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.WARRANTY)
        )

        self.assertEqual(knowledge.count("WARRANTY REVIEW"), 1)

    def test_detailed_commissioning_remains_conditional(self):
        generic_knowledge = get_analysis_knowledge(classification_for())
        commissioning_knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.COMMISSIONING)
        )

        self.assertNotIn("PRESSURE TESTING", generic_knowledge)
        self.assertNotIn("EVACUATION", generic_knowledge)
        self.assertIn("PRESSURE TESTING", commissioning_knowledge)
        self.assertIn("EVACUATION", commissioning_knowledge)

    def test_repair_vs_replace_remains_conditional(self):
        routine_repair_knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.ELECTRICAL_CONTROLS)
        )
        major_repair_knowledge = get_analysis_knowledge(
            classification_for(AnalysisModule.REPAIR_VS_REPLACE)
        )

        self.assertNotIn("REPAIR VS REPLACEMENT", routine_repair_knowledge)
        self.assertIn("REPAIR VS REPLACEMENT", major_repair_knowledge)


if __name__ == "__main__":
    unittest.main()
