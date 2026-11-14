"""Checkpoint 3 synthetic installation evidence; models/values are fictional inputs.

No OEM database, live model calls, or fixture-name decision rules. The structured
response is supplied just as in the accepted offline domain tests; production
scope, provenance, normalization, policy, and rendering run without bypasses.
"""
import contextlib
import html
import io
import unittest

import main
import commissioning as cs
import refrigerant_system as rs
from evidence_integrity import assessment_owner
from system_sizing import SIZING_SUBJECT
from test_system_sizing import analysis, sizing
from test_mini_split_scope_routing import classify, classification
from test_repair_diagnostics import upload

MATCH = "Quoted indoor/outdoor equipment compatibility"
BASIS = "The homeowner requests new heating and cooling for the studio."
COMBINATION = "Submitted manufacturer combination document lists outdoor SYN-O24 and indoor SYN-I18 together as an approved combination."
LOADS = "Studio cooling load: 12,000 Btu/h; heating load: 16,000 Btu/h."
CAPACITY = "Selected cooling capacity: 14,000 Btu/h; heating output: 18,000 Btu/h at the submitted heating design conditions. The selection meets these studio loads."
STARTUP = "Follow manufacturer startup procedure and document final operating readings. Verify heating and cooling operation, controls and safeties."
INSTALL = "Installation scope includes new refrigerant piping sized for the listed equipment, pressure testing and evacuation under the submitted manufacturer procedure, applicable charge adjustment, electrical connections and communication wiring, and condensate drainage."
SCOPE = "Install a new single-zone ductless mini-split for the studio."
MULTI = "Install a new multi-zone mini-split for the east and west rooms."
MULTI_MATCH = "Submitted manufacturer combination document approves outdoor SYN-O24 with east head SYN-I18 and west head SYN-I12, two connected zones and their individual capacities. The listed indoor nominal total is 30,000 Btu/h; the outdoor nominal capacity is 24,000 Btu/h."
MULTI_LOADS = "East zone cooling load: 10,000 Btu/h; west zone cooling load: 7,000 Btu/h; combined heating load: 20,000 Btu/h."
MULTI_CAPACITY = "Submitted selection provides east zone cooling capacity of 12,000 Btu/h and west zone cooling capacity of 9,000 Btu/h simultaneously; available outdoor cooling output is 21,000 Btu/h at the submitted design conditions. Heating output is 22,000 Btu/h at the heating design conditions. The selection meets the zone loads."


def item(subject, evidence=(), status="ADEQUATE", scope="APPROPRIATE", gaps=(), conflicts=()):
    return main.TechnicalEvidenceAssessment(subject=subject, materiality="PRIMARY",
        diagnostic_evidence_status=status, scope_support=scope,
        documented_evidence=list(evidence), material_gaps=list(gaps), contradictions=list(conflicts))


def case(scope=SCOPE, match=COMBINATION, loads=LOADS, capacity=CAPACITY, startup=STARTUP, extra=""):
    source = "\n".join(filter(None, (scope, BASIS, match, loads, capacity, INSTALL, startup, extra)))
    raw = analysis(item("Basis for full-system replacement", [BASIS]),
                   item(MATCH, [match]) if match else item(MATCH, status="INCOMPLETE", scope="PARTIALLY_DEFINED", gaps=["Manufacturer combination documentation is missing."]),
                   item(SIZING_SUBJECT, [loads, capacity]) if loads and capacity else item(SIZING_SUBJECT, status="INCOMPLETE", scope="PARTIALLY_DEFINED", gaps=["Zone load and capacity selection evidence is missing."]))
    raw.equipment_analysis = "The proposal identifies the installation scope."
    raw.installation_concerns = "The proposal describes the installation work."
    return raw, source


def finalize(raw, source):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw, source, 1, classification(), fresh_analysis=True)


def owned(result, subject):
    return next(a for a in result.technical_assessments if a.subject == subject)


