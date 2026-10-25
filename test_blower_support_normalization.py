"""Offline sufficiency regressions; model responses are supplied, never requested."""
import contextlib
import html
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
import electrical_controls as ec
from evidence_integrity import assessment_owner
from test_repair_diagnostics import upload
from test_technical_support_derivation import analysis_with, assessment


BAD = Path(__file__).with_name("electrical_blower_bad_test.txt").read_text()
GOOD = Path(__file__).with_name("electrical_blower_good_test.txt").read_text()
ASSERTION = "Blower motor is bad."
MODEL_FLAG = "No specific model or compatibility details for the proposed replacement motor are given."
POSITIVES = ["The warranty covers both parts and labor for one year.",
             "The quoted scope includes essential steps for replacing the blower motor."]


def classified():
    return main.QuoteClassification(quote_type="repair", system_type="unknown",
        primary_scope="ECM blower motor replacement", repair_components=["blower motor"],
        modules_required=[main.AnalysisModule.MOTORS])


def raw(evidence=None, status="INCOMPLETE", scope="PARTIALLY_DEFINED"):
    result = analysis_with([assessment(status, scope, subject="Blower motor failure",
        evidence=[ASSERTION] if evidence is None else evidence,
        gaps=[] if status in {"CONFIRMED", "ADEQUATE"} else ["No documented tests confirming the blower motor's failure."])],
        pricing="LIMITED")
    result.project_overview = "This proposal replaces a bad ECM blower motor."
    result.red_flags = [MODEL_FLAG, "Blower motor diagnostic tests are missing.",
                        "Blower motor failure is not established by testing."]
    result.good_signs = POSITIVES.copy()
    return result


def finalize(value, source=BAD):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(value, source, 1, classified(), fresh_analysis=True)


class BlowerSupportNormalizationTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_unsupported(self, result):
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        item = next(a for a in result.technical_assessments if assessment_owner(a.subject) == "blower motor")
        self.assertEqual((item.diagnostic_evidence_status, item.scope_support), ("ABSENT", "UNSUPPORTED"))
        self.assertEqual(item.documented_evidence, [])

    def test_exact_bad_fixture_corrects_partial_assessment_and_customer_report(self):
        incoming = raw()
        before = incoming.model_dump()
        result, report = upload(incoming, BAD, classified())
        self.assert_unsupported(result)
        self.assertEqual(result.decision.pricing_transparency, "LIMITED")
        self.assertEqual(result.good_signs, POSITIVES)
        self.assertEqual(len(result.red_flags), 1)
        self.assertIn("without findings", result.red_flags[0])
        self.assertNotIn(MODEL_FLAG, report)
        self.assertNotIn(ASSERTION, html.unescape(report))
        self.assertNotIn("bad ECM blower", report)
        self.assertNotIn("observations point toward", report)
        self.assertIn("Unsupported", report)
        self.assertIn("GET A SECOND OPINION", report)
        self.assertIn("recommends replacing", result.project_overview)
        self.assertEqual(len(result.contractor_questions), 2)
        self.assertEqual(main.repair_diagnostic_question_owner(result.contractor_questions[0]), "motor_blower")
        self.assertEqual(main.contractor_question_category(result.contractor_questions[1]), "pricing")
        self.assertIn("itemized", result.contractor_questions[1].lower())
        self.assertEqual(result.analysis_integrity.coverage_gaps, [])
        self.assertEqual(ec.electrical_items(result), [])
        self.assertEqual(report, main.build_report_html(result, 1))
        self.assertEqual(incoming.model_dump(), before)

    def test_assertions_cannot_support_a_partial_or_confirmed_diagnosis(self):
        for assertion in (ASSERTION, "Failed ECM blower motor/module.", "Technician diagnosed a faulty blower motor."):
            for status, scope in (("INCOMPLETE", "PARTIALLY_DEFINED"), ("CONFIRMED", "APPROPRIATE")):
                with self.subTest(assertion=assertion, status=status):
                    self.assert_unsupported(finalize(raw([assertion], status, scope),
                        "Replace ECM blower motor.\n" + assertion))

    def test_symptoms_alone_are_absent_evidence(self):
        symptoms = ["Indoor fan is not running and system is not cooling properly.",
                    "Blower motor is not running.", "No airflow from the vents.",
                    "Blower does not run.", "Customer reported blower motor does not operate."]
        for symptom in symptoms:
            with self.subTest(symptom=symptom):
                self.assert_unsupported(finalize(raw([symptom]), "Replace blower motor.\n" + symptom))

    def test_recommendation_and_planned_tests_are_not_results(self):
        for planned in ("Replace ECM blower motor.", "Test system operation.",
                        "Blower motor replacement recommended.",
                        "The contractor will test blower motor windings.",
                        "Blower motor testing is planned.", "Verify blower motor power and control command."):
            with self.subTest(planned=planned):
                self.assert_unsupported(finalize(raw([ASSERTION, planned]), BAD + "\n" + planned))

    def test_missing_measurements_are_not_findings(self):
        for missing in ("No blower motor voltage or winding measurements are documented.",
                        "Blower motor was not tested.", "Blower motor diagnostic measurements were not provided.",
                        "Blower motor testing was not performed."):
            with self.subTest(missing=missing):
                self.assert_unsupported(finalize(raw([ASSERTION, missing]), BAD + "\n" + missing))

    def test_meaningful_incomplete_findings_remain_partial(self):
        for finding in ("Blower motor power input measured 240 VAC.",
                        "Blower harness inspected and found secure.",
                        "Blower motor power is present.",
                        "Blower motor power is present, but winding tests were not performed."):
            with self.subTest(finding=finding):
                result = finalize(raw([finding]), "Replace blower motor.\n" + finding)
                self.assertEqual(result.decision.technical_support, "PARTIALLY_SUPPORTED")
                self.assertEqual(result.technical_assessments[0].documented_evidence, [finding])

    def test_unknown_source_backed_observation_is_not_inferred_absent(self):
        finding = "Blower manufacturer service procedure records an inconclusive module response."
        result = finalize(raw([finding]), "Replace blower motor.\n" + finding)
        self.assertEqual(result.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(result.technical_assessments[0].documented_evidence, [finding])

    def test_exact_good_ecm_fixture_retains_confirmed_support(self):
        findings = [s.strip(" -\t") for s in GOOD.split("Diagnostic findings:")[1].split("Recommended repair:")[0].splitlines() if s.strip()]
        result = finalize(raw(findings, "CONFIRMED", "APPROPRIATE"), GOOD)
        self.assertEqual(result.decision.technical_support, "SUPPORTED")
        self.assertEqual(result.technical_assessments[0].documented_evidence, findings)
        self.assertEqual(result.red_flags, [])
        self.assertEqual(ec.electrical_items(result), [])

    def test_direct_physical_finding_does_not_require_numeric_readings(self):
        finding = "Blower motor shaft inspected and found seized with the blower wheel disconnected."
        for status in ("ADEQUATE", "CONFIRMED"):
            with self.subTest(status=status):
                result = finalize(raw([finding], status, "APPROPRIATE"), "Replace blower motor.\n" + finding)
                self.assertEqual(result.decision.technical_support, "SUPPORTED")
                self.assertEqual(result.technical_assessments[0].documented_evidence, [finding])

    def test_psc_findings_and_normal_applicable_capacitor_are_preserved(self):
        findings = ["PSC blower motor power input measured 240 VAC.",
                    "PSC blower motor winding tested open.", "Blower run capacitor tested normal."]
        result = finalize(raw(findings, "CONFIRMED", "APPROPRIATE"), "Replace PSC blower motor.\n" + "\n".join(findings))
        self.assertEqual(result.decision.technical_support, "SUPPORTED")
        self.assertEqual(result.technical_assessments[0].documented_evidence, findings)
        self.assertEqual(ec.electrical_items(result), [])

    def test_missing_model_only_is_not_a_major_red_flag(self):
        for flag in (MODEL_FLAG, "Replacement blower motor model number is missing.",
                     "ECM motor compatibility is not documented."):
            with self.subTest(flag=flag):
                incoming = raw()
                incoming.red_flags = [flag]
                result = finalize(incoming)
                self.assertNotIn(flag, result.red_flags)
                self.assertEqual(len(result.red_flags), 1)

    def test_documented_compatibility_conflict_survives(self):
        for conflict in ("The proposed replacement blower motor connector is incompatible with the existing harness.",
                         "There is no compatibility between the proposed replacement blower motor connector and the existing harness."):
            with self.subTest(conflict=conflict):
                incoming = raw()
                incoming.red_flags = [conflict, MODEL_FLAG]
                result = finalize(incoming, BAD + "\n" + conflict)
                self.assertIn(conflict, result.red_flags)
                self.assertTrue(any("without findings" in flag for flag in result.red_flags))

    def test_unrelated_component_flag_is_not_removed(self):
        flag = "Proposed control board model is missing."
        incoming = raw()
        incoming.red_flags.append(flag)
        result = finalize(incoming)
        self.assertIn(flag, result.red_flags)

    def test_documented_warranty_and_scope_positives_survive(self):
        self.assertEqual(finalize(raw()).good_signs, POSITIVES)

    def test_missing_or_normal_capacitor_does_not_create_an_assessment(self):
        for reference in ("Capacitor was not tested.", "No capacitor measurements were documented.",
                          "Capacitor tested normal."):
            with self.subTest(reference=reference):
                result = finalize(raw(), BAD + "\n" + reference)
                self.assert_unsupported(result)
                self.assertEqual(ec.electrical_items(result), [])
                self.assertNotIn("capacitor", " ".join(result.red_flags + result.contractor_questions).lower())

    def test_independent_capacitor_failure_remains_recognized(self):
        result = finalize(raw(), BAD + "\nCapacitor inspected and found ruptured.")
        self.assert_unsupported(result)
        self.assertEqual(len(ec.electrical_items(result)), 1)
        self.assertEqual(ec.electrical_items(result)[0].diagnostic_evidence_status, "CONFIRMED")

    def test_finalization_is_idempotent(self):
        first = finalize(raw())
        self.assertEqual(first.model_dump(), finalize(first).model_dump())

    def test_analyze_api_and_email_share_corrected_decision(self):
        responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=v))])
                     for v in (classified(), raw())]
        with patch.object(main.client.beta.chat.completions, "parse", side_effect=responses), \
                patch.object(main, "send_review_email") as email, contextlib.redirect_stdout(io.StringIO()):
            response = TestClient(main.app).post("/analyze", json={"packageName": "tier1", "files": [
                {"fileName": "electrical_blower_bad_test.txt", "extractedText": BAD}]})
        self.assertEqual(response.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assert_unsupported(final)
        self.assertEqual(response.json(), final.model_dump(mode="json"))

    def test_source_free_compatibility_mode_retains_existing_assessment(self):
        incoming = raw()
        with contextlib.redirect_stdout(io.StringIO()):
            result = main.finalize_customer_analysis(incoming, fresh_analysis=False)
        self.assertEqual(result.technical_assessments[0], incoming.technical_assessments[0])


if __name__ == "__main__":
    unittest.main()
