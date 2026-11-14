"""Exact saved analysis replay with the user-supplied installation scenario.

SOURCE preserves the source excerpt embedded in the saved JSON and supplies the
remaining facts from the user's scenario. It is not claimed to be an independently
recovered verbatim original request. The saved analysis is copied without edits.
"""
import contextlib
import html
import io
from pathlib import Path
import unittest

import main
import commissioning as cs
from evidence_integrity import material_work, assessment_owner
from test_mini_split_installation import case, item, finalize, owned, MATCH, SIZING_SUBJECT
from test_mini_split_scope_routing import classify, classification
from test_repair_diagnostics import upload

RESPONSE=Path(__file__).with_name('test_fixtures')/'mini_split_multizone_20261111_162228.json'
SOURCE = 'Project:\nInstall a new three-zone ductless mini-split\nheat pump in an existing residence in Reno, NV.\n\nProposed equipment:\nOne outdoor heat pump, nominal 24,000 BTU/h.\nTwo indoor wall units, nominal 9,000 BTU/h each.\nOne indoor wall unit, nominal 12,000 BTU/h.\n\nCombined indoor nominal capacity: 30,000 BTU/h.\nOutdoor nominal capacity: 24,000 BTU/h.\n\nNo manufacturer or model numbers provided.\nNo manufacturer-approved combination table.\nNo connected-zone capacity documentation.\nNo documented individual room load calculations provided.\nNo low-outdoor-temperature heating capacity data.\nNo supplemental heating assessment documented.\n\nInstallation includes pressure testing, system evacuation,\nstartup and operational verification.\nContractor will verify system operation after installation.\n\n$13,500 lump sum, equipment and labor included.'


def saved():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


def installed_classification():
    return main.QuoteClassification(quote_type="installation",system_type="heat pump",
        primary_scope="Install a new three-zone ductless mini-split", replacement_components=["outdoor unit","indoor units"],
        modules_required=[main.AnalysisModule.REPAIR_VS_REPLACE,main.AnalysisModule.SYSTEM_SIZING,main.AnalysisModule.COMMISSIONING])


def finalized_live(raw=None, source=SOURCE):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw or saved(), source, 1, installed_classification(), fresh_analysis=True)


