"""Exact EEV live-response replay and bounded component/system ownership checks."""
import contextlib
import html
import io
from pathlib import Path
import unittest

import main
import commissioning as cs
import refrigerant_system as rs
from evidence_integrity import component_repair_work, material_work
from system_sizing import sizing_required
from test_mini_split_scope_routing import classification
from test_repair_diagnostics import upload
from test_technical_support_derivation import analysis_with, assessment


RESPONSE = Path(__file__).with_name("test_fixtures") / "mini_split_eev_live_response_20261108.json"
SOURCE = """Existing ductless mini-split.
System is not cooling properly.
Contractor recommends replacing the electronic expansion valve (EEV) only.
No valve-specific measurements, manufacturer diagnostic results, or component isolation findings documented.
Scope includes replacing the EEV and verifying operation afterward.
No new installation, complete system replacement, or capacity change proposed.
Total repair price: $850 lump sum, parts and labor included."""


def saved_response():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


def finalize(raw=None, source=SOURCE):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw or saved_response(), source, 1,
                                              classification(), fresh_analysis=True)


class MiniSplitEEVLiveTests(unittest.TestCase):
    def assert_component_boundary(self, result):
        self.assertFalse(cs.commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))
        self.assertNotIn("replacement basis", result.analysis_integrity.coverage_gaps)
        self.assertNotIn("How Will Startup Be Verified?", main.build_report_html(result, 1))

    def test_exact_eight_question_response_through_production_upload(self):
        raw = saved_response()
        original = raw.model_dump()
        self.assertEqual(len(raw.contractor_questions), 8)
        self.assertEqual(len(raw.red_flags), 2)
        self.assertIn("How Will Startup Be Verified?", main.build_report_html(raw, 1))
        result, report = upload(raw, SOURCE, classification())
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(len(result.red_flags), 1)
        self.assertEqual(result.good_signs, raw.good_signs)
        self.assertEqual(result.price_facts, raw.price_facts)
        self.assertEqual(len(result.contractor_questions), 2)
        self.assertIn("manufacturer", result.contractor_questions[0])
        self.assertEqual(result.contractor_questions[-1], raw.contractor_questions[-1])
        self.assert_component_boundary(result)
        self.assertEqual(report, main.build_report_html(result, 1))
        self.assertNotIn("replacement basis", result.model_dump_json())
        self.assertNotIn("startup checks", result.model_dump_json().lower())
        for question in result.contractor_questions:
            self.assertEqual(html.unescape(report).count(question), 1)
        self.assertEqual(raw.model_dump(), original)

    def test_unsupported_eev_keeps_absent_evidence_and_one_diagnosis(self):
        result = finalize()
        item = rs.refrigerant_items(result)[0]
        self.assertEqual(item.diagnostic_evidence_status, "ABSENT")
        self.assertEqual(item.scope_support, "UNSUPPORTED")
        self.assertEqual(len(result.red_flags), 1)
        self.assertEqual(len(result.contractor_questions), 2)

    def test_eev_and_lev_replacing_grammar_routes_as_component_work(self):
        for component in ("EEV", "LEV", "electronic expansion valve", "linear expansion valve"):
            with self.subTest(component=component):
                text = f"Existing mini-split. Contractor recommends replacing the {component} only. Verify operation after repair."
                self.assertEqual(component_repair_work(text), {"metering device"})
                self.assertEqual(material_work(text), {"metering device"})
                self.assertFalse(sizing_required(text))
                self.assertFalse(cs.commissioning_required(text))
                self.assertTrue(rs.refrigerant_required(text))
                self.assert_component_boundary(finalize(source=text))

    def test_negated_system_work_does_not_create_replacement_basis(self):
        self.assertEqual(material_work(SOURCE), {"metering device"})
        self.assertFalse(cs.commissioning_required(SOURCE))
        self.assertFalse(sizing_required(SOURCE))
        self.assert_component_boundary(finalize())

    def test_component_mentions_or_declined_repair_do_not_establish_scope(self):
        for text in ("Existing mini-split contains an EEV.", "No EEV replacement is proposed.",
                     "Not replacing the EEV.", "EEV replacement if needed."):
            with self.subTest(text=text):
                self.assertFalse(component_repair_work(text))

    def test_source_grounded_valid_eev_evidence_is_preserved(self):
        evidence = ["The attached manufacturer diagnostic procedure was performed on the EEV.",
                    "The EEV failed to respond to the commanded valve movement during the isolation test.",
                    "The documented isolation checks ruled out the control signal and sensor input as the cause."]
        raw = analysis_with([assessment("CONFIRMED", "APPROPRIATE", subject=rs.METERING,
                                       evidence=evidence)], pricing="LIMITED")
        text = "Replace the EEV. Verify operation after repair.\n" + "\n".join(evidence)
        result = finalize(raw, text)
        self.assertEqual(result.decision.technical_support, "SUPPORTED")
        self.assertEqual(rs.refrigerant_items(result)[0].documented_evidence, evidence)
        self.assertFalse(result.red_flags)
        self.assertFalse(any(main.repair_diagnostic_question_owner(q) == "metering_evidence"
                             for q in result.contractor_questions))
        self.assert_component_boundary(result)

    def test_manufacturer_test_name_or_promised_test_is_not_completed_evidence(self):
        for text in ("Manufacturer EEV test.", "The contractor will perform the EEV isolation test."):
            with self.subTest(text=text):
                result = finalize(source=SOURCE + "\n" + text)
                self.assertEqual(result.decision.technical_support, "UNSUPPORTED")

    def test_planned_verification_is_positive_and_needs_no_procedure_question(self):
        result = finalize()
        self.assertEqual(result.good_signs, saved_response().good_signs)
        self.assertNotIn("unclear what specific tests", result.installation_concerns)
        self.assertNotIn("detail the verification steps", " ".join(result.decision.required_actions))
        self.assertFalse(any(main.contractor_question_category(q) in
                             {"verification", "commissioning", "refrigerant_verification"}
                             for q in result.contractor_questions))

    def test_genuinely_missing_verification_keeps_a_followup(self):
        raw = saved_response()
        result = finalize(raw, SOURCE.replace("Scope includes replacing the EEV and verifying operation afterward.\n", ""))
        self.assertIn("How will operation be verified after the repair?", result.contractor_questions)
        self.assertEqual(len(result.contractor_questions), 3)
        self.assertNotIn(raw.good_signs[0], result.good_signs)
        self.assert_component_boundary(result)

    def test_actual_verification_conflict_survives(self):
        raw = saved_response()
        conflict = "Operation was not verified after the repair."
        question = "Why was operation not verified after the repair?"
        raw.technical_assessments.append(assessment("CONTRADICTORY", "UNSUPPORTED",
            materiality="MATERIAL_SECONDARY", subject="EEV repair closeout",
            evidence=[conflict], contradictions=[conflict]))
        raw.contractor_questions.insert(0, question)
        result = finalize(raw, SOURCE + "\n" + conflict)
        self.assertIn(question, result.contractor_questions)
        self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))

    def test_separate_refrigerant_concern_and_question_survive(self):
        raw = saved_response()
        conflict = "The documented service-valve leak remains unrepaired."
        raw.technical_assessments.append(assessment("CONTRADICTORY", "UNSUPPORTED",
            materiality="MATERIAL_SECONDARY", subject=rs.CAUSE,
            evidence=[conflict], contradictions=[conflict]))
        result = finalize(raw, SOURCE + "\n" + conflict)
        self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))
        self.assertIn(conflict, " ".join(result.red_flags))
        self.assertTrue(any("leak" in q.lower() for q in result.contractor_questions))
        self.assertTrue(any("EEV" in q for q in result.contractor_questions))

    def test_new_single_and_multi_zone_installations_keep_commissioning(self):
        for text in ("Install a new single-zone mini-split.", "Install a new multi-zone mini-split."):
            with self.subTest(text=text):
                result = finalize(analysis_with([]), text)
                self.assertTrue(main.sizing_assessments(result))
                self.assertTrue(cs.commissioning_items(result))
                self.assertIn("How Will Startup Be Verified?", main.build_report_html(result, 1))

    def test_complete_replacement_keeps_system_review(self):
        for text in ("Replace the complete mini-split system.", "Contractor recommends replacing the complete mini-split system."):
            with self.subTest(text=text):
                result = finalize(analysis_with([]), text)
                self.assertTrue(main.sizing_assessments(result))
                self.assertTrue(cs.commissioning_items(result))

    def test_mixed_component_and_system_work_keeps_commissioning(self):
        for text in ("Replace EEV and install a new mini-split.",
                     "Contractor recommends replacing the EEV and installing a new mini-split.",
                     SOURCE + "\nInstall a new multi-zone mini-split for the addition."):
            with self.subTest(text=text):
                result = finalize(source=text)
                self.assertTrue(main.sizing_assessments(result))
                self.assertTrue(cs.commissioning_items(result))

    def test_eev_guidance_is_equipment_specific_not_universal_charge_readings(self):
        result = finalize()
        guidance = " ".join(p for section in result.repair_evidence_sections for p in section.paragraphs)
        self.assertIn("manufacturer", guidance)
        self.assertIn("isolation", guidance)
        for concern in ("charge", "airflow", "control", "sensor"):
            self.assertIn(concern, guidance)
        self.assertNotIn("superheat/subcooling findings", guidance)
        self.assertNotIn("pressure, temperature", guidance)

    def test_unrelated_eev_mention_does_not_change_txv_guidance(self):
        item = assessment("ABSENT", "UNSUPPORTED", subject=rs.METERING)
        review = rs.refrigerant_evidence_review(item, "Replace the TXV. Another system contains an EEV.")
        self.assertEqual(review.definition, rs.REFRIGERANT_EVIDENCE[rs.METERING])

    def test_component_part_compatibility_question_and_gap_are_not_system_matching(self):
        raw = saved_response()
        gap = "Compatibility of the replacement EEV part is not documented."
        question = "Which EEV part specification confirms compatibility?"
        raw.missing_information += " " + gap
        raw.contractor_questions.insert(0, question)
        result = finalize(raw, SOURCE + "\n" + gap)
        self.assertIn(gap, result.missing_information)
        self.assertIn(question, result.contractor_questions)
        self.assert_component_boundary(result)

    def test_diagnostic_aliases_deduplicate_without_losing_price(self):
        raw = saved_response()
        raw.contractor_questions += raw.contractor_questions[:] + [
            "What EEV testing establishes valve failure?",
            "What findings isolate the electronic expansion valve as the cause?"]
        result = finalize(raw)
        self.assertEqual(len(result.contractor_questions), 2)
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]), "pricing")

    def test_final_cap_applies_after_late_owner_and_integrity_questions(self):
        # Independent repair owners add diagnostic questions after the early
        # category pass; coverage gaps can append more. Exercise the final model.
        names = ["Blower motor failure", "Inducer motor failure", "Condenser fan motor failure",
                 "Gas valve failure", "Heat exchanger failure", rs.METERING]
        raw = analysis_with([assessment("ABSENT", "UNSUPPORTED", subject=name) for name in names], pricing="LIMITED")
        text = "Replace blower motor. Replace inducer motor. Replace condenser fan motor. Replace gas valve. Replace heat exchanger. Replace EEV. Total: $850."
        result = finalize(raw, text)
        self.assertLessEqual(len(result.contractor_questions), 6)
        self.assertEqual(len(result.contractor_questions), len(set(result.contractor_questions)))
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]), "pricing")

    def test_repeated_finalization_and_json_html_parity(self):
        result = finalize()
        for _ in range(3):
            again = finalize(main.HVACAnalysis.model_validate_json(result.model_dump_json()))
            self.assertEqual(again.model_dump(), result.model_dump())
            self.assertEqual(main.build_report_html(again, 1), main.build_report_html(result, 1))
            self.assertEqual(len(again.contractor_questions), 2)
            result = again


if __name__ == "__main__":
    unittest.main()
