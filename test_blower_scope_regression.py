"""Offline ownership regressions for the submitted live blower proposal."""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
import electrical_controls as ec
from evidence_integrity import material_work, assessment_owner
from test_repair_diagnostics import upload
from test_technical_support_derivation import analysis_with, assessment


# Verbatim quote supplied with the live-production regression report.
LIVE_BLOWER = """ABC Heating & Cooling

Service Recommendation:
Replace indoor blower motor.

Customer complaint:
System is running but there is no airflow from the vents.

Technician findings:
Blower motor is not running.

Recommended repair:
Replace blower motor.

Blower motor replacement: $975
Labor and materials included.

No voltage readings, capacitor measurements, amp readings, winding tests, or other diagnostic measurements are listed."""

MISSING = (
    "No capacitor measurement was documented.",
    "Capacitor was not tested.",
    "No capacitor replacement is proposed.",
    "No capacitor replacement recommended.",
    "No evidence of capacitor failure.",
    "Blower motor diagnosis does not include capacitor testing.",
    "No voltage, capacitor, amp, or winding measurements were listed.",
)
NORMAL = (
    "Capacitor tested normal.",
    "Capacitor measured within its rated tolerance.",
    "45 µF ±6% capacitor measured 44.8 µF.",
)


def classified():
    # Deliberately overinclusive AI classification: a mention is not scope.
    return main.QuoteClassification(quote_type="repair", system_type="unknown",
        primary_scope="Blower motor replacement", repair_components=["blower motor", "capacitor"],
        modules_required=[main.AnalysisModule.MOTORS, main.AnalysisModule.ELECTRICAL_CONTROLS])


def blower(status="ABSENT", scope="UNSUPPORTED", evidence=None):
    return analysis_with([assessment(status, scope, subject="Blower motor failure", evidence=evidence)],
                         pricing="LIMITED")


class BlowerScopeRegressionTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def finalize(self, raw, source):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.finalize_customer_analysis(raw, source, 1, classified(), fresh_analysis=True)

    def assert_blower_only(self, result):
        self.assertEqual({assessment_owner(a.subject) for a in result.technical_assessments}, {"blower motor"})
        self.assertEqual(result.analysis_integrity.coverage_gaps, [])
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(result.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(len(result.red_flags), 1)
        self.assertNotIn("capacitor", " ".join(result.red_flags).lower())
        self.assertNotIn("capacitor", result.missing_information.lower())
        self.assertNotIn("capacitor", " ".join(result.contractor_questions).lower())
        self.assertEqual([main.repair_diagnostic_question_owner(q) for q in result.contractor_questions],
                         ["motor_blower", None])
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]), "pricing")
        self.assertLessEqual(len(result.contractor_questions), 6)
        self.assertNotIn("proposed capacitor work", result.model_dump_json().lower())

    def test_live_quote_has_only_blower_material_scope(self):
        self.assertEqual(material_work(LIVE_BLOWER), {"blower motor"})
        self.assertEqual(material_work(LIVE_BLOWER, classified()), {"blower motor"})

    def test_missing_capacitor_references_do_not_create_work(self):
        for reference in MISSING:
            with self.subTest(reference=reference):
                source = "Replace blower motor.\n" + reference
                self.assertEqual(material_work(source, classified()), {"blower motor"})
                self.assertEqual(ec.proposed_components(source), [])

    def test_normal_capacitor_findings_do_not_create_work(self):
        for reference in NORMAL:
            with self.subTest(reference=reference):
                source = "Replace blower motor.\n" + reference
                self.assertEqual(material_work(source, classified()), {"blower motor"})
                self.assertEqual(ec.proposed_components(source), [])

    def test_generic_diagnostic_reference_does_not_create_work(self):
        source = "Replace blower motor.\nBlower motor diagnosis includes capacitor testing as applicable."
        self.assertEqual(material_work(source, classified()), {"blower motor"})
        self.assertFalse(ec.electrical_required(source))

    def test_routing_does_not_restore_phantom_electrical_module(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=classified()))])
        with patch.object(main.client.beta.chat.completions, "parse", return_value=response), contextlib.redirect_stdout(io.StringIO()):
            routed = main.classify_quotes(LIVE_BLOWER)
            knowledge = main.get_analysis_knowledge(classified(), LIVE_BLOWER)
        self.assertIn(main.AnalysisModule.MOTORS, routed.modules_required)
        self.assertNotIn(main.AnalysisModule.ELECTRICAL_CONTROLS, routed.modules_required)
        self.assertNotIn(ec.ELECTRICAL_CONTROLS_RULES.strip(), knowledge)

    def test_missing_or_normal_evidence_cannot_retain_phantom_assessment(self):
        for reference in (*MISSING, *NORMAL):
            with self.subTest(reference=reference):
                raw = blower()
                raw.technical_assessments.append(assessment("ABSENT", "UNSUPPORTED",
                    subject=ec.PREFIX + "capacitor", evidence=[reference]))
                self.assert_blower_only(self.finalize(raw, "Replace blower motor.\n" + reference))

    def test_exact_live_quote_upload_api_and_email_parity(self):
        raw = blower()
        before = raw.model_dump()
        final, report = upload(raw, LIVE_BLOWER, classified())
        self.assert_blower_only(final)
        self.assertEqual(final.technical_assessments[0].documented_evidence, [])
        self.assertNotIn("proposed capacitor work", report.lower())
        self.assertEqual(report, main.build_report_html(final, 1))
        responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
                     for value in (classified(), raw)]
        with patch.object(main.client.beta.chat.completions, "parse", side_effect=responses), \
                patch.object(main, "send_review_email") as email, contextlib.redirect_stdout(io.StringIO()):
            response = TestClient(main.app).post("/analyze", json={"packageName": "tier1", "files": [
                {"fileName": "repair-evidence.txt", "extractedText": LIVE_BLOWER}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), final.model_dump(mode="json"))
        self.assertEqual(response.json(), email.call_args.kwargs["analysis"].model_dump(mode="json"))
        with patch.dict("os.environ", {"EMAIL_APP_PASSWORD": "offline-placeholder"}), \
                patch.object(main.smtplib, "SMTP_SSL") as smtp:
            main.send_review_email("Offline", "offline@example.com", "tier1", ["repair-evidence.txt"], final)
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual(message.get_body(preferencelist=("html",)).get_content().rstrip(), report.rstrip())
        self.assertEqual(raw.model_dump(), before)

    def test_affirmative_capacitor_replacement_still_requires_assessment(self):
        source = "Replace blower motor.\nReplace failed capacitor.\nNo capacitor measurement was documented."
        self.assertEqual(material_work(source, classified()), {"blower motor", "capacitor"})
        final = self.finalize(blower(), source)
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "ABSENT")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_affirmative_capacitor_failure_activates_independent_analysis(self):
        for finding in ("45 µF ±6% capacitor measured 18 µF.", "Capacitor inspected and found ruptured."):
            with self.subTest(finding=finding):
                source = "Replace blower motor.\n" + finding
                self.assertEqual(material_work(source, classified()), {"blower motor", "capacitor"})
                self.assertTrue(ec.electrical_required(source))
                final = self.finalize(blower(), source)
                self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONFIRMED")
                self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_missing_or_normal_test_does_not_suppress_separate_failure(self):
        for reference in (*MISSING[:2], *NORMAL):
            for separator in ("\n", "; "):
                with self.subTest(reference=reference, separator=separator):
                    source = "Replace blower motor.\n" + reference.rstrip(".") + separator + "Capacitor inspected and found ruptured."
                    final = self.finalize(blower(), source)
                    self.assertEqual(material_work(source, classified()), {"blower motor", "capacitor"})
                    items = ec.electrical_items(final)
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0].diagnostic_evidence_status, "CONFIRMED")
                    self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_explicit_replacement_with_normal_test_keeps_conflict(self):
        final = self.finalize(blower(), "Replace blower motor.\nReplace capacitor.\n45 µF ±6% capacitor measured 44.8 µF.")
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONTRADICTORY")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_blower_support_boundaries_and_supporting_evidence_survive(self):
        findings = ["Blower motor power input measured 240 VAC.", "Blower motor winding tested open."]
        for status, scope, support, evidence in (
                ("ABSENT", "UNSUPPORTED", "UNSUPPORTED", []),
                ("INCOMPLETE", "PARTIALLY_DEFINED", "PARTIALLY_SUPPORTED", findings[:1]),
                ("CONFIRMED", "APPROPRIATE", "SUPPORTED", findings)):
            with self.subTest(status=status):
                reference = "Capacitor tested normal."
                source = "Replace blower motor.\n" + "\n".join([*evidence, reference])
                raw = blower(status, scope, evidence)
                if evidence:
                    raw.technical_assessments[0].documented_evidence.append(reference)
                final = self.finalize(raw, source)
                self.assertEqual(final.decision.technical_support, support)
                self.assertEqual(ec.electrical_items(final), [])
                self.assertEqual(final.decision.pricing_transparency, "LIMITED")
                self.assertEqual(final.technical_assessments[0].documented_evidence,
                                 raw.technical_assessments[0].documented_evidence)


if __name__ == "__main__":
    unittest.main()
