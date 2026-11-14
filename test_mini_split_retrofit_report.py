"""Exact saved response, replayed with the user's documented synthetic scenario.

SOURCE retains the excerpt embedded in the response and the supplied scenario;
it is not claimed to be a verbatim recovered submission or the earlier failed
token-limit proposal. The saved response fixture is copied without changes.
"""
import contextlib
import html
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import main
import commissioning as cs
from evidence_integrity import assessment_owner
from test_mini_split_installation import item, MATCH
from test_mini_split_multizone_cleanup import saved as multizone_response
from test_mini_split_multizone_live import finalized_live as finalize_multizone
from test_mini_split_startup_regression import SOURCE as REPAIR_SOURCE, saved_response as repair_response
from test_mini_split_scope_routing import classification as repair_classification
from test_repair_diagnostics import upload

RESPONSE = Path(__file__).with_name("test_fixtures") / "mini_split_retrofit_20261113_183810.json"
SOURCE = """Project:
Replace the outdoor unit of an existing
single-zone ductless mini-split system
in Reno, Nevada.

Proposed equipment:
New inverter-driven outdoor heat pump.
Nominal capacity: 18,000 BTU/h.

Existing indoor equipment will be retained.
Existing refrigerant line set will be reused.
No manufacturer or model numbers provided.
Manufacturer compatibility documentation for the retained indoor unit is not provided.
Existing refrigerant type is unknown.
Line-set dimensions and refrigerant suitability are not verified.
Electrical and communication compatibility is not documented.
No heating or cooling load calculation provided.

Scope:
Remove existing outdoor unit.
Install replacement outdoor heat pump.
Reconnect existing refrigerant piping.
Reconnect electrical and communication wiring.
Verify system operation after installation.
Pressure-testing and evacuation procedures are not detailed.
No documented commissioning test results.
No total quoted price supplied.
"""


def saved():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


def classification():
    return main.QuoteClassification(quote_type="replacement", system_type="ductless mini-split",
        primary_scope="Replace outdoor unit and retain indoor equipment", replacement_components=["outdoor unit"],
        modules_required=[main.AnalysisModule.EQUIPMENT_MATCHING, main.AnalysisModule.SYSTEM_SIZING,
                          main.AnalysisModule.PRICING, main.AnalysisModule.COMMISSIONING,
                          main.AnalysisModule.REPAIR_VS_REPLACE])


def finalize(raw=None, source=SOURCE):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw or saved(), source, 1, classification(), fresh_analysis=True)


class MiniSplitRetrofitReportTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_policy(self, result):
        self.assertEqual(result.decision.technical_support, main.derive_technical_support(result.technical_assessments))
        self.assertEqual(result.decision.verdict, main.determine_verdict(result.decision))

    def test_exact_saved_response_preserves_inputs_and_uses_existing_policy(self):
        raw = saved()
        before = raw.model_dump()
        self.assertEqual(raw.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertIn("Absence of pressure testing", raw.red_flags[0])
        self.assertTrue(any("address these provisions:" in q for q in raw.contractor_questions))
        result = finalize(raw)
        self.assert_policy(result)
        self.assertEqual(result.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(result.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(result.decision.pricing_transparency, "ABSENT")

    def test_missing_compatibility_aliases_cannot_bypass_partial_support(self):
        for reverse in (False, True):
            raw = saved()
            if reverse:
                raw.technical_assessments.reverse()
            result = finalize(raw)
            matching = [a for a in result.technical_assessments if assessment_owner(a.subject) == "equipment matching"]
            self.assertEqual(len(matching), 2)
            self.assertTrue(all(a.scope_support == "PARTIALLY_DEFINED" and not a.contradictions for a in matching))
            self.assert_policy(result)
            self.assertEqual(result.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_actual_retained_equipment_conflict_remains_unsupported(self):
        conflict = "Submitted manufacturer documentation prohibits pairing the retained indoor unit with the proposed outdoor unit."
        raw = saved()
        raw.technical_assessments[0] = item(raw.technical_assessments[0].subject, [conflict],
            "CONTRADICTORY", "UNSUPPORTED", conflicts=[conflict])
        raw.red_flags.append(conflict)
        result = finalize(raw, SOURCE + conflict)
        self.assert_policy(result)
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertIn(conflict, result.red_flags)
        self.assertNotIn("diagnosis or repair", result.recommendation)

    def test_partial_pair_cannot_hide_independent_matching_conflict(self):
        conflict = "Submitted manufacturer documentation prohibits the proposed outdoor unit with the retained upstairs indoor unit."
        raw = saved()
        raw.technical_assessments.append(item("Upstairs indoor/outdoor equipment compatibility", [conflict],
            "CONTRADICTORY", "UNSUPPORTED", conflicts=[conflict]))
        raw.red_flags.append(conflict)
        result = finalize(raw, SOURCE + conflict)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertIn(conflict, result.red_flags)

    def test_unknown_line_set_suitability_is_a_gap_not_a_defect(self):
        result = finalize()
        plan = cs.commissioning_items(result)[0]
        self.assertEqual(plan.scope_support, "PARTIALLY_DEFINED")
        self.assertTrue(any("line-set reuse" in g for g in plan.material_gaps))
        self.assertFalse(plan.contradictions)
        self.assertFalse(result.red_flags)
        self.assertTrue(any("refrigerant" in q and "line-set" in q for q in result.contractor_questions))

    def test_documented_line_set_conflict_is_preserved(self):
        conflict = "The submitted installation instructions prohibit reusing the existing line set, but the proposal retains it."
        raw = saved()
        raw.technical_assessments.append(item(cs.COMMISSIONING_SUBJECT, [conflict],
            "CONTRADICTORY", "UNSUPPORTED", conflicts=[conflict]))
        raw.red_flags.append(conflict)
        result = finalize(raw, SOURCE + conflict)
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertIn(conflict, result.red_flags)
        self.assertTrue(any("line set conflict" in q for q in result.contractor_questions))

    def test_missing_procedure_documentation_is_not_confirmed_omission(self):
        result = finalize()
        self.assertFalse(result.red_flags)
        plan = cs.commissioning_items(result)[0]
        self.assertIn("Pressure-testing and evacuation procedures are not detailed.", plan.material_gaps)
        self.assertTrue(any("pressure-testing" in q for q in result.contractor_questions))

    def test_explicit_procedure_omission_still_requires_resolution(self):
        for conflict in ("Pressure testing and evacuation will not be performed.",
                         "Required startup testing is excluded."):
            with self.subTest(conflict=conflict):
                source = SOURCE.replace("Pressure-testing and evacuation procedures are not detailed.", conflict)
                result = finalize(source=source)
                self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
                self.assertIn(conflict, cs.commissioning_items(result)[0].contradictions)
                self.assertIn(conflict, result.red_flags)

    def test_planned_verification_is_credited_without_inventing_completed_tests(self):
        result = finalize()
        self.assertIn("The proposal includes operational verification after installation.", result.good_signs)
        self.assertNotIn("completed checks", " ".join(result.good_signs))
        self.assertEqual(cs.commissioning_items(result)[0].diagnostic_evidence_status, "INCOMPLETE")

    def test_missing_verification_does_not_keep_a_planned_positive(self):
        raw = saved()
        raw.technical_assessments = [a for a in raw.technical_assessments if a.subject != "Scope of Installation for replacement"]
        for assessment in raw.technical_assessments:
            assessment.documented_evidence = [e for e in assessment.documented_evidence if "Verify system operation" not in e]
        result = finalize(raw, SOURCE.replace("Verify system operation after installation.\n", ""))
        self.assertFalse(any("operational verification" in sign for sign in result.good_signs))
        self.assertTrue(any("startup" in q for q in result.contractor_questions))

    def test_missing_replacement_basis_is_preserved_without_inventing_failure(self):
        result = finalize()
        self.assertIn("replacement basis", result.analysis_integrity.coverage_gaps)
        self.assertTrue(any("condition or project goal" in q for q in result.contractor_questions))
        self.assertIn("reason for replacing", result.missing_information.lower())
        self.assertEqual(result.support_heading, "Installation Support")
        for text in (result.banner_explanation, result.homeowner_takeaway, result.bottom_line, result.recommendation):
            self.assertNotIn("diagnosis or repair", text)

    def test_documented_elective_replacement_basis_remains_valid(self):
        reason = "The homeowner requested a voluntary planned upgrade; no equipment failure is claimed."
        raw = saved()
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.technical_assessments.append(item("Basis for replacement", [reason]))
        result = finalize(raw, SOURCE + reason)
        self.assertEqual(main.replacement_basis_assessment(result).scope_support, "APPROPRIATE")
        self.assertNotIn("replacement basis", result.analysis_integrity.coverage_gaps)
        self.assertEqual(result.decision.verdict, "REVIEW_BEFORE_APPROVING")

    def test_unsupported_failure_driven_replacement_is_not_downgraded(self):
        claim = "Contractor claims the outdoor unit failed and requires replacement, without diagnostic results."
        raw = saved()
        raw.replacement_context = main.ReplacementContext.FAILURE_DRIVEN
        raw.technical_assessments.append(item("Basis for replacement", [claim], "ABSENT", "UNSUPPORTED",
            gaps=["No documented diagnostic findings establish outdoor-unit failure."]))
        result = finalize(raw, SOURCE + claim)
        self.assert_policy(result)
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(main.replacement_basis_assessment(result).scope_support, "UNSUPPORTED")

    def test_absent_price_requests_total_without_inventing_amount(self):
        raw = saved()
        raw.pricing_review = "The quoted total is $99999."
        result = finalize(raw)
        self.assertEqual(result.decision.pricing_transparency, "ABSENT")
        self.assertFalse(result.price_facts)
        self.assertIn("No total project price was supplied", result.pricing_review)
        self.assertIn("total project price", result.contractor_questions[-1])
        for text in (result.pricing_review, result.recommendation, result.homeowner_takeaway,
                     result.bottom_line, *result.contractor_questions):
            self.assertNotIn("quoted total", text)
            self.assertNotIn("$99999", text)

    def test_lump_sum_price_keeps_independent_itemization_question(self):
        raw = saved()
        raw.decision.pricing_transparency = "LIMITED"
        raw.pricing_review = "The $8,000 total includes equipment and labor without a breakdown."
        result = finalize(raw, SOURCE.replace("No total quoted price supplied.", "Total price: $8,000 lump sum, equipment and labor included."))
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertIn("$8,000 total", result.contractor_questions[-1])
        self.assertEqual(result.decision.technical_support, finalize().decision.technical_support)
        self.assertEqual(result.price_facts[0].items[0].amount, 8000)

    def test_questions_cover_material_gaps_once_in_plain_language(self):
        result = finalize()
        questions = result.contractor_questions
        self.assertLessEqual(len(questions), 6)
        self.assertEqual(len(questions), len(set(questions)))
        self.assertIn("manufacturer combination", questions[0])
        self.assertTrue(any("electrical/communication compatibility" in q for q in questions))
        self.assertTrue(any("refrigerant" in q and "line-set" in q for q in questions))
        self.assertTrue(any("load summary" in q for q in questions))
        self.assertEqual(sum("pressure-testing" in q or "startup checks" in q for q in questions), 1)
        self.assertEqual(main.contractor_question_category(questions[-1]), "pricing")
        for q in questions:
            self.assertNotIn("address these provisions:", q)
            self.assertLess(len(q), 190)

    def test_independent_source_supported_concern_remains_visible(self):
        concern = "The existing outdoor mounting bracket has documented structural damage."
        question = "How will the damaged outdoor mounting bracket be addressed?"
        raw = saved()
        raw.red_flags.append(concern)
        raw.contractor_questions.append(question)
        result = finalize(raw, SOURCE + concern)
        self.assertIn(concern, result.red_flags)
        self.assertIn(question, result.contractor_questions)
        self.assertNotIn(saved().red_flags[0], result.red_flags)

    def test_refrigerant_question_about_separate_equipment_is_not_a_duplicate(self):
        raw = saved()
        question = "What refrigerant type is used by the separate garage system?"
        raw.contractor_questions.append(question)
        result = finalize(raw, SOURCE + "The separate garage system's refrigerant type also needs identification.")
        self.assertIn(question, result.contractor_questions)
        self.assertTrue(any("line-set size" in q for q in result.contractor_questions))

    def test_new_multizone_acceptance_stays_partial_with_installation_credit(self):
        raw = multizone_response()
        result = finalize_multizone(raw)
        self.assertEqual(result.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(result.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(result.support_heading, "Installation Support")
        self.assertTrue(result.good_signs)
        self.assertEqual(result.red_flags, raw.red_flags)

    def test_component_repair_still_avoids_system_installation_review(self):
        result, report = upload(repair_response(), REPAIR_SOURCE, repair_classification())
        self.assertFalse(cs.commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertNotIn("How Will Startup Be Verified?", report)

    def test_repeated_finalization_is_idempotent(self):
        result = finalize()
        for _ in range(3):
            repeated = finalize(result)
            self.assertEqual(result.model_dump(), repeated.model_dump())
            result = repeated

    def test_production_upload_html_and_finalized_json_agree(self):
        result, report = upload(saved(), SOURCE, classification())
        self.assertEqual(report, main.build_report_html(result, 1))
        for question in result.contractor_questions:
            self.assertIn(question, html.unescape(report))
        self.assertEqual(result.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertNotIn(saved().red_flags[0], html.unescape(report))
        self.assertNotIn("diagnosis or repair", html.unescape(report))
        self.assertEqual(main.HVACAnalysis.model_validate_json(result.model_dump_json()), result)


if __name__ == "__main__":
    unittest.main()