class MultiZoneInstallationLiveTests(unittest.TestCase):
    def assert_new_installation(self,result):
        self.assertEqual(result.support_heading,"Installation Support")
        self.assertEqual(result.decision.verdict,main.determine_verdict(result.decision))
        text=" ".join([result.recommendation,result.banner_explanation,result.homeowner_takeaway,result.bottom_line,
            result.missing_information,result.equipment_analysis,result.installation_concerns,*result.contractor_questions])
        for forbidden in ("diagnosis or repair","replacement basis","retained","wrong about the repair"):
            self.assertNotIn(forbidden,text.lower())
        self.assertNotIn("replacement basis",result.analysis_integrity.coverage_gaps)

    def test_exact_saved_missing_only_installation_uses_canonical_policy(self):
        raw=saved()
        self.assertEqual(raw.decision.technical_support,"UNSUPPORTED")
        self.assertEqual(len(raw.contractor_questions),6)
        before=raw.model_dump()
        result=finalized_live(raw)
        self.assert_new_installation(result)
        self.assertEqual(result.decision.technical_support,"PARTIALLY_SUPPORTED")
        self.assertEqual(result.decision.verdict,"REVIEW_BEFORE_APPROVING")
        self.assertEqual(result.decision.pricing_transparency,"LIMITED")
        self.assertFalse(result.red_flags)
        self.assertEqual(raw.model_dump(),before)
        self.assertEqual(main.derive_technical_support(result.technical_assessments),result.decision.technical_support)

    def test_new_installation_has_no_replacement_basis_owner(self):
        for text in (SOURCE,"Install a new single-zone ductless mini-split.","New multi-zone mini-split installation."):
            with self.subTest(text=text):
                self.assertNotIn("replacement basis",material_work(text,installed_classification()))
                routed=classify(text,installed_classification())
                self.assertNotIn(main.AnalysisModule.REPAIR_VS_REPLACE,routed.modules_required)
                self.assertIn(main.AnalysisModule.EQUIPMENT_MATCHING,routed.modules_required)

    def test_uploaded_metadata_does_not_supply_replacement_or_repair_scope(self):
        wrapped = "QUOTE 1\nFile Name: replacement-repair-evidence.txt\n\n" + SOURCE
        self.assertTrue(main.new_mini_split_installation(wrapped))
        self.assertNotIn("replacement basis",material_work(wrapped,installed_classification()))
        self.assertFalse(main.new_mini_split_installation(wrapped+"\nReplace a compressor on a separate existing system."))
        self.assertFalse(main.new_mini_split_installation(wrapped+"\nQUOTE 2\nReplace the existing mini-split."))

    def test_load_assessment_alias_is_sizing_not_failed_diagnosis(self):
        result=finalized_live()
        loads=[a for a in result.technical_assessments if "Load assessment" in a.subject]
        self.assertEqual(len(loads),1)
        self.assertEqual(loads[0].diagnostic_evidence_status,"INCOMPLETE")
        self.assertEqual(loads[0].scope_support,"PARTIALLY_DEFINED")
        self.assertEqual(assessment_owner(loads[0].subject),"system sizing")

    def test_new_single_zone_missing_only_specifications(self):
        result=finalized_live(source=SOURCE.replace("three-zone","single-zone"))
        self.assert_new_installation(result)
        self.assertEqual(result.decision.technical_support,"PARTIALLY_SUPPORTED")
        self.assertFalse(result.red_flags)

    def test_valid_documented_installation_remains_supported(self):
        raw,source=case()
        result=finalize(raw,source)
        self.assert_new_installation(result)
        self.assertEqual(result.decision.technical_support,"SUPPORTED")
        self.assertEqual(result.decision.verdict,"PROCEED")
        self.assertFalse(result.contractor_questions)
        self.assertFalse(main.replacement_basis_assessment(result))

    def test_actual_equipment_conflict_still_requires_second_opinion(self):
        conflict="Submitted manufacturer document prohibits the proposed indoor/outdoor equipment combination."
        raw,source=case(match=conflict)
        raw.technical_assessments[1]=item(MATCH,[conflict],"CONTRADICTORY","UNSUPPORTED",conflicts=[conflict])
        raw.red_flags=[conflict]
        result=finalize(raw,source)
        self.assert_new_installation(result)
        self.assertEqual(result.decision.verdict,"GET_A_SECOND_OPINION")
        self.assertIn(conflict,result.red_flags)
        self.assertIn("installation",result.banner_explanation.lower())

    def test_adequate_loads_and_cold_weather_output_do_not_get_missing_questions(self):
        raw,source=case()
        result=finalize(raw,source)
        self.assertEqual(owned(result,SIZING_SUBJECT).scope_support,"APPROPRIATE")
        self.assertFalse(any("load" in q.lower() or "winter" in q.lower() for q in result.contractor_questions))

    def test_missing_combination_for_named_models_does_not_request_model_identities(self):
        models = "Outdoor model SYN-O24 and indoor models SYN-I18 and SYN-I12 are listed. No approved combination for these models is supplied."
        raw,source = case(match=models)
        raw.technical_assessments[1] = item(MATCH,[models],"INCOMPLETE","PARTIALLY_DEFINED",
            gaps=["Manufacturer combination documentation is missing."])
        result = finalize(raw,source)
        self.assertTrue(any("combination" in q for q in result.contractor_questions))
        self.assertFalse(any(q.startswith("What manufacturer") for q in result.contractor_questions))

    def test_question_priority_dedup_and_cold_weather_gap(self):
        result=finalized_live()
        questions=result.contractor_questions
        self.assertLessEqual(len(questions),6)
        self.assertIn("manufacturer",questions[0].lower())
        self.assertIn("model",questions[0].lower())
        self.assertIn("combination",questions[1].lower())
        self.assertIn("room",questions[2].lower())
        self.assertIn("load",questions[2].lower())
        self.assertIn("winter design",questions[3].lower())
        self.assertIn("supplemental",questions[3].lower())
        self.assertIn("winter design",result.missing_information)
        self.assertIn("document",questions[4].lower())
        self.assertEqual(main.contractor_question_category(questions[-1]),"pricing")
        self.assertEqual(sum("load summary" in q.lower() or "room-by-room" in q.lower() for q in questions),1)
        self.assertFalse(any("how the proposed capacities" in q.lower() for q in questions))

    def test_planned_commissioning_tasks_are_acknowledged_without_repeat_requests(self):
        result=finalized_live()
        plan=" ".join(cs.commissioning_items(result)[0].documented_evidence).lower()
        self.assertIn("pressure test",plan)
        self.assertIn("evacuation",plan)
        paragraphs=" ".join(cs.commissioning_paragraphs(result)).lower()
        self.assertIn("pressure testing",paragraphs)
        self.assertIn("evacuation",paragraphs)
        self.assertIn("operation",paragraphs)
        questions=" ".join(result.contractor_questions).lower()
        self.assertNotIn("what startup checks will be completed",questions)
        self.assertTrue(any("operation" in s.lower() for s in result.good_signs))

    def test_documented_startup_records_remove_generic_followup(self):
        result=finalized_live(source=SOURCE+"\nManufacturer startup procedure includes documentation of final operating readings.")
        self.assertEqual(cs.commissioning_items(result)[0].scope_support,"APPROPRIATE")
        self.assertFalse(any(main.contractor_question_category(q)=="commissioning" for q in result.contractor_questions))

    def test_documented_startup_conflict_keeps_priority_over_missing_paperwork(self):
        conflict = "No startup testing will be performed."
        source = SOURCE.replace("startup and operational verification.",conflict).replace(
            "Contractor will verify system operation after installation.","")
        result = finalized_live(source=source)
        self.assertEqual(result.decision.verdict,"GET_A_SECOND_OPINION")
        self.assertEqual(cs.commissioning_items(result)[0].scope_support,"UNSUPPORTED")
        self.assertEqual(main.contractor_question_category(result.contractor_questions[0]),"commissioning")
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]),"pricing")

    def test_complete_replacement_and_partial_retrofit_keep_their_owners(self):
        for scope in ("Replace the complete mini-split system.","Replace mini-split outdoor unit; retain existing indoor heads."):
            raw,source=case(scope=scope,match="")
            result=finalize(raw,source)
            self.assertIn("replacement basis",material_work(source,installed_classification()))
            self.assertIsNotNone(main.replacement_basis_assessment(result))
            self.assertEqual(result.support_heading,"Diagnostic Support")
        self.assertTrue(main.primary_equipment_matching_assessment(result))
        self.assertIn("retained"," ".join(result.contractor_questions))

    def test_component_only_and_mixed_scope_preserve_repairs(self):
        from test_mini_split_scope_routing import finalize as repair_finalize
        for component in ("thermistor","EEV","inverter drive board"):
            result=repair_finalize("Replace mini-split "+component+".")
            self.assertFalse(main.sizing_assessments(result))
            self.assertFalse(cs.commissioning_items(result))
            self.assertEqual(result.support_heading,"Diagnostic Support")
        raw,source=case(extra="Replace the compressor in a separate existing system; no diagnostic tests documented.")
        result=finalize(raw,source)
        self.assertEqual(result.decision.technical_support,"UNSUPPORTED")
        self.assertEqual(result.support_heading,"Diagnostic Support")

    def test_independent_severe_concern_survives_missing_document_normalization(self):
        conflict="The proposed outdoor mounting location has documented structural damage and cannot support the equipment."
        raw=saved()
        raw.technical_assessments.append(item("Mounting structure safety",[conflict],"CONTRADICTORY","UNSUPPORTED",conflicts=[conflict]))
        raw.red_flags.append(conflict)
        raw.contractor_questions.append("How will the documented structural damage at the mounting location be corrected?")
        result=finalized_live(raw,SOURCE+"\n"+conflict)
        self.assertEqual(result.decision.verdict,"GET_A_SECOND_OPINION")
        self.assertIn(conflict,result.red_flags)
        self.assertTrue(any("structural damage" in q for q in result.contractor_questions))

    def test_render_and_upload_parity_repeated_finalization(self):
        result,report=upload(saved(),SOURCE,installed_classification())
        self.assert_new_installation(result)
        self.assertIn("<strong>Installation Support</strong>",report)
        self.assertNotIn("<strong>Diagnostic Support</strong>",report)
        self.assertIn("Does the Equipment Selection Make Sense?",report)
        self.assertNotIn("Does the Diagnosis Make Sense?",report)
        self.assertEqual(report,main.build_report_html(result,1))
        for question in result.contractor_questions:self.assertEqual(html.unescape(report).count(question),1)
        for _ in range(3):
            again=finalized_live(main.HVACAnalysis.model_validate_json(result.model_dump_json()))
            self.assertEqual(result.model_dump(),again.model_dump())
            self.assertEqual(report,main.build_report_html(again,1))
            result=again

if __name__=="__main__":unittest.main()