class MiniSplitInstallationTests(unittest.TestCase):
    def test_single_zone_installation_restores_matching_knowledge_and_assessment(self):
        routed = classify(SCOPE)
        self.assertIn(main.AnalysisModule.EQUIPMENT_MATCHING, routed.modules_required)
        with contextlib.redirect_stdout(io.StringIO()):
            knowledge = main.get_analysis_knowledge(classification(), SCOPE)
        self.assertIn(main.EQUIPMENT_MATCHING_ANALYSIS_RULES.strip(), knowledge)
        raw, source = case(match="")
        raw.technical_assessments = [a for a in raw.technical_assessments if a.subject != MATCH]
        result = finalize(raw, source)
        matching = owned(result, MATCH)
        self.assertEqual((matching.diagnostic_evidence_status,matching.scope_support), ("INCOMPLETE","PARTIALLY_DEFINED"))
        self.assertFalse(result.red_flags)

    def test_new_multi_zone_with_approved_unequal_nominal_totals(self):
        raw, source = case(scope=MULTI, match=MULTI_MATCH, loads=MULTI_LOADS, capacity=MULTI_CAPACITY)
        result = finalize(raw, source)
        self.assertEqual(result.decision.technical_support,"SUPPORTED")
        self.assertEqual(owned(result,MATCH).documented_evidence,[MULTI_MATCH])
        self.assertFalse(result.red_flags)
        self.assertFalse(result.contractor_questions)

    def test_documented_correct_single_zone_specifications(self):
        raw, source = case()
        result = finalize(raw, source)
        self.assertEqual(result.decision.technical_support,"SUPPORTED")
        self.assertEqual(owned(result,MATCH).scope_support,"APPROPRIATE")
        self.assertFalse(rs.refrigerant_items(result))
        self.assertFalse(any(assessment_owner(a.subject) in {"compressor","control board"} for a in result.technical_assessments))

    def test_actual_manufacturer_combination_conflict(self):
        conflict = "Submitted manufacturer document prohibits outdoor SYN-O24 with indoor SYN-I18; the proposal pairs those models."
        raw, source = case(match=conflict)
        raw.technical_assessments[1] = item(MATCH,[conflict],"CONTRADICTORY","UNSUPPORTED",conflicts=[conflict])
        raw.red_flags=[conflict]
        result=finalize(raw,source)
        self.assertEqual(owned(result,MATCH).contradictions,[conflict])
        self.assertEqual(result.decision.technical_support,"UNSUPPORTED")
        self.assertTrue(result.red_flags)

    def test_connected_zone_limit_is_a_real_conflict(self):
        conflict="Submitted manufacturer document allows two connected zones on SYN-O24, but the proposal connects three zones."
        raw,source=case(scope=MULTI,match=conflict)
        raw.technical_assessments[1]=item(MATCH,[conflict],"CONTRADICTORY","UNSUPPORTED",conflicts=[conflict])
        result=finalize(raw,source)
        self.assertEqual(owned(result,MATCH).contradictions,[conflict])
        self.assertEqual(result.decision.technical_support,"UNSUPPORTED")

    def test_documented_nominal_connection_limit_is_not_discarded(self):
        conflict = "Submitted manufacturer limit permits a summed indoor nominal capacity of 30,000 Btu/h on the outdoor unit, but the proposed total is 36,000 Btu/h."
        raw, source = case(scope=MULTI, match=conflict)
        raw.technical_assessments[1] = item(MATCH, [conflict], "CONTRADICTORY", "UNSUPPORTED", conflicts=[conflict])
        result = finalize(raw, source)
        self.assertEqual(owned(result, MATCH).contradictions, [conflict])
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")

    def test_unknown_matching_does_not_remove_independent_outdoor_concern(self):
        concern = "The proposed outdoor mounting bracket has documented structural damage."
        question = "How will the damaged outdoor mounting bracket be addressed?"
        raw, source = case(match="", extra=concern)
        raw.red_flags = [concern]
        raw.contractor_questions = [question]
        result = finalize(raw, source)
        self.assertIn(concern, result.red_flags)
        self.assertIn(question, result.contractor_questions)

    def test_unknown_match_is_partial_and_does_not_claim_identified_models_missing(self):
        models="Outdoor unit: SYN-O24. Indoor unit: SYN-I18. Manufacturer combination documentation is not supplied."
        for status in ("ABSENT","INCOMPLETE"):
            with self.subTest(status=status):
                raw,source=case(match=models)
                raw.technical_assessments[1]=item(MATCH,[models],status,"UNSUPPORTED",gaps=["Manufacturer combination documentation is missing."])
                raw.red_flags=["Equipment compatibility is unverified."]
                result=finalize(raw,source)
                self.assertEqual(owned(result,MATCH).scope_support,"PARTIALLY_DEFINED")
                self.assertFalse(result.red_flags)
                self.assertNotIn("models and documentation tying them together are not included",result.missing_information)
                self.assertNotIn("exact indoor equipment still needs to be identified",result.installation_concerns)

    def test_nominal_head_sum_alone_does_not_prove_incompatibility(self):
        nominal="The summed indoor nominal capacity is 30,000 Btu/h, which exceeds the outdoor nominal capacity of 24,000 Btu/h."
        raw,source=case(scope=MULTI,match=nominal)
        raw.technical_assessments[1]=item(MATCH,[nominal],"CONTRADICTORY","UNSUPPORTED",conflicts=[nominal])
        raw.red_flags=["The equipment combination is incompatible because the summed indoor capacities exceed outdoor nominal capacity."]
        result=finalize(raw,source)
        self.assertEqual(owned(result,MATCH).diagnostic_evidence_status,"INCOMPLETE")
        self.assertFalse(owned(result,MATCH).contradictions)
        self.assertFalse(result.red_flags)

    def test_adequate_zone_sizing_uses_design_output(self):
        raw,source=case(scope=MULTI,match=MULTI_MATCH,loads=MULTI_LOADS,capacity=MULTI_CAPACITY)
        result=finalize(raw,source)
        self.assertEqual(owned(result,SIZING_SUBJECT).scope_support,"APPROPRIATE")
        self.assertIn(MULTI_CAPACITY,owned(result,SIZING_SUBJECT).documented_evidence)

    def test_missing_sizing_is_partial_without_wrong_size_flag(self):
        raw,source=case(loads="",capacity="")
        result=finalize(raw,source)
        self.assertEqual(owned(result,SIZING_SUBJECT).scope_support,"PARTIALLY_DEFINED")
        self.assertFalse(result.red_flags)
        self.assertTrue(any(main.contractor_question_category(q)=="system_sizing" for q in result.contractor_questions))

    def test_nominal_mini_split_capacity_is_not_cold_weather_output(self):
        capacity="Selected mini-split nominal capacity: 18,000 Btu/h. The selection meets the heating load."
        raw,source=case(capacity=capacity)
        result=finalize(raw,source)
        self.assertEqual(owned(result,SIZING_SUBJECT).scope_support,"PARTIALLY_DEFINED")
        self.assertFalse(result.red_flags)

    def test_documented_cold_climate_shortfall_is_preserved(self):
        conflict="Submitted design heating load is 26,000 Btu/h, but supplied equipment heating output at the same design conditions is 18,000 Btu/h and no supplemental heat is included."
        raw,source=case(extra=conflict)
        raw.technical_assessments[2]=item(SIZING_SUBJECT,[conflict],"CONTRADICTORY","UNSUPPORTED",conflicts=[conflict])
        result=finalize(raw,source)
        self.assertEqual(owned(result,SIZING_SUBJECT).contradictions,[conflict])
        self.assertEqual(result.decision.technical_support,"UNSUPPORTED")

    def test_documented_supplemental_heat_can_support_selection(self):
        cap="Selected mini-split cooling capacity: 14,000 Btu/h; heating output at design conditions: 12,000 Btu/h. Supplemental heating output: 6,000 Btu/h is available concurrently under the submitted control plan. The selection covers the heating and cooling loads."
        raw,source=case(capacity=cap)
        result=finalize(raw,source)
        self.assertEqual(owned(result,SIZING_SUBJECT).scope_support,"APPROPRIATE")

    def test_retained_line_set_is_unknown_without_reuse_evidence(self):
        reuse="Retain and reuse existing line sets; pipe sizes and reuse suitability are not documented."
        raw,source=case(scope="Replace mini-split outdoor unit; retain existing indoor heads.",extra=reuse)
        result=finalize(raw,source)
        startup=owned(result,cs.COMMISSIONING_SUBJECT)
        self.assertEqual(startup.scope_support,"PARTIALLY_DEFINED")
        self.assertFalse(startup.contradictions)
        self.assertFalse(result.red_flags)
        self.assertTrue(any("line" in q.lower() and "reuse" in q.lower() for q in result.contractor_questions))
        self.assertFalse(rs.refrigerant_items(result))

    def test_documented_reuse_suitability_is_not_a_missing_detail(self):
        reuse="Submitted manufacturer installation instructions permit reuse of the identified existing line sets with the proposed refrigerant; documented pipe sizes, lengths, lift and condition meet those requirements."
        raw,source=case(extra=reuse)
        result=finalize(raw,source)
        self.assertEqual(owned(result,cs.COMMISSIONING_SUBJECT).scope_support,"APPROPRIATE")
        self.assertFalse(result.red_flags)
        self.assertFalse(result.contractor_questions)

    def test_reuse_gap_preserves_independent_documented_startup_positive(self):
        raw, source = case(extra="Retain and reuse existing line sets; pipe sizes and reuse suitability are not documented.")
        raw.good_signs = ["The proposed startup work includes documentation of the checks after installation."]
        result = finalize(raw, source)
        self.assertIn(raw.good_signs[0], result.good_signs)
        self.assertEqual(result.good_signs.count(raw.good_signs[0]), 1)
        self.assertEqual(owned(result, cs.COMMISSIONING_SUBJECT).scope_support, "PARTIALLY_DEFINED")
        self.assertEqual(result.model_dump(), finalize(result, source).model_dump())

    def test_actual_retrofit_conflict_is_not_erased_by_good_startup_plan(self):
        conflict="The submitted installation instructions prohibit reusing the existing line set, but the proposal retains it."
        raw,source=case(extra=conflict)
        raw.technical_assessments.append(item(cs.COMMISSIONING_SUBJECT,[conflict],"CONTRADICTORY","UNSUPPORTED",conflicts=[conflict]))
        result=finalize(raw,source)
        self.assertEqual(owned(result,cs.COMMISSIONING_SUBJECT).contradictions,[conflict])
        self.assertEqual(result.decision.technical_support,"UNSUPPORTED")
        self.assertTrue(any("line set" in flag for flag in result.red_flags))
        self.assertTrue(any("line set" in q for q in result.contractor_questions))

    def test_independent_installation_requirements_survive(self):
        for conflict in (
            "Submitted electrical instructions require a dedicated supply circuit, but the proposal explicitly retains a conflicting shared circuit.",
            "Submitted communication wiring instructions prohibit the proposed cable type.",
            "Submitted condensate drainage instructions require a pump for this route, but the proposal omits it.",
            "Submitted pressure testing instructions prohibit the proposed test procedure.",
        ):
            with self.subTest(conflict=conflict):
                raw,source=case(extra=conflict)
                raw.technical_assessments.append(item(cs.COMMISSIONING_SUBJECT,[conflict],"CONTRADICTORY","UNSUPPORTED",conflicts=[conflict]))
                result=finalize(raw,source)
                self.assertIn(conflict,owned(result,cs.COMMISSIONING_SUBJECT).contradictions)
                self.assertEqual(result.decision.technical_support,"UNSUPPORTED")

    def test_missing_installation_detail_is_partial_not_a_defect(self):
        gap="Condensate drainage route is not described."
        raw,source=case(extra=gap)
        raw.technical_assessments.append(item(cs.COMMISSIONING_SUBJECT,status="INCOMPLETE",scope="PARTIALLY_DEFINED",gaps=[gap]))
        result=finalize(raw,source)
        self.assertIn(gap,owned(result,cs.COMMISSIONING_SUBJECT).material_gaps)
        self.assertFalse(result.red_flags)

    def test_missing_commissioning_is_partial_without_invented_targets(self):
        raw,source=case(startup="")
        result=finalize(raw,source)
        self.assertEqual(owned(result,cs.COMMISSIONING_SUBJECT).scope_support,"PARTIALLY_DEFINED")
        self.assertFalse(result.red_flags)
        report=main.build_report_html(result,1)
        for invented in ("500 microns","300 psi","400 CFM/ton","24 volts"):
            self.assertNotIn(invented,report)

    def test_component_repairs_keep_installation_domains_out(self):
        from test_mini_split_scope_routing import finalize as repair_finalize
        for repair in ("Replace mini-split thermistor.","Replace mini-split EEV.","Replace mini-split LEV.","Replace inverter drive board."):
            with self.subTest(repair=repair):
                result=repair_finalize(repair)
                self.assertFalse(main.primary_equipment_matching_assessment(result))
                self.assertFalse(main.sizing_assessments(result))
                self.assertFalse(cs.commissioning_items(result))

    def test_mixed_repair_and_installation_preserves_both(self):
        repair="Replace mini-split thermistor on the existing system; no diagnostic tests documented."
        raw,source=case(extra=repair)
        raw.technical_assessments.append(item("Electrical control evidence: temperature sensor",status="ABSENT",scope="UNSUPPORTED",gaps=["Thermistor isolation findings are missing."]))
        result=finalize(raw,source)
        self.assertTrue(main.primary_equipment_matching_assessment(result))
        self.assertTrue(main.sizing_assessments(result))
        self.assertTrue(cs.commissioning_items(result))
        self.assertTrue(any(assessment_owner(a.subject)=="temperature sensor" for a in result.technical_assessments))

    def test_upload_html_json_parity_question_limit_and_pricing_independence(self):
        raw,source=case(match="",loads="",capacity="",startup="",extra="Total installation price: $9,000 lump sum.")
        raw.decision.pricing_transparency="LIMITED"
        result,report=upload(raw,source,classification())
        self.assertEqual(result.decision.pricing_transparency,"LIMITED")
        self.assertEqual(report,main.build_report_html(result,1))
        self.assertLessEqual(len(result.contractor_questions),6)
        self.assertEqual(len(result.contractor_questions),len(set(result.contractor_questions)))
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]),"pricing")
        for q in result.contractor_questions:
            self.assertEqual(html.unescape(report).count(q),1)

    def test_repeated_finalization(self):
        for extra in ("", "Retain and reuse existing line sets; pipe sizes and reuse suitability are not documented."):
            raw,source=case(match="",extra=extra)
            result=finalize(raw,source)
            again=finalize(result,source)
            self.assertEqual(result.model_dump(),again.model_dump())
            self.assertEqual(main.build_report_html(result,1),main.build_report_html(again,1))

    def test_new_installation_charge_allowance_is_not_a_refrigerant_diagnosis(self):
        raw, source = case(extra="Add refrigerant per manufacturer instructions for the installed line length.")
        result = finalize(raw, source)
        self.assertFalse(rs.refrigerant_items(result))
        self.assertNotIn(main.AnalysisModule.REFRIGERANT_SYSTEM, classify(source).modules_required)
        self.assertNotIn("refrigerant", result.analysis_integrity.coverage_gaps)

    def test_independent_refrigerant_repair_with_installation_is_preserved(self):
        repair = "Existing separate mini-split has low refrigerant charge; recharge is proposed without measurements."
        raw, source = case(extra=repair)
        result = finalize(raw, source)
        self.assertTrue(rs.refrigerant_items(result))
        self.assertIn(main.AnalysisModule.REFRIGERANT_SYSTEM, classify(source).modules_required)

    def test_ducted_mini_split_uses_existing_airflow_review(self):
        raw, source = case(scope="Install a new ducted mini-split using existing ducts.")
        result = finalize(raw, source)
        self.assertTrue(main.duct_items(result))
        self.assertFalse(any(a.scope_support == "UNSUPPORTED" for a in main.duct_items(result)))

    def test_retained_equipment_refrigerant_and_controls_conflicts(self):
        for conflict in (
            "Submitted manufacturer document states the retained indoor unit uses a refrigerant incompatible with the proposed outdoor unit.",
            "Submitted manufacturer document states the retained indoor controller cannot communicate with the proposed outdoor unit.",
        ):
            with self.subTest(conflict=conflict):
                raw, source = case(scope="Replace mini-split outdoor unit; retain existing indoor heads.", match=conflict)
                raw.technical_assessments[1] = item(MATCH, [conflict], "CONTRADICTORY", "UNSUPPORTED", conflicts=[conflict])
                result = finalize(raw, source)
                self.assertEqual(owned(result, MATCH).contradictions, [conflict])
                self.assertFalse(rs.refrigerant_items(result))

    def test_planned_reuse_verification_does_not_require_completed_readings(self):
        reuse = "Reuse existing line sets only after verifying their condition, sizes and suitability against manufacturer requirements."
        raw, source = case(extra=reuse)
        result = finalize(raw, source)
        self.assertEqual(owned(result, cs.COMMISSIONING_SUBJECT).scope_support, "APPROPRIATE")
        self.assertFalse(result.contractor_questions)

    def test_independent_installation_conflicts_and_missing_startup_remain_distinct(self):
        conflicts = ["Submitted communication wiring instructions prohibit the proposed cable type.",
                     "Submitted condensate drainage instructions require a pump for this route, but the proposal omits it."]
        raw, source = case(startup="", extra="\n".join(conflicts))
        raw.technical_assessments.append(item(cs.COMMISSIONING_SUBJECT, conflicts, "CONTRADICTORY", "UNSUPPORTED", conflicts=conflicts))
        raw.red_flags = conflicts * 2
        result = finalize(raw, source)
        for conflict in conflicts:
            self.assertEqual(result.red_flags.count(conflict), 1)
        self.assertTrue(any("startup checks" in q for q in result.contractor_questions))
        self.assertLessEqual(len(result.contractor_questions), 6)
        again = finalize(result, source)
        self.assertEqual(result.model_dump(), again.model_dump())

if __name__=="__main__":
    unittest.main()
