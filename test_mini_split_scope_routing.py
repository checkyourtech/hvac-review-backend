"""Checkpoint 1: scope/ownership only; no manufacturer or verdict fixtures."""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from commissioning import commissioning_required, commissioning_items, COMMISSIONING_SUBJECT
from compressor import compressor_required
from electrical_controls import electrical_required
from evidence_integrity import material_work, recovery_modules, assessment_owner, coverage_gaps
from refrigerant_system import refrigerant_required, refrigerant_items, subject_kind, METERING, VERIFICATION
from system_sizing import sizing_required, SIZING_SUBJECT
from test_technical_support_derivation import analysis_with, assessment


def classification(**kwargs):
    return main.QuoteClassification(quote_type="repair", system_type="mini split",
                                    primary_scope="Submitted work", modules_required=[], **kwargs)


def classify(text, raw=None):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=raw or classification()))])
    with patch.object(main.client.beta.chat.completions, "parse", return_value=response):
        return main.classify_quotes(text)


def finalize(text, items=(), raw_classification=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(analysis_with(list(items)), text, 1,
                                              raw_classification or classification(), fresh_analysis=True)


class MiniSplitScopeRoutingTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_repair_scope(self, text, owner, module):
        self.assertEqual(material_work(text), {owner})
        self.assertEqual(recovery_modules(material_work(text)), {module})
        self.assertFalse(sizing_required(text))
        self.assertFalse(commissioning_required(text))
        self.assertFalse(compressor_required(text))
        self.assertEqual(electrical_required(text), module == "electrical_controls")
        self.assertEqual(refrigerant_required(text), module == "refrigerant_system")
        self.assertEqual({m.value for m in classify(text).modules_required}, {module})
        result = finalize(text)
        owners = {assessment_owner(a.subject) for a in result.technical_assessments}
        self.assertLessEqual(owners, {owner})
        self.assertFalse(commissioning_items(result))
        self.assertFalse(any(a.diagnostic_evidence_status in {"CONFIRMED", "ADEQUATE"}
                             for a in result.technical_assessments))

    def assert_system_scope(self, text):
        self.assertTrue(sizing_required(text))
        self.assertTrue(commissioning_required(text))
        routed = classify(text)
        self.assertIn(main.AnalysisModule.SYSTEM_SIZING, routed.modules_required)
        self.assertIn(main.AnalysisModule.COMMISSIONING, routed.modules_required)
        result = finalize(text)
        self.assertTrue(main.sizing_assessments(result))
        self.assertTrue(commissioning_items(result))

    def test_thermistor_only_repair(self):
        for text in ("Replace mini-split thermistor.", "Replace indoor thermistor.",
                     "Replace mini-split indoor thermistor.", "Replace temperature sensor."):
            with self.subTest(text=text):
                self.assert_repair_scope(text, "temperature sensor", "electrical_controls")

    def test_eev_only_repair(self):
        for text in ("Replace mini-split EEV.", "Replace EEV.", "Replace electronic expansion valve."):
            with self.subTest(text=text):
                self.assert_repair_scope(text, "metering device", "refrigerant_system")

    def test_lev_only_repair(self):
        for text in ("Replace mini-split LEV.", "Replace LEV.", "Replace linear expansion valve."):
            with self.subTest(text=text):
                self.assert_repair_scope(text, "metering device", "refrigerant_system")

    def test_inverter_pcb_only_repair(self):
        for text in ("Replace inverter PCB.", "Replace inverter drive board.",
                     "Replace mini-split inverter control board."):
            with self.subTest(text=text):
                self.assert_repair_scope(text, "control board", "electrical_controls")

    def test_communication_control_repair(self):
        for text in ("Repair communication-control wiring.", "Repair communicating control wiring.",
                     "Replace mini-split communication module."):
            with self.subTest(text=text):
                self.assert_repair_scope(text, "communication control", "electrical_controls")

    def test_conventional_control_board_repair(self):
        self.assert_repair_scope("Replace control board.", "control board", "electrical_controls")

    def test_new_single_zone_installation(self):
        for text in ("Install a new single-zone mini-split.",
                     "Install a new single-zone mini-split with inverter control board and thermistor."):
            with self.subTest(text=text):
                self.assert_system_scope(text)
                self.assertFalse(electrical_required(text))

    def test_new_multi_zone_installation(self):
        self.assert_system_scope("Install a new multi-zone mini-split with electronic expansion valves.")
        self.assertTrue(sizing_required("Multi-zone equipment selection for the addition."))

    def test_complete_mini_split_replacement(self):
        self.assert_system_scope("Replace the complete mini-split system.")

    def test_capacity_changing_partial_retrofit(self):
        text = "Install an additional indoor head on the existing mini-split. Increase system capacity for the addition."
        self.assert_system_scope(text)

    def test_partial_outdoor_unit_replacement(self):
        self.assert_system_scope("Replace mini-split outdoor unit; retain existing indoor heads.")

    def test_mixed_component_and_system_work(self):
        for text in ("Replace EEV; install a new mini-split.",
                     "Replace inverter drive board and install a new mini-split.",
                     "Replace mini-split and repair communication-control wiring."):
            with self.subTest(text=text):
                self.assert_system_scope(text)
                self.assertIn("replacement basis", material_work(text))

    def test_component_mentions_without_repair_scope(self):
        for text in ("Existing mini-split contains a thermistor and EEV.",
                     "Installed equipment includes LEV and inverter PCB.",
                     "The communication-control wiring is shown in the diagram."):
            with self.subTest(text=text):
                self.assertFalse(electrical_required(text))
                self.assertFalse(refrigerant_required(text))
                self.assertEqual(material_work(text), set())
                self.assertFalse(any(a.diagnostic_evidence_status in {"CONFIRMED", "ADEQUATE"}
                                     for a in finalize(text).technical_assessments))

    def test_declined_or_conditional_repairs_do_not_route(self):
        for text in ("Do not replace EEV.", "Thermistor replacement is not proposed.",
                     "Replace inverter PCB if needed.", "Homeowner declined thermistor replacement."):
            with self.subTest(text=text):
                self.assertFalse(electrical_required(text))
                self.assertFalse(refrigerant_required(text))
                self.assertEqual(material_work(text), set())

    def test_healthy_component_mention_does_not_add_independent_work(self):
        text = "Replace EEV and check thermistor. Thermistor tested normal."
        self.assertEqual(material_work(text), {"metering device"})
        self.assertFalse(electrical_required(text))
        self.assertTrue(refrigerant_required(text))

    def test_existing_measurements_without_proposed_repair_are_not_failure(self):
        text = "Indoor thermistor measured within supplied criteria. EEV response tested normal."
        self.assertEqual(material_work(text), set())
        self.assertFalse(electrical_required(text))
        self.assertFalse(refrigerant_required(text))

    def test_classifier_system_scope_does_not_override_component_source(self):
        for text in ("Replace mini-split thermistor.", "Replace mini-split EEV.",
                     "Replace inverter drive board.", "Repair communication-control wiring."):
            raw = main.QuoteClassification(quote_type="replacement", system_type="mini split",
                primary_scope="Replace system", replacement_components=["mini split"], modules_required=[
                    main.AnalysisModule.SYSTEM_SIZING, main.AnalysisModule.COMMISSIONING,
                    main.AnalysisModule.EQUIPMENT_MATCHING, main.AnalysisModule.REPAIR_VS_REPLACE])
            before = raw.model_dump()
            with self.subTest(text=text):
                routed = classify(text, raw)
                self.assertFalse(set(routed.modules_required) & {
                    main.AnalysisModule.SYSTEM_SIZING, main.AnalysisModule.COMMISSIONING,
                    main.AnalysisModule.EQUIPMENT_MATCHING, main.AnalysisModule.REPAIR_VS_REPLACE})
                self.assertEqual(raw.model_dump(), before)

    def test_knowledge_routing_preserves_input_classification(self):
        raw = classification()
        raw.modules_required = [main.AnalysisModule.SYSTEM_SIZING, main.AnalysisModule.COMMISSIONING,
                                main.AnalysisModule.EQUIPMENT_MATCHING]
        before = raw.model_dump()
        with contextlib.redirect_stdout(io.StringIO()):
            knowledge = main.get_analysis_knowledge(raw, "Replace mini-split LEV.")
        for module in raw.modules_required:
            self.assertNotIn(main.ANALYSIS_MODULES[module].strip(), knowledge)
        self.assertIn(main.ANALYSIS_MODULES[main.AnalysisModule.REFRIGERANT_SYSTEM].strip(), knowledge)
        self.assertEqual(raw.model_dump(), before)

    def test_raw_new_system_assessments_removed_for_component_repair(self):
        items = [assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=subject)
                 for subject in (SIZING_SUBJECT, COMMISSIONING_SUBJECT,
                                 "Quoted indoor/outdoor equipment compatibility", "Basis for full-system replacement")]
        result = finalize("Replace mini-split thermistor.", items)
        self.assertFalse(result.technical_assessments)
        self.assertEqual(result.analysis_integrity.coverage_gaps, ["temperature sensor"])

    def test_independent_multi_component_repairs_preserved(self):
        text = "Replace thermistor; replace EEV; replace inverter PCB."
        self.assertEqual(material_work(text), {"temperature sensor", "metering device", "control board"})
        self.assertEqual({m.value for m in classify(text).modules_required}, {"electrical_controls", "refrigerant_system"})
        self.assertFalse(sizing_required(text))
        self.assertFalse(commissioning_required(text))

    def test_independent_compressor_and_refrigerant_work_preserved(self):
        text = "Replace thermistor. Replace compressor. Add refrigerant after the documented circuit repair."
        routed = classify(text)
        for module in (main.AnalysisModule.ELECTRICAL_CONTROLS, main.AnalysisModule.COMPRESSOR,
                       main.AnalysisModule.REFRIGERANT_SYSTEM):
            self.assertIn(module, routed.modules_required)
        self.assertFalse(sizing_required(text))

    def test_independent_major_repair_economics_preserved(self):
        text = "Replace thermistor. Replace compressor. Compare compressor repair versus replacement."
        raw = classification()
        raw.modules_required = [main.AnalysisModule.REPAIR_VS_REPLACE]
        self.assertIn(main.AnalysisModule.REPAIR_VS_REPLACE, classify(text, raw).modules_required)

    def test_capacity_review_is_not_new_system_commissioning(self):
        text = "Replace mini-split EEV. Review system capacity for the remodel."
        self.assertTrue(sizing_required(text))
        self.assertFalse(commissioning_required(text))

    def test_failed_required_repair_verification_preserved(self):
        conflict = "Required safety verification failed after thermistor repair."
        item = assessment("CONTRADICTORY", "UNSUPPORTED", subject=COMMISSIONING_SUBJECT,
                          evidence=[conflict], contradictions=[conflict])
        result = finalize("Replace mini-split thermistor. " + conflict, [item])
        self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))

    def test_eev_repair_does_not_create_charge_diagnosis(self):
        result = finalize("Replace mini-split EEV.")
        self.assertEqual([subject_kind(a.subject) for a in refrigerant_items(result)], [METERING])
        self.assertFalse(any("low charge" in a.subject.lower() for a in result.technical_assessments))

    def test_repair_verification_is_preserved(self):
        evidence = "Final refrigerant charge was verified using the supplied procedure after EEV repair."
        item = assessment("CONFIRMED", subject=VERIFICATION, evidence=[evidence])
        result = finalize("Replace EEV.\n" + evidence, [item])
        retained = next(a for a in result.technical_assessments if a.subject == VERIFICATION)
        self.assertEqual(retained.documented_evidence, [evidence])
        self.assertEqual(retained.diagnostic_evidence_status, "CONFIRMED")
        self.assertFalse(commissioning_items(result))

    def test_structured_evidence_status_is_not_inferred_from_alias(self):
        for component in ("thermistor", "EEV", "LEV", "inverter PCB", "communication module"):
            for status, scope, evidence in (
                ("ABSENT", "UNSUPPORTED", []),
                ("INCOMPLETE", "PARTIALLY_DEFINED", [f"{component} measured; isolation remains incomplete."]),
                ("CONFIRMED", "APPROPRIATE", [f"{component} isolated and tested against supplied criteria; failure confirmed."]),
            ):
                with self.subTest(component=component, status=status):
                    item = assessment(status, scope, subject=component + " diagnosis", evidence=evidence)
                    result = finalize("Replace " + component + ".\n" + "\n".join(evidence), [item])
                    owner = assessment_owner(item.subject)
                    retained = next(a for a in result.technical_assessments if assessment_owner(a.subject) == owner)
                    self.assertEqual(retained.diagnostic_evidence_status, status)
                    self.assertEqual(retained.scope_support, scope)

    def test_unrelated_evidence_cannot_cover_missing_sensor_work(self):
        result = analysis_with([assessment("CONFIRMED", subject="Control board diagnosis",
                                          evidence=["Control board tested normally."])])
        self.assertEqual(coverage_gaps(result, material_work("Replace thermistor.")), ["temperature sensor"])

    def test_multiple_quotes_do_not_suppress_system_work(self):
        text = "QUOTE 1\nReplace mini-split thermistor.\nQUOTE 2\nInstall a new multi-zone mini-split."
        self.assert_system_scope(text)


if __name__ == "__main__":
    unittest.main()
