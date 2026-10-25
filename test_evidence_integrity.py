"""Phase 1: hostile/omitted AI output through real offline production finalization.

Only external AI/email I/O is mocked. Extraction, routing, support, finalization
and rendering are the same paths used by /analyze and /upload.
"""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
import electrical_controls as ec
from evidence_integrity import SourceEvidence, ground_assessments, material_work, recovery_modules
from test_repair_diagnostics import upload
from test_technical_support_derivation import analysis_with, assessment


BLOWER = "Replace blower motor — $1,250.\nBlower does not run.\nMotor: $800. Labor: $450."
TRANSFORMER = ("Replace control transformer — $650.\nPrimary rated 120 VAC\n"
               "Primary measured 120 VAC\nSecondary rated 24 VAC\nSecondary measured 0 VAC")


def classification():
    return main.QuoteClassification(quote_type="unknown", system_type="unknown",
                                    primary_scope="Submitted work", modules_required=[])


def final(raw, source):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw, source, 1, classification(), fresh_analysis=True)


def analyze(raw, source):
    responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=v))])
                 for v in (classification(), raw)]
    with contextlib.redirect_stdout(io.StringIO()), patch.object(
            main.client.beta.chat.completions, "parse", side_effect=responses), patch.object(
            main, "send_review_email") as email:
        result = TestClient(main.app).post("/analyze", json={"packageName": "tier1", "files": [
            {"fileName": "integrity.txt", "extractedText": source}]})
    assert result.status_code == 200, result.text
    return result.json(), email.call_args.kwargs["analysis"]


class EvidenceIntegrityTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_incomplete(self, result):
        self.assertEqual(result.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(result.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(result.analysis_integrity.status, "INCOMPLETE")
        self.assertEqual(result.red_flags, [])
        self.assertIn("could not fully validate", result.homeowner_takeaway)

    def test_missing_material_motor_assessment_upload(self):
        raw = analysis_with([], ai_support="SUPPORTED")
        result, report = upload(raw, BLOWER, classification())
        self.assert_incomplete(result)
        self.assertEqual(result.technical_assessments, [])
        self.assertEqual(result.analysis_integrity.coverage_gaps, ["blower motor"])
        self.assertEqual(len(result.contractor_questions), 1)
        self.assertEqual(report, main.build_report_html(result, 1))

    def test_minor_only_cannot_cover_material_motor(self):
        raw = analysis_with([assessment("CONFIRMED", materiality="MINOR", subject="Warranty")], ai_support="SUPPORTED")
        self.assert_incomplete(final(raw, BLOWER))

    def test_unrelated_assessment_cannot_cover_material_motor(self):
        raw = analysis_with([assessment("CONFIRMED", subject="Thermostat operation", evidence=["Thermostat responds normally."])])
        self.assert_incomplete(final(raw, BLOWER + "\nThermostat responds normally."))

    def test_replacement_basis_is_required(self):
        raw = analysis_with([assessment("ADEQUATE", subject="Equipment matching", evidence=["AHRI lists a matched combination."])])
        result = final(raw, "Replace the furnace. AHRI lists a matched combination.\nEquipment $4,000. Labor $2,000.")
        self.assertIn("replacement basis", result.analysis_integrity.coverage_gaps)
        self.assertNotEqual(result.decision.verdict, "PROCEED")

    def test_source_free_legacy_cannot_bypass_fresh_endpoint(self):
        raw = analysis_with([], ai_support="SUPPORTED")
        with contextlib.redirect_stdout(io.StringIO()):
            legacy = main.finalize_customer_analysis(raw)
        self.assertEqual(legacy.analysis_integrity.status, "LEGACY_UNVALIDATED")
        self.assertEqual(legacy.decision.technical_support, "SUPPORTED")
        self.assert_incomplete(final(raw, ""))

    def test_ai_cannot_supply_complete_integrity_marker(self):
        raw = analysis_with([], ai_support="SUPPORTED")
        raw.analysis_integrity.status = "COMPLETE"
        payload, emailed = analyze(raw, BLOWER)
        self.assert_incomplete(emailed)
        self.assertEqual(payload, emailed.model_dump(mode="json"))

    def test_omitted_existing_repair_modules_are_restored(self):
        cases = {"blower motor": "motors", "condenser fan motor": "motors", "compressor": "compressor",
                 "capacitor": "electrical_controls", "contactor": "electrical_controls",
                 "control transformer": "electrical_controls", "control board": "electrical_controls",
                 "heat exchanger": "heat_exchanger", "evaporator coil": "refrigerant_system",
                 "furnace": "repair_vs_replace", "AC": "repair_vs_replace",
                 "heat pump": "repair_vs_replace", "air handler": "repair_vs_replace"}
        for component, module in cases.items():
            with self.subTest(component=component):
                response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=classification()))])
                with patch.object(main.client.beta.chat.completions, "parse", return_value=response), contextlib.redirect_stdout(io.StringIO()):
                    result = main.classify_quotes(f"Replace {component}. Findings attached.")
                self.assertIn(main.AnalysisModule(module), result.modules_required)

    def test_recovered_capacitor_support_is_not_blocked(self):
        text = Path("electrical_controls_capacitor_good_test.txt").read_text()
        result = final(analysis_with([]), text)
        self.assertEqual(result.decision.technical_support, "SUPPORTED")
        self.assertEqual(result.analysis_integrity.status, "COMPLETE")

    def injected(self, subject, evidence):
        raw = analysis_with([assessment("CONFIRMED", subject=subject, evidence=[evidence])], ai_support="SUPPORTED")
        raw.good_signs = [evidence]
        raw.equipment_analysis = evidence
        raw.project_overview = evidence
        raw.decision.verdict_reasons = [evidence]
        raw.repair_evidence_sections = [main.RepairEvidenceSection(owner="fake", title="Fake", paragraphs=(evidence,))]
        return raw

    def test_invented_compressor_reading_removed_from_api_email_html(self):
        evidence = "Compressor insulation resistance measured 0.01 megohms directly at isolated compressor terminals."
        raw = self.injected("Claimed compressor failure", evidence)
        before = raw.model_dump()
        source = "Replace compressor. Compressor hums on start.\nCompressor/materials $3,000. Labor $1,950. Total $4,950."
        payload, emailed = analyze(raw, source)
        self.assertNotEqual(emailed.decision.technical_support, "SUPPORTED")
        self.assertNotIn("0.01", json.dumps(payload))
        self.assertNotIn("0.01", main.build_report_html(emailed, 1))
        self.assertEqual(payload, emailed.model_dump(mode="json"))
        self.assertEqual(before, raw.model_dump())

    def test_invented_motor_measurement_removed_from_upload(self):
        for evidence in ("Blower motor measured 237 volts at the motor terminals.",
                         "Blower motor winding resistance measured 3.78 ohms.",
                         "Blower motor current measured 17.32 amps."):
            with self.subTest(evidence=evidence):
                raw = self.injected("Blower motor failure", evidence)
                result, report = upload(raw, BLOWER, classification())
                self.assertNotEqual(result.decision.technical_support, "SUPPORTED")
                self.assertNotIn(evidence, result.model_dump_json())
                self.assertEqual(result.good_signs, [])
                self.assertNotIn(evidence, report)
                self.assertEqual(report, main.build_report_html(result, 1))

    def test_cross_component_evidence_cannot_support_another_owner(self):
        for owner, other, fact in (
                ("blower motor", "compressor", "Compressor winding tested open."),
                ("contactor", "transformer", "Transformer secondary measured 24 VAC."),
                ("control board", "refrigerant", "Refrigerant suction pressure measured 102 PSIG.")):
            with self.subTest(owner=owner):
                raw = analysis_with([assessment("CONFIRMED", subject=owner + " failure", evidence=[fact])])
                ground_assessments(raw, SourceEvidence(f"Replace {owner}.\n" + fact))
                self.assertEqual(raw.technical_assessments[0].documented_evidence, [])
                self.assertNotEqual(raw.technical_assessments[0].diagnostic_evidence_status, "CONFIRMED")

    def test_equivalent_units_preserve_source_evidence(self):
        for text in ("24V", "24 volts", "24 VAC", "24.0 VAC"):
            source = SourceEvidence("Replace control transformer.\nTransformer secondary measured 24 VAC.")
            self.assertTrue(source.grounded(f"Transformer secondary measured {text}.", "transformer"))

    def test_absence_and_numeric_zero_equivalence(self):
        for text in ("Transformer secondary measured no output.", "Transformer secondary measured 0 VAC output."):
            self.assertTrue(SourceEvidence("Transformer secondary measured 0 VAC.").grounded(text, "transformer"))

    def test_numeric_transformer_remains_supported_through_production(self):
        raw = analysis_with([assessment("CONFIRMED", subject=ec.PREFIX + "transformer",
            evidence=["Primary measured 120 VAC", "Secondary measured 0 VAC"])], pricing="LIMITED")
        result, report = upload(raw, TRANSFORMER, classification())
        self.assertEqual(result.decision.technical_support, "SUPPORTED")
        self.assertEqual(result.red_flags, [])
        self.assertEqual(len(result.contractor_questions), 1)
        self.assertEqual(main.contractor_question_category(result.contractor_questions[0]), "pricing")
        self.assertEqual(report, main.build_report_html(result, 1))

    def test_transformer_numeric_field_note_variants(self):
        for primary in ("Primary measured 120 VAC", "120V present at primary", "Input: 120 volts"):
            for secondary in ("Secondary measured 0 VAC", "0V on secondary", "Secondary output zero", "24 VAC secondary absent"):
                with self.subTest(primary=primary, secondary=secondary):
                    source = f"Replace control transformer.\n{primary}\n{secondary}"
                    self.assertEqual(final(analysis_with([]), source).decision.technical_support, "SUPPORTED")

    def test_missing_secondary_test_is_not_zero(self):
        for absent in ("Secondary not measured.", "Secondary voltage was never tested.",
                       "Secondary output unknown.", "Secondary output was not zero."):
            with self.subTest(absent=absent):
                source = "Replace control transformer.\nPrimary measured 120 VAC.\n" + absent
                self.assertNotEqual(final(analysis_with([]), source).decision.technical_support, "SUPPORTED")

    def test_negated_input_and_planned_measurements_cannot_support(self):
        for statement in ("Primary voltage not present.", "Primary voltage never measured.", "Will measure primary voltage at 120 VAC."):
            source = "Replace transformer.\n" + statement + "\nSecondary measured 0 VAC."
            self.assertNotEqual(final(analysis_with([]), source).decision.technical_support, "SUPPORTED")

    def test_source_backed_richer_compressor_finding_survives(self):
        evidence = "Manufacturer bulletin K identifies the compressor's measured winding pattern as a failure condition."
        raw = analysis_with([assessment("CONFIRMED", subject="Claimed compressor failure", evidence=[evidence])])
        result = final(raw, "Replace compressor.\n" + evidence)
        self.assertEqual(result.decision.technical_support, "SUPPORTED")
        self.assertIn(evidence, result.technical_assessments[0].documented_evidence)

    def test_source_backed_electrical_physical_failure_survives_parser_miss(self):
        evidence = "Manufacturer rejection criterion identifies the transformer insulation rupture photographed at the winding."
        raw = analysis_with([assessment("CONFIRMED", subject=ec.PREFIX + "transformer", evidence=[evidence])])
        result = final(raw, "Replace transformer.\n" + evidence)
        self.assertEqual(result.decision.technical_support, "SUPPORTED")

    def test_rendering_does_not_restore_unverified_raw_snapshot(self):
        raw = self.injected("Blower motor failure", "Blower motor measured 999 volts.")
        result = final(raw, BLOWER)
        before = result.model_dump()
        self.assertNotIn("999", main.build_report_html(result, 1))
        self.assertEqual(result.model_dump(), before)

    def test_same_number_wrong_unit_is_not_provenance(self):
        source = SourceEvidence("Replace transformer. Transformer primary measured 120 VAC.")
        self.assertFalse(source.grounded("Transformer primary measured 120 amps.", "transformer"))

    def test_primary_reading_cannot_supply_secondary_measurement(self):
        source = SourceEvidence("Transformer primary measured 120 VAC and secondary measured 0 VAC.")
        self.assertFalse(source.grounded("Transformer secondary measured 120 VAC.", "transformer"))

    def test_unmeasured_zero_is_not_a_measured_zero(self):
        source = SourceEvidence("Transformer secondary not measured; a 0 VAC reading is only an example.")
        self.assertFalse(source.grounded("Transformer secondary measured 0 VAC.", "transformer"))

    def test_real_source_conflict_is_not_erased_by_valid_other_evidence(self):
        source = Path("compressor_good_test.txt").read_text()
        conflict = "The proposed replacement is a different compressor from the one tested."
        raw = analysis_with([assessment("CONTRADICTORY", "UNSUPPORTED", subject="Claimed compressor failure", contradictions=[conflict])])
        result = final(raw, source + "\n" + conflict)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertIn(conflict, result.technical_assessments[0].contradictions)

    def test_omitted_replacement_basis_restored_to_prompt(self):
        c = classification()
        before = c.model_dump()
        with contextlib.redirect_stdout(io.StringIO()):
            knowledge = main.get_analysis_knowledge(c, "Replace furnace. Existing furnace condition review attached.")
        self.assertIn(main.ANALYSIS_MODULES[main.AnalysisModule.REPAIR_VS_REPLACE], knowledge)
        self.assertEqual(c.model_dump(), before)

    def test_declined_work_and_routine_verification_do_not_expand_coverage(self):
        text = "Replace control board.\nVerify blower operation after repair.\nNo compressor replacement proposed."
        self.assertEqual(material_work(text), {"control board"})

    def test_source_backed_motor_paraphrase_survives_evidence_card(self):
        source = "Replace blower motor.\nBlower motor power input measured 240 VAC.\nBlower motor winding tested open."
        raw = analysis_with([assessment("CONFIRMED", subject="Blower motor failure", evidence=[
            "Blower motor power input measured 240.0 volts.", "Blower motor winding tested open."])])
        result, report = upload(raw, source, classification())
        self.assertEqual(result.decision.technical_support, "SUPPORTED")
        self.assertIn("240.0 volts", report)
        self.assertEqual(result.contractor_questions, [])

    def test_invented_reading_in_gaps_questions_and_pricing_prose_is_removed(self):
        fake = "Blower motor current measured 999 amps."
        raw = self.injected("Blower motor failure", fake)
        raw.technical_assessments[0].material_gaps = [fake]
        raw.contractor_questions = ["Why did testing show blower motor current measured 999 amps?"]
        raw.pricing_review = fake
        raw.installation_concerns = fake
        raw.decision.required_actions = [fake]
        result = final(raw, BLOWER)
        self.assertNotIn("999", result.model_dump_json())

    def test_actual_email_renderer_uses_same_final_object(self):
        result, upload_html = upload(self.injected("Blower motor failure", "Blower motor measured 999 volts."),
                                     BLOWER, classification())
        with patch.dict("os.environ", {"EMAIL_APP_PASSWORD": "offline-placeholder"}), patch.object(main.smtplib, "SMTP_SSL") as smtp:
            main.send_review_email("Offline", "offline@example.com", "tier1", ["integrity.txt"], result)
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        email_html = message.get_body(preferencelist=("html",)).get_content()
        self.assertEqual(email_html.rstrip(), upload_html.rstrip())
        self.assertNotIn("999", email_html)

    def test_confirmed_empty_owned_assessment_is_not_meaningful_coverage(self):
        raw = analysis_with([assessment("CONFIRMED", subject="Blower motor failure")], ai_support="SUPPORTED")
        result = final(raw, BLOWER)
        self.assert_incomplete(result)

    def test_integrity_is_recomputed_on_each_fresh_finalization(self):
        raw = analysis_with([], ai_support="SUPPORTED")
        first = final(raw, BLOWER)
        again = final(first, BLOWER)
        self.assertEqual(first.model_dump(), again.model_dump())

    def test_condemnation_restores_existing_heat_exchanger_owner(self):
        for text in ("Furnace condemned because the heat exchanger is cracked.",
                     "Technician diagnosed a failed heat exchanger; furnace replacement proposed."):
            self.assertIn("heat_exchanger", recovery_modules(material_work(text)))

    def test_replacement_equipment_description_is_not_a_compressor_diagnosis(self):
        self.assertEqual(material_work("Replace full HVAC system with a scroll compressor."), {"replacement basis"})

    def test_transformer_ratings_are_not_readings(self):
        for text in ("Primary rated 120 VAC documented. Secondary measured 0 VAC.",
                     "Primary measured 120 VAC. Secondary rated zero VAC documented.",
                     "Rated primary 120 VAC. Primary measured 60 VAC. Secondary measured 0 VAC."):
            with self.subTest(text=text):
                result = final(analysis_with([]), "Replace transformer.\n" + text)
                self.assertNotEqual(result.decision.technical_support, "SUPPORTED")


if __name__ == "__main__":
    unittest.main()
