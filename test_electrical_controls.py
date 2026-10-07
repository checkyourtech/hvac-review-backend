"""Phase 2J: offline source-to-assessment-to-upload report boundaries."""
import contextlib
import html
import io
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
import electrical_controls as ec
from compressor import compressor_items, compressor_paragraphs, finalize_compressor_fields
from test_technical_support_derivation import assessment, analysis_with


CASES = {
    "electrical_controls_capacitor_good_test.txt": ("capacitor", "CONFIRMED", "SUPPORTED", 0),
    "electrical_controls_capacitor_bad_test.txt": ("capacitor", "CONTRADICTORY", "UNSUPPORTED", 1),
    "electrical_capacitor_contradictory_test.txt": ("capacitor", "CONTRADICTORY", "UNSUPPORTED", 1),
    "electrical_controls_board_good_test.txt": ("control board", "CONFIRMED", "SUPPORTED", 0),
    "electrical_controls_board_partial_test.txt": ("control board", "INCOMPLETE", "PARTIALLY_SUPPORTED", 0),
    "electrical_controls_board_bad_test.txt": ("control board", "ABSENT", "UNSUPPORTED", 1),
    "electrical_controls_contactor_bad_test.txt": ("contactor", "ABSENT", "UNSUPPORTED", 1),
    "electrical_controls_contactor_partial_test.txt": ("contactor", "INCOMPLETE", "PARTIALLY_SUPPORTED", 0),
    "electrical_controls_contactor_good_test.txt": ("contactor", "CONFIRMED", "SUPPORTED", 0),
    "electrical_controls_pressure_partial_test.txt": ("pressure switch", "INCOMPLETE", "PARTIALLY_SUPPORTED", 0),
    "electrical_pressure_switch_good_test.txt": ("pressure switch", "CONFIRMED", "SUPPORTED", 0),
    "electrical_pressure_switch_bad_test.txt": ("pressure switch", "ABSENT", "UNSUPPORTED", 1),
    "electrical_flame_sensor_good_test.txt": ("flame sensor", "CONFIRMED", "SUPPORTED", 0),
    "electrical_flame_sensor_bad_test.txt": ("flame sensor", "INCOMPLETE", "PARTIALLY_SUPPORTED", 0),
    "electrical_igniter_good_test.txt": ("igniter", "CONFIRMED", "SUPPORTED", 0),
    "electrical_igniter_bad_test.txt": ("igniter", "ABSENT", "UNSUPPORTED", 1),
    "electrical_capacitor_good_test.txt": ("capacitor", "CONFIRMED", "SUPPORTED", 0),
    "electrical_control_board_bad_test.txt": ("control board", "ABSENT", "UNSUPPORTED", 1),
}

INVENTED = ("237 volts", "26.8 control volts", "9.6 amps", "14.2 ohms", "91 µF", "±12%",
            "continuity passed at pin Q9", "board output at pin Q9 present", "fault code FAKE99",
            "switch closed at 4.1", "8.3 microamps", "igniter current 6.7", "manufacturer criterion FAKE77",
            "ruptured transformer housing", "MODEL-FAKE42", "19-year warranty", "startup test passed at 111 CFM")


def classified(modules=None):
    return main.QuoteClassification(quote_type="repair", system_type="unknown", primary_scope="Component repair",
                                    modules_required=modules or [])


def raw(component=None, malicious=False):
    a = analysis_with([], ai_support="SUPPORTED", pricing="LIMITED")
    if malicious:
        invented = ". ".join(INVENTED)
        a.technical_assessments = [assessment("CONFIRMED", subject=ec.PREFIX + component, evidence=[invented])]
        for name in ("project_overview", "equipment_analysis", "missing_information", "installation_concerns",
                     "pricing_review", "quote_comparison", "best_quote_recommendation", "contractor_vetting"):
            setattr(a, name, invented)
        a.good_signs = [invented]
        a.red_flags = [invented]
        a.contractor_questions = [f"What about {s}?" for s in INVENTED]
        a.decision.required_actions = [invented]
        a.decision.verdict_reasons = [invented]
        a.decision.optional_suggestions = [invented]
    return a


def mixed_capacitor_raw():
    """Live-like partial assessment, including evidence attached to the wrong owner."""
    a = raw()
    a.technical_assessments = [assessment(
        "INCOMPLETE", "PARTIALLY_DEFINED", subject="Claimed compressor failure",
        evidence=["Technician found the outdoor unit not running.",
                  "Technician tested the dual run capacitor and found it failed.",
                  "45/5 MFD", "Compressor/HERM: 18.6 MFD", "Fan: 2.1 MFD"],
        gaps=["Compressor-specific failure has not been confirmed."])]
    a.installation_concerns = (
        "Capacitor and compressor replacement are included. "
        "A critical step, testing with a known-good capacitor, was not documented.")
    a.contractor_questions = [
        "How will you verify the operational effectiveness of the new components after installation?",
        "What capacitance did the capacitor measure?",
    ]
    return a


class ElectricalControlsTests(unittest.TestCase):
    def final(self, text, original=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.finalize_customer_analysis(original or raw(), text, 1, classified())

    def check_case(self, final, filename):
        component, status, support, flags = CASES[filename]
        items = ec.electrical_items(final)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].subject, ec.PREFIX + component)
        self.assertEqual(items[0].materiality, "PRIMARY")
        self.assertEqual(items[0].diagnostic_evidence_status, status)
        self.assertEqual(final.decision.technical_support, support)
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION" if support == "UNSUPPORTED" else "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
        self.assertEqual(len(final.red_flags), flags)
        self.assertEqual(len(final.contractor_questions), 1 if support == "SUPPORTED" else 2)
        self.assertEqual(main.contractor_question_category(final.contractor_questions[-1]), "pricing")
        if support != "SUPPORTED":
            self.assertTrue(main.contractor_question_category(final.contractor_questions[0]).startswith("electrical_"))
        self.assertIn("What Does the Electrical Evidence Show?", main.build_report_html(final, 1))

    def test_registry_and_prompt(self):
        self.assertEqual(main.ANALYSIS_MODULES[main.AnalysisModule.ELECTRICAL_CONTROLS], ec.ELECTRICAL_CONTROLS_RULES)
        for text in ("PRIMARY", "ABSENT", "INCOMPLETE", "CONTRADICTORY", "not a universal checklist", "MOTORS", "COMPRESSOR"):
            self.assertIn(text, ec.ELECTRICAL_CONTROLS_RULES)
        self.assertNotIn("BLOWER MOTOR AND ECM REPAIRS", ec.ELECTRICAL_CONTROLS_RULES)

    def test_source_recovery_all_component_boundaries(self):
        for name in CASES:
            with self.subTest(fixture=name):
                self.check_case(self.final(Path(name).read_text()), name)

    def test_production_upload_all_fixtures_with_omissions_and_invented_ai_fields(self):
        for name, (component, _, _, _) in CASES.items():
            text = Path(name).read_text()
            for malicious in (False, True):
                with self.subTest(fixture=name, malicious=malicious):
                    original = raw(component, malicious)
                    before = original.model_dump()
                    results = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                               for a in (classified(), original)]
                    with TemporaryDirectory(prefix="cyt-electrical-") as upload_dir, patch.object(main, "UPLOAD_DIR", Path(upload_dir)), contextlib.redirect_stdout(io.StringIO()), patch.object(
                            main.client.beta.chat.completions, "parse", side_effect=results) as api, patch.object(main, "send_review_email") as email:
                        response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                            files={"files": (name, text.encode(), "text/plain")})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(api.call_count, 2)
                    final = email.call_args.kwargs["analysis"]
                    self.check_case(final, name)
                    self.assertEqual(response.text, main.build_report_html(final, 1))
                    self.assertEqual(original.model_dump(), before)
                    for token in INVENTED:
                        self.assertNotIn(token, html.unescape(response.text))
                        self.assertNotIn(token, str(final.model_dump()))

    def test_routing_recovers_omitted_module(self):
        text = Path("electrical_controls_board_bad_test.txt").read_text()
        parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=classified()))])
        with patch.object(main.client.beta.chat.completions, "parse", return_value=parsed):
            result = main.classify_quotes(text)
        self.assertIn(main.AnalysisModule.ELECTRICAL_CONTROLS, result.modules_required)
        self.assertIn(ec.ELECTRICAL_CONTROLS_RULES.strip(), main.get_analysis_knowledge(classified(), text))

    def test_motor_firewall_routing_and_finalization(self):
        for motor in ("blower motor", "condenser fan motor", "inducer motor", "ECM module"):
            with self.subTest(motor=motor):
                text = f"Replace {motor} — $1,250.\nCorrect voltage at motor; motor does not run."
                self.assertFalse(ec.electrical_required(text))
                original = analysis_with([assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=f"{motor} failure")])
                final = self.final(text, original)
                self.assertEqual(ec.electrical_items(final), [])
                self.assertNotIn("What Does the Electrical Evidence Show?", main.build_report_html(final, 1))
                knowledge = main.get_analysis_knowledge(classified([main.AnalysisModule.ELECTRICAL_CONTROLS, main.AnalysisModule.MOTORS]), text)
                self.assertNotIn(ec.ELECTRICAL_CONTROLS_RULES.strip(), knowledge)
                parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=classified([main.AnalysisModule.ELECTRICAL_CONTROLS])))])
                with patch.object(main.client.beta.chat.completions, "parse", return_value=parsed):
                    result = main.classify_quotes(text)
                self.assertIn(main.AnalysisModule.MOTORS, result.modules_required)
                self.assertNotIn(main.AnalysisModule.ELECTRICAL_CONTROLS, result.modules_required)

    def test_compressor_winding_firewall(self):
        text = "Replace compressor — $4,950.\nCompressor leads isolated.\nTesting directly at compressor terminals shows short to ground."
        final = self.final(text)
        self.assertEqual(ec.electrical_items(final), [])
        self.assertEqual(compressor_items(final)[0].diagnostic_evidence_status, "CONFIRMED")

    def test_failed_capacitor_does_not_prove_compressor_failure(self):
        final = self.final(Path("compressor_capacitor_boundary_test.txt").read_text())
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONFIRMED")
        self.assertEqual(compressor_items(final)[0].diagnostic_evidence_status, "ABSENT")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_compressor_failure_does_not_prove_capacitor_failure(self):
        text = "Replace compressor and replace capacitor.\nCompressor leads isolated.\nTesting directly at compressor terminals shows short to ground."
        final = self.final(text)
        self.assertEqual(compressor_items(final)[0].diagnostic_evidence_status, "CONFIRMED")
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "ABSENT")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_compressor_question_can_mention_starting_capacitor(self):
        question = "What testing shows compressor failure rather than a capacitor problem?"
        self.assertEqual(main.contractor_question_category(question), "compressor_evidence")

    def test_independent_material_warranty_question_is_not_control_evidence(self):
        original = raw()
        original.technical_assessments = [assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            materiality="MATERIAL_SECONDARY", subject="Warranty coverage", gaps=["Warranty exclusion conflicts with the stated coverage."])]
        original.contractor_questions = ["What warranty applies to the capacitor given the stated exclusion?"]
        final = self.final(Path("electrical_controls_capacitor_good_test.txt").read_text(), original)
        self.assertIn("warranty", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_physical_capacitor_failure_without_numeric_checklist(self):
        final = self.final("Replace capacitor.\nCapacitor inspected and found ruptured.")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_capacitor_tolerance_is_not_universal(self):
        for tolerance, expected in (("6", "CONFIRMED"), ("20", "CONTRADICTORY")):
            final = self.final(f"Replace capacitor.\n45 µF ±{tolerance}% capacitor measured 40 µF.")
            self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, expected)

    def test_proposed_test_is_not_a_result(self):
        final = self.final("Replace capacitor.\nWill test whether 45 µF ±6% capacitor measured 18 µF.")
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "ABSENT")

    def test_board_power_alone_not_sufficient(self):
        final = self.final("Replace control board.\nControl board power is present but the unit does not work.")
        self.assertNotEqual(final.decision.technical_support, "SUPPORTED")

    def test_board_normal_operation_contradicts_failure(self):
        final = self.final("Replace control board.\nControl board required input and expected output both operate correctly.")
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONTRADICTORY")

    def test_switch_normal_state_contradicts_failure(self):
        final = self.final("Replace pressure switch.\nPressure switch changes state correctly under the required condition.")
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONTRADICTORY")

    def test_bare_sensor_and_igniter_are_absent(self):
        for component in ("flame sensor", "igniter"):
            final = self.final(f"Replace {component} — $500.")
            self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "ABSENT")
            self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_contactor_direct_behavior(self):
        final = self.final("Replace contactor.\nContactor has correct control voltage but does not close when commanded.")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_contactor_fixtures_have_focused_gaps_questions_and_findings(self):
        for state, question in (
            ("bad", "What testing showed that the contactor itself has failed?"),
            ("partial", "What control voltage was measured at the contactor coil during the call?"),
            ("good", None),
        ):
            with self.subTest(state=state):
                filename = f"electrical_controls_contactor_{state}_test.txt"
                final = self.final(Path(filename).read_text())
                self.check_case(final, filename)
                item = final.technical_assessments[0]
                self.assertEqual(len(final.technical_assessments), 1)
                self.assertEqual(len(final.good_signs), 1 if state == "good" else 0)
                self.assertEqual(len(item.material_gaps), 0 if state == "good" else 1)
                technical = [q for q in final.contractor_questions if main.contractor_question_category(q) != "pricing"]
                self.assertEqual(technical, [question] if question else [])
                if state == "partial":
                    self.assertEqual(item.material_gaps, [
                        "The quote does not confirm control voltage at the contactor coil during the call."])
                    self.assertEqual(final.missing_information, item.material_gaps[0])
                elif state == "good":
                    self.assertEqual(final.missing_information,
                                     "No important technical information is missing from the submitted diagnosis.")

    def test_contactor_symptoms_alone_do_not_isolate_failure(self):
        for symptom in ("The outdoor unit is not running.", "The system is not cooling.",
                        "Contactor does not pull in."):
            with self.subTest(symptom=symptom):
                final = self.final("Replace contactor.\n" + symptom)
                self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
                self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "ABSENT")
                self.assertEqual(len(final.red_flags), 1)
                self.assertEqual(final.good_signs, [])

    def test_contactor_call_and_failed_response_stay_partial_without_coil_voltage(self):
        source = Path("electrical_controls_contactor_partial_test.txt").read_text()
        for missing in ("Contactor coil control voltage has not been confirmed.",
                        "Contactor coil control voltage was not tested.",
                        "Contactor coil control voltage is absent.", ""):
            with self.subTest(missing=missing):
                text = source.replace("Contactor coil control voltage has not been confirmed.", missing)
                original = raw()
                original.technical_assessments = [assessment("CONFIRMED", subject=ec.PREFIX + "contactor",
                    evidence=["Thermostat call confirmed present.", "Contactor does not pull in during the call."])]
                final = self.final(text, original)
                self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
                self.assertEqual(final.red_flags, [])
                self.assertEqual(final.good_signs, [])
                self.assertEqual(final.contractor_questions[0],
                                 "What control voltage was measured at the contactor coil during the call?")

    def test_contactor_direct_input_and_switching_failure_support_repair(self):
        for behavior in ("Contactor did not pull in during the call.",
                         "Contactor does not close during the call.",
                         "Contactor contacts do not transfer during the call.",
                         "Contactor switched output is absent during the call."):
            with self.subTest(behavior=behavior):
                source = Path("electrical_controls_contactor_good_test.txt").read_text().replace(
                    "Contactor did not pull in during the call.", behavior)
                final = self.final(source)
                self.assertEqual(final.decision.technical_support, "SUPPORTED")
                self.assertEqual(final.red_flags, [])
                self.assertEqual(len(final.good_signs), 1)
                self.assertEqual(final.technical_assessments[0].material_gaps, [])
                self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["pricing"])

    def test_relay_uses_the_same_three_evidence_boundaries(self):
        for state in ("bad", "partial", "good"):
            with self.subTest(state=state):
                source = Path(f"electrical_controls_contactor_{state}_test.txt").read_text()
                final = self.final(source.replace("Contactor", "Relay").replace("contactor", "relay"))
                expected = CASES[f"electrical_controls_contactor_{state}_test.txt"]
                self.assertEqual(final.decision.technical_support, expected[2])
                self.assertEqual(ec.electrical_items(final)[0].subject, ec.PREFIX + "relay")
                self.assertEqual(len(final.red_flags), expected[3])
                self.assertEqual(len(final.good_signs), 1 if state == "good" else 0)
                technical = [q for q in final.contractor_questions if main.contractor_question_category(q) != "pricing"]
                self.assertEqual(technical, [] if state == "good" else [
                    "What testing showed that the relay itself has failed?" if state == "bad" else
                    "What control voltage was measured at the relay coil during the call?"])

    def test_contactor_control_input_cannot_be_borrowed_or_invented(self):
        for voltage in (
            "Control board control voltage confirmed present during the call.",
            "Relay control voltage confirmed present at the coil during the call.",
            "Relay control voltage is present but the contactor does not pull in.",
            "Contactor line voltage confirmed present during the call.",
            "Will test whether contactor control voltage is present at the coil during the call.",
            "Example: contactor control voltage confirmed present at the coil during the call.",
        ):
            with self.subTest(voltage=voltage):
                final = self.final("Replace contactor.\nThermostat call confirmed present.\n"
                                   "Contactor does not pull in during the call.\n" + voltage)
                self.assertNotEqual(final.decision.technical_support, "SUPPORTED")
        text = ("QUOTE 1\nReplace contactor.\nThermostat call confirmed present.\n"
                "Contactor does not pull in during the call.\nQUOTE 2\n"
                "Contactor control voltage confirmed present at the coil during the call.")
        self.assertEqual(ec.electrical_source_facts(text), [])

    def test_contactor_pricing_remains_independent_of_technical_support(self):
        for state in ("bad", "partial", "good"):
            for pricing in ("LIMITED", "ADEQUATE"):
                with self.subTest(state=state, pricing=pricing):
                    source = Path(f"electrical_controls_contactor_{state}_test.txt").read_text()
                    if pricing == "ADEQUATE":
                        source = source.replace("Replace contactor — $650.",
                                                "Replace contactor.\nParts: $200.\nLabor: $450.\nTotal: $650.")
                    original = raw()
                    original.decision.pricing_transparency = pricing
                    final = self.final(source, original)
                    self.assertEqual(final.decision.technical_support,
                                     CASES[f"electrical_controls_contactor_{state}_test.txt"][2])
                    self.assertEqual(final.decision.pricing_transparency, pricing)
                    price_questions = [q for q in final.contractor_questions if main.contractor_question_category(q) == "pricing"]
                    self.assertEqual(len(price_questions), 1 if pricing == "LIMITED" else 0)
                    self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION" if state == "bad" else
                                     "PROCEED" if state == "good" and pricing == "ADEQUATE" else "REVIEW_BEFORE_APPROVING")
                    if state == "good" and pricing == "ADEQUATE":
                        self.assertEqual(final.contractor_questions, [])
                        self.assertNotIn("breakdown", final.homeowner_takeaway + final.bottom_line)

    def test_contactor_upload_copy_firewalls_and_deduplication(self):
        forbidden = ("24 VAC", "line voltage", "resistance", "amperage", "terminal R1", "burned contacts",
                     "pitted contacts", "welded contacts", "fault code E17", "compressor", "motor",
                     "refrigerant", "system sizing", "commissioning", "combustion")
        for state in ("bad", "partial", "good"):
            with self.subTest(state=state):
                filename = f"electrical_controls_contactor_{state}_test.txt"
                source = Path(filename).read_text()
                original = raw("contactor", malicious=True)
                invented = ". ".join(forbidden)
                original.technical_assessments[0].documented_evidence = [invented]
                original.good_signs = [invented, invented]
                original.contractor_questions += [
                    "What testing showed contactor failure?", "What voltage was measured at the contactor coil?",
                    "How will system commissioning be verified after the repair?",
                ]
                before = original.model_dump()
                responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                             for a in (classified(), original)]
                with TemporaryDirectory(prefix="cyt-contactor-") as upload_dir, patch.object(
                        main, "UPLOAD_DIR", Path(upload_dir)), contextlib.redirect_stdout(io.StringIO()), patch.object(
                        main.client.beta.chat.completions, "parse", side_effect=responses) as api, patch.object(main, "send_review_email") as email:
                    response = TestClient(main.app).post("/upload",
                        data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
                        files={"files": (filename, source.encode(), "text/plain")})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(api.call_count, 2)
                final = email.call_args.kwargs["analysis"]
                self.check_case(final, filename)
                self.assertEqual(len(final.technical_assessments), 1)
                self.assertEqual(compressor_items(final), [])
                self.assertEqual(main.refrigerant_items(final), [])
                self.assertEqual(main.sizing_assessments(final), [])
                self.assertEqual(len(final.good_signs), 1 if state == "good" else 0)
                report = html.unescape(response.text)
                for token in (*forbidden, *INVENTED, "How Will Startup Be Verified?", "Is the New System the Right Size?",
                              "What Does the Compressor Evidence Show?", "What Does the Refrigerant Evidence Show?"):
                    self.assertNotIn(token.lower(), report.lower())
                if state == "good":
                    explanation = (
                        "The technician confirmed that the contactor was receiving the control signal during the call, "
                        "but the contactor did not pull in as expected. Those findings support replacing the contactor.")
                    self.assertEqual(ec.electrical_paragraphs(final), [explanation])
                    self.assertEqual(report.count(explanation), 1)
                    for raw_fact in source.splitlines()[2:]:
                        self.assertNotIn(raw_fact, report)
                    self.assertEqual(final.technical_assessments[0].material_gaps, [])
                self.assertEqual(response.text, main.build_report_html(final, 1))
                self.assertEqual(original.model_dump(), before)
                self.assertEqual(self.final(source, final).model_dump(), final.model_dump())

    def test_contactor_routing_recovers_only_electrical_controls(self):
        for state in ("bad", "partial", "good"):
            with self.subTest(state=state):
                source = Path(f"electrical_controls_contactor_{state}_test.txt").read_text()
                parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=classified()))])
                with patch.object(main.client.beta.chat.completions, "parse", return_value=parsed):
                    result = main.classify_quotes(source)
                self.assertEqual(result.modules_required, [main.AnalysisModule.ELECTRICAL_CONTROLS])

    def test_contactor_polished_copy_preserves_only_submitted_measurements(self):
        source = Path("electrical_controls_contactor_good_test.txt").read_text()
        extra = "Contactor control voltage measured 26 volts at the coil during the call."
        final = self.final(source + "\n" + extra)
        evidence = final.technical_assessments[0].model_dump()
        paragraphs = " ".join(ec.electrical_paragraphs(final))
        self.assertEqual(paragraphs.count(extra), 1)
        self.assertIn(extra, final.technical_assessments[0].documented_evidence)
        self.assertEqual(final.technical_assessments[0].model_dump(), evidence)
        self.assertNotIn("24 VAC", paragraphs)
        for finding in ("welded contacts", "failed continuity test", "measured excessive voltage drop across closed contacts"):
            final = self.final(f"Replace contactor.\nContactor {finding}.")
            self.assertEqual(final.decision.technical_support, "SUPPORTED")
            self.assertNotIn("receiving the control signal", " ".join(ec.electrical_paragraphs(final)))

    def test_question_purpose_deduplication(self):
        original = raw()
        original.contractor_questions = ["What testing shows control board failure?", "What board input/output test supports replacing the control board?", "Can you itemize the price?"]
        final = self.final(Path("electrical_controls_board_partial_test.txt").read_text(), original)
        self.assertEqual(len(final.contractor_questions), 2)
        self.assertEqual(main.contractor_question_category(final.contractor_questions[0]), "electrical_control_board")
        self.assertEqual(main.contractor_question_category(final.contractor_questions[1]), "pricing")

    def test_two_independent_component_gaps_get_distinct_purposes(self):
        final = self.final("Replace control board and replace pressure switch.\nControl board fault code E17 recorded.\nPressure switch fault code PS2 recorded.")
        self.assertEqual(len(ec.electrical_items(final)), 2)
        categories = [main.contractor_question_category(q) for q in final.contractor_questions]
        self.assertIn("electrical_control_board", categories)
        self.assertIn("electrical_pressure_switch", categories)

    def test_supported_adequate_price_can_proceed(self):
        original = raw()
        original.decision.pricing_transparency = "ADEQUATE"
        text = "Replace capacitor.\n45 µF ±6% capacitor measured 18 µF.\nCapacitor: $200.\nLabor: $450.\nTotal: $650."
        final = self.final(text, original)
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.contractor_questions, [])

    def test_partial_board_missing_evidence_and_itemization_copy_through_upload(self):
        filename = "electrical_controls_board_partial_test.txt"
        source = Path(filename).read_text()
        original = raw()
        before = original.model_dump()
        responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                     for a in (classified(), original)]
        with TemporaryDirectory(prefix="cyt-board-partial-copy-") as upload_dir, patch.object(
                main, "UPLOAD_DIR", Path(upload_dir)), contextlib.redirect_stdout(io.StringIO()), patch.object(
                main.client.beta.chat.completions, "parse", side_effect=responses) as api, patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload",
                data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
                files={"files": (filename, source.encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(api.call_count, 2)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.technical_assessments), 1)
        self.assertEqual(final.missing_information,
                         "The quote does not show the input/output testing needed to confirm that the control board itself has failed.")
        self.assertEqual(final.pricing_review,
                         "The quoted total is $1,800, but the quote does not show how that amount is divided. "
                         "Ask for an itemized breakdown of the parts, labor, and other major charges before approving the work.")
        self.assertEqual(final.contractor_questions, [
            "What input and output testing showed that the control board itself has failed?",
            "Can you provide an itemized breakdown of the parts, labor, and other charges included in the $1,800 total?",
        ])
        report = html.unescape(response.text)
        for fact in ("E17", "power is present", "required call and expected output have not been tested",
                     "does not yet isolate it as the failed component"):
            self.assertIn(fact, report)
        self.assertNotIn("does not show what finding supports replacing the control board", report)
        self.assertNotIn("Ask what the total covers", report)
        for forbidden in ("internal cost", "markup", "wholesale", "hourly labor", "every nut", "every fitting"):
            self.assertNotIn(forbidden, final.pricing_review.lower())
        self.assertEqual(response.text, main.build_report_html(final, 1))
        self.assertEqual(original.model_dump(), before)

    def test_limited_or_absent_electrical_lump_sum_requests_major_charge_itemization(self):
        # This scope bypasses the single named-work total helper, exercising the
        # electrical presentation fallback without recalibrating its pricing state.
        source = Path("electrical_controls_board_partial_test.txt").read_text().replace(
            "Replace control board — $1,800.",
            "Replace the control board and reconnect its wiring.\nTotal repair price: $1,800.")
        for transparency in ("LIMITED", "ABSENT"):
            with self.subTest(transparency=transparency):
                original = raw()
                original.decision.pricing_transparency = transparency
                final = self.final(source, original)
                self.assertEqual(final.decision.pricing_transparency, transparency)
                self.assertIn("itemized breakdown of the parts, labor, and other major charges", final.pricing_review)
                self.assertIn("$1,800", final.pricing_review)
                self.assertEqual(sum(main.contractor_question_category(q) == "pricing" for q in final.contractor_questions), 1)
                self.assertNotIn("Ask what", final.pricing_review)

    def test_adequate_partial_board_pricing_copy_is_unchanged(self):
        source = Path("electrical_controls_board_partial_test.txt").read_text().replace(
            "Replace control board — $1,800.",
            "Replace control board.\nParts: $1,000.\nLabor: $800.\nTotal repair price: $1,800.")
        original = raw()
        original.decision.pricing_transparency = "ADEQUATE"
        final = self.final(source, original)
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(final.pricing_review, "Parts: $1,000. Labor: $800. Total repair price: $1,800.")
        self.assertEqual(final.contractor_questions,
                         ["What input and output testing showed that the control board itself has failed?"])
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.red_flags, [])

    def test_supported_board_plain_language_through_upload_preserves_decision_and_evidence(self):
        filename = "electrical_controls_board_good_test.txt"
        source = Path(filename).read_text()
        original = raw()
        before = original.model_dump()
        # The pre-copy path is a baseline for every non-presentation field.
        with patch.object(ec, "_supported_board_input_output_copy", return_value=None):
            baseline = self.final(source, original)
        responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                     for a in (classified(), original)]
        with TemporaryDirectory(prefix="cyt-board-good-copy-") as upload_dir, patch.object(
                main, "UPLOAD_DIR", Path(upload_dir)), contextlib.redirect_stdout(io.StringIO()), patch.object(
                main.client.beta.chat.completions, "parse", side_effect=responses) as api, patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload",
                data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
                files={"files": (filename, source.encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(api.call_count, 2)
        final = email.call_args.kwargs["analysis"]
        changed_copy = {"good_signs", "homeowner_takeaway", "bottom_line"}
        self.assertEqual(final.model_dump(exclude=changed_copy), baseline.model_dump(exclude=changed_copy))
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.good_signs), 1)
        self.assertEqual(len(final.technical_assessments), 1)
        self.assertEqual(final.technical_assessments[0].documented_evidence, [
            "Control board required power and voltage confirmed present.",
            "Control board thermostat call confirmed present.",
            "Control board expected output measured absent during the call.",
        ])
        self.assertEqual(final.contractor_questions, [
            "Can you provide an itemized breakdown of the parts, labor, and other charges included in the $1,800 total?",
        ])
        report = html.unescape(response.text)
        electrical = html.unescape(re.search(
            r"<h2>What Does the Electrical Evidence Show\?</h2>(.*?)</div>", response.text, re.S).group(1))
        for fact in ("confirmed power to the control board", "thermostat was calling for operation",
                     "expected output from the board was absent", "support replacing the control board"):
            self.assertIn(fact, electrical)
        for awkward in ("One documented technical strength:", "Control board required power",
                        "Control board thermostat call", "Control board expected output", "clearer breakdown"):
            self.assertNotIn(awkward, report)
        self.assertIn("board had power and received the thermostat call", final.homeowner_takeaway)
        self.assertIn("expected output was missing", final.homeowner_takeaway)
        for text in (final.homeowner_takeaway, final.bottom_line):
            self.assertIn("itemized breakdown of the $1,800 total", text)
        self.assertIn("power at the board", final.good_signs[0])
        self.assertIn("thermostat call reaching the board", final.good_signs[0])
        self.assertIn("expected board output being absent", final.good_signs[0])
        customer_copy = " ".join([final.homeowner_takeaway, final.bottom_line, *final.good_signs,
                                  *ec.electrical_paragraphs(final)])
        self.assertEqual(set(re.findall(r"\d[\d,.]*", customer_copy)), {"1,800"})
        self.assertNotRegex(customer_copy.lower(), r"terminal|fault code|\d+\s*(?:volts?|amps?|ohms?)")
        for invented in INVENTED:
            self.assertNotIn(invented, customer_copy)
        self.assertEqual(response.text, main.build_report_html(final, 1))
        self.assertEqual(original.model_dump(), before)
        self.assertEqual(self.final(source, final).model_dump(), final.model_dump())

    def test_supported_board_readability_preserves_additional_submitted_measurements(self):
        source = Path("electrical_controls_board_good_test.txt").read_text()
        extra = "Control board voltage measured 120 volts at terminal R1."
        final = self.final(source + "\n" + extra)
        evidence_before = [a.model_dump() for a in final.technical_assessments]
        paragraphs = ec.electrical_paragraphs(final)
        self.assertIn(extra, " ".join(paragraphs))
        self.assertIn(extra, final.technical_assessments[0].documented_evidence)
        self.assertEqual([a.model_dump() for a in final.technical_assessments], evidence_before)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_other_supported_board_failure_does_not_invent_input_output_checks(self):
        source = ("Replace control board — $1,800.\n"
                  "The control board failed the submitted manufacturer rejection criterion C7.")
        final = self.final(source)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertIsNone(ec._supported_board_input_output_copy(final.technical_assessments[0]))
        evidence = " ".join(ec.electrical_paragraphs(final))
        self.assertIn("C7", evidence)
        for invented in ("confirmed power", "thermostat call", "output was missing", "output being absent"):
            self.assertNotIn(invented, evidence + " ".join(final.good_signs))

    def test_partial_and_bad_board_presentation_is_unchanged(self):
        for filename in ("electrical_controls_board_partial_test.txt", "electrical_controls_board_bad_test.txt"):
            with self.subTest(filename=filename):
                source = Path(filename).read_text()
                with patch.object(ec, "_supported_board_input_output_copy", return_value=None):
                    baseline = self.final(source)
                    baseline_paragraphs = ec.electrical_paragraphs(baseline)
                final = self.final(source)
                self.assertEqual(final.model_dump(), baseline.model_dump())
                self.assertEqual(ec.electrical_paragraphs(final), baseline_paragraphs)

    def test_supported_board_adequate_price_still_proceeds_without_questions(self):
        source = Path("electrical_controls_board_good_test.txt").read_text().replace(
            "Replace control board — $1,800.",
            "Replace control board.\nParts: $1,000.\nLabor: $800.\nTotal repair price: $1,800.")
        original = raw()
        original.decision.pricing_transparency = "ADEQUATE"
        final = self.final(source, original)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.contractor_questions, [])
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.good_signs), 1)
        self.assertNotIn("breakdown", final.homeowner_takeaway + final.bottom_line)

    def test_good_board_upload_rejects_sizing_contamination_and_duplicate_owner(self):
        filename = "electrical_controls_board_good_test.txt"
        source = Path(filename).read_text()
        baseline = self.final(source)
        for scope, parts, modules in (
            ("Control board replacement", ["Control Board"], [main.AnalysisModule.SYSTEM_SIZING]),
            ("Replace control board", ["Control Board"], []),
            ("Furnace replacement", ["furnace"], [main.AnalysisModule.SYSTEM_SIZING]),
            ("Component repair", [], []),
        ):
            with self.subTest(scope=scope, modules=modules):
                c = main.QuoteClassification(quote_type="replacement", system_type="furnace",
                    primary_scope=scope, replacement_components=parts, modules_required=modules)
                original = raw()
                board_evidence = source.splitlines()[2:]
                original.technical_assessments = [
                    assessment("CONFIRMED", subject=ec.PREFIX + "Control Board", evidence=board_evidence),
                    assessment("CONFIRMED", subject=ec.PREFIX + "control board", evidence=board_evidence),
                    assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=main.SIZING_SUBJECT,
                               gaps=["The system size still needs clarification before approval."]),
                ]
                original.equipment_analysis = (
                    "The documented electrical findings support replacing the Control Board. "
                    "The documented electrical findings support replacing the control board.")
                original.good_signs = [" ".join(board_evidence), baseline.good_signs[0]]
                original.missing_information = "The system size still needs clarification before approval."
                original.installation_concerns = "Test the board after replacement to make sure the repair works."
                original.contractor_questions = [
                    "How did you determine the size of the new system?",
                    "How will you verify the repair after installation?",
                ]
                original.decision.required_actions = ["Confirm the new system size before approving installation."]
                before = original.model_dump()
                responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                             for a in (c, original)]
                with TemporaryDirectory(prefix="cyt-board-scope-") as upload_dir, patch.object(
                        main, "UPLOAD_DIR", Path(upload_dir)), contextlib.redirect_stdout(io.StringIO()), patch.object(
                        main.client.beta.chat.completions, "parse", side_effect=responses) as api, patch.object(main, "send_review_email") as email:
                    response = TestClient(main.app).post("/upload",
                        data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
                        files={"files": (filename, source.encode(), "text/plain")})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(api.call_count, 2)
                self.assertNotIn(main.AnalysisModule.SYSTEM_SIZING, c.modules_required)
                self.assertNotIn(main.SYSTEM_SIZING_RULES.strip(), str(api.call_args_list[1].kwargs["messages"]))
                final = email.call_args.kwargs["analysis"]
                self.assertEqual(final.model_dump(), baseline.model_dump())
                self.assertEqual(final.decision.technical_support, "SUPPORTED")
                self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
                self.assertEqual(final.decision.pricing_transparency, "LIMITED")
                self.assertEqual(final.red_flags, [])
                self.assertEqual(len(final.good_signs), 1)
                self.assertEqual(main.sizing_assessments(final), [])
                self.assertEqual(main.commissioning_items(final), [])
                self.assertEqual(len(ec.electrical_items(final)), 1)
                self.assertEqual(final.technical_assessments[0].documented_evidence, board_evidence)
                self.assertEqual(final.equipment_analysis, "The documented electrical findings support replacing the control board.")
                self.assertEqual(final.contractor_questions, [
                    "Can you provide an itemized breakdown of the parts, labor, and other charges included in the $1,800 total?",
                ])
                report = html.unescape(response.text)
                for forbidden in ("Is the New System the Right Size?", "How did you determine the size",
                                  "system size still needs", "Get the sizing results", "Control board required power",
                                  "Test the board after replacement", "How Will Startup Be Verified?"):
                    self.assertNotIn(forbidden, report)
                self.assertEqual(len(ec.electrical_paragraphs(final)), 1)
                self.assertEqual(report.count("Those findings support replacing the control board."), 1)
                self.assertIn("No important technical information is missing", final.missing_information)
                self.assertIn("itemized breakdown", final.pricing_review)
                self.assertEqual(original.model_dump(), before)
                self.assertEqual(response.text, main.build_report_html(final, 1))
                self.assertEqual(self.final(source, final).model_dump(), final.model_dump())

    def test_electrical_owner_aliases_resolve_to_one_canonical_assessment(self):
        source = Path("electrical_controls_board_good_test.txt").read_text()
        for subject in ("Electrical control evidence: Control Board", "ELECTRICAL CONTROL EVIDENCE: CONTROL BOARD",
                        "Electrical control evidence: control-board", "Electrical control evidence: Control Board failure"):
            with self.subTest(subject=subject):
                original = raw()
                original.technical_assessments = [assessment("CONFIRMED", subject=subject, evidence=source.splitlines()[2:])]
                final = self.final(source, original)
                self.assertEqual(len(final.technical_assessments), 1)
                self.assertEqual(final.technical_assessments[0].subject, ec.PREFIX + "control board")
                self.assertEqual(len(final.good_signs), 1)
                self.assertEqual(len(ec.electrical_paragraphs(final)), 1)

    def test_partial_bad_board_with_irrelevant_sizing_keep_existing_boundaries(self):
        for name in ("partial", "bad"):
            with self.subTest(case=name):
                source = Path(f"electrical_controls_board_{name}_test.txt").read_text()
                original = raw()
                original.technical_assessments = [assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=main.SIZING_SUBJECT)]
                final = self.final(source, original)
                self.assertEqual(final.model_dump(), self.final(source).model_dump())
                self.assertEqual(main.sizing_assessments(final), [])

    def test_raw_unchanged_and_finalization_idempotent(self):
        text = Path("electrical_controls_capacitor_good_test.txt").read_text()
        original = raw()
        before = original.model_dump()
        first = self.final(text, original)
        second = self.final(text, first)
        self.assertEqual(original.model_dump(), before)
        self.assertEqual(first.model_dump(), second.model_dump())

    def test_no_cross_quote_capacitance_join(self):
        text = "QUOTE 1\nReplace capacitor.\n45 µF ±6% capacitor.\nQUOTE 2\nCapacitor measured 18 µF."
        self.assertEqual(ec.electrical_source_facts(text), [])

    def test_negated_or_hypothetical_results_do_not_confirm_failure(self):
        for text in (
            "Replace igniter.\nIgniter visually inspected and not cracked.",
            "Replace igniter.\nIgniter resistance tested not open.",
            "Replace control board.\nNo confirmed board damage tied to output failure.",
            "Replace control board.\nControl board required power and voltage confirmed present.\nControl board required call not present.\nControl board expected output absent.",
            "Replace capacitor.\nExample: 45 µF ±6% capacitor measured 18 µF.",
            "Replace flame sensor.\nBurners do not ignite normally.\nFlame signal measured at 0.4 microamps DC.\nManufacturer minimum flame signal is 1.5 microamps DC.",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(self.final(text).decision.technical_support, "SUPPORTED")

    def test_ai_supported_fault_code_alone_is_not_preserved(self):
        text = "Replace control board.\nControl board fault code E17 recorded."
        original = raw()
        original.technical_assessments = [assessment("CONFIRMED", subject=ec.PREFIX + "control board",
                                                   evidence=["Control board fault code E17 recorded."])]
        self.assertEqual(self.final(text, original).decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_weak_capacitor_without_reading_is_partial(self):
        final = self.final("Replace capacitor.\nCapacitor described as weak.")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.red_flags, [])

    def test_intermittent_igniter_is_partial(self):
        final = self.final("Replace igniter.\nIgniter operation is intermittent.")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_signal_evidence_does_not_require_cleaning_universally(self):
        final = self.final("Replace flame sensor.\nBurners ignite normally.\nFlame signal measured at 0.4 microamps DC.\nManufacturer minimum flame signal is 1.5 microamps DC.")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_explicit_manufacturer_rejection_evidence(self):
        for component in ("igniter", "control board", "limit switch"):
            text = f"Replace {component}.\nThe {component} failed the submitted manufacturer rejection criterion C7."
            with self.subTest(component=component):
                final = self.final(text)
                self.assertEqual(final.decision.technical_support, "SUPPORTED")
                self.assertIn("C7", " ".join(ec.electrical_paragraphs(final)))

    def test_component_name_or_generic_no_heat_does_not_prove_failure(self):
        for component in ("control board", "capacitor", "pressure switch", "igniter"):
            final = self.final(f"System will not start. Replace {component}.")
            self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_independent_control_problem_with_motor_is_retained(self):
        text = "Replace blower motor and replace capacitor.\n45 µF ±6% capacitor measured 18 µF."
        original = analysis_with([assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject="Blower motor failure")])
        final = self.final(text, original)
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONFIRMED")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_bare_board_has_no_unrelated_sections_or_invented_readings(self):
        final = self.final(Path("electrical_controls_board_bad_test.txt").read_text())
        report = main.build_report_html(final, 1)
        for heading in ("How Will Startup Be Verified?", "Is the New System the Right Size?",
                        "What Does the Compressor Evidence Show?", "What Does the Refrigerant Evidence Show?"):
            self.assertNotIn(heading, report)
        self.assertEqual(final.good_signs, [])
        for word in ("volts", "amps", "ohms", "fault code", "microamps", "warranty"):
            self.assertNotIn(word, " ".join(ec.electrical_paragraphs(final)).lower())

    def test_compressor_start_capacitor_is_not_compressor_replacement(self):
        text = "Replace compressor start capacitor.\n45 µF ±6% capacitor measured 18 µF."
        original = raw()
        original.technical_assessments = [assessment("ABSENT", "UNSUPPORTED", subject="Claimed compressor failure")]
        final = self.final(text, original)
        self.assertEqual(compressor_items(final), [])
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONFIRMED")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertFalse(main.compressor_required(text))

    def test_motor_capacitor_is_control_work_not_motor_replacement(self):
        text = "Replace blower motor run capacitor.\n45 µF ±6% capacitor measured 18 µF."
        self.assertFalse(ec.motor_work_required(text))
        self.assertTrue(ec.electrical_required(text))

    def test_documented_closed_contact_voltage_drop_supports_repair(self):
        final = self.final("Replace contactor.\nContactor measured excessive voltage drop across closed contacts.")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_normal_capacitance_does_not_erase_independent_physical_failure(self):
        final = self.final("Replace capacitor.\n45 µF ±6% capacitor measured 44.8 µF.\nCapacitor inspected and found ruptured.")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_partial_capacitor_question_does_not_reask_known_reading(self):
        final = self.final("Replace capacitor.\n45 µF capacitor measured 40 µF.")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertIn("tolerance", final.contractor_questions[0])
        self.assertNotIn("What capacitance", final.contractor_questions[0])

    def test_gross_capacitance_loss_does_not_require_invented_tolerance(self):
        for source in (
            "Replace capacitor.\n45 µF capacitor measured 18 µF.",
            "Replace dual run capacitor.\nCapacitor rating: 45/5 MFD.\nCompressor/HERM: 18.6 MFD\nFan: 2.1 MFD",
            "Replace dual run capacitor.\nCapacitor rating: 40/5 µF.\nFan: 4.9 µF\nHERM: 16 µF",
        ):
            with self.subTest(source=source):
                final = self.final(source)
                self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONFIRMED")
                self.assertEqual(final.decision.technical_support, "SUPPORTED")
                self.assertFalse(any(ec.electrical_question_purpose(q) for q in final.contractor_questions))
                electrical = " ".join(ec.electrical_paragraphs(final))
                self.assertNotIn("tolerance", electrical.lower())
                self.assertNotIn("±", electrical)
                self.assertNotIn("%", electrical)

    def test_no_tolerance_does_not_establish_normal_or_fail_for_modest_deviation(self):
        final = self.final("Replace capacitor.\n45 µF capacitor measured 44.8 µF.")
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(final.red_flags, [])

    def test_dual_readings_require_section_identity(self):
        source = "Replace dual run capacitor.\nCapacitor rating: 45/5 MFD.\nMeasured capacitance: 18.6 MFD."
        self.assertNotEqual(self.final(source).decision.technical_support, "SUPPORTED")

    def test_explicit_tolerance_takes_precedence_over_gross_loss_recovery(self):
        # Synthetic wide limit: the positive-only recovery must never silently
        # replace the actual submitted criterion with a default tolerance.
        final = self.final("Replace capacitor.\n45 µF ±70% capacitor measured 18 µF.")
        self.assertEqual(ec.electrical_items(final)[0].diagnostic_evidence_status, "CONTRADICTORY")

    def test_cross_module_capacitor_recovery_through_upload(self):
        filename = "electrical_capacitor_compressor_bad_test.txt"
        source = Path(filename).read_text()
        for stale_capacitor in (False, True):
            with self.subTest(stale_capacitor=stale_capacitor):
                original = raw()
                if stale_capacitor:
                    original.technical_assessments = [assessment(
                        "INCOMPLETE", "PARTIALLY_DEFINED", subject=ec.PREFIX + "capacitor",
                        gaps=["No capacitor findings support replacement."])]
                    original.technical_assessments.append(assessment(
                        "CONFIRMED", subject="Claimed compressor failure", evidence=[
                            "Technician states the failed capacitor indicates the compressor has likely been damaged and recommends replacing both the capacitor and compressor."
                        ]))
                    original.equipment_analysis = "The quote points toward the capacitor, but does not yet isolate it as the failed component."
                    original.missing_information = "The quote does not show what findings support replacing the capacitor."
                    original.red_flags = ["No capacitor test findings support replacement."]
                    original.contractor_questions = ["What capacitance did the capacitor measure, and what tolerance is listed on it?"]
                    original.decision.required_actions = ["Ask for the capacitor readings and tolerance."]
                    original.decision.verdict_reasons = ["The capacitor diagnosis is not supported."]
                before = original.model_dump()
                responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                             for a in (classified(), original)]
                with TemporaryDirectory(prefix="cyt-capacitor-cross-") as upload_dir, patch.object(
                        main, "UPLOAD_DIR", Path(upload_dir)), contextlib.redirect_stdout(io.StringIO()), patch.object(
                        main.client.beta.chat.completions, "parse", side_effect=responses), patch.object(main, "send_review_email") as email:
                    response = TestClient(main.app).post("/upload",
                        data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
                        files={"files": (filename, source.encode(), "text/plain")})
                self.assertEqual(response.status_code, 200)
                final = email.call_args.kwargs["analysis"]
                capacitor = ec.electrical_items(final)[0]
                compressor = compressor_items(final)[0]
                refrigerant = main.refrigerant_items(final)[0]
                self.assertEqual((capacitor.diagnostic_evidence_status, capacitor.scope_support), ("CONFIRMED", "APPROPRIATE"))
                self.assertEqual(main.derive_technical_support([capacitor]), "SUPPORTED")
                self.assertEqual((compressor.diagnostic_evidence_status, compressor.scope_support), ("ABSENT", "UNSUPPORTED"))
                self.assertEqual((refrigerant.diagnostic_evidence_status, refrigerant.scope_support), ("INCOMPLETE", "PARTIALLY_DEFINED"))
                self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
                self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
                self.assertEqual(final.decision.pricing_transparency, "LIMITED")
                self.assertEqual(len(final.red_flags), 1)
                self.assertIn("compressor", final.red_flags[0].lower())
                self.assertNotIn("capacitor", final.red_flags[0].lower())
                self.assertNotIn("capacitor", final.missing_information.lower())
                self.assertIn("compressor", final.missing_information.lower())
                self.assertIn("refrigerant", final.missing_information.lower())
                self.assertEqual(set(main.contractor_question_category(q) for q in final.contractor_questions),
                                 {"compressor_evidence", "refrigerant_evidence", "pricing"})
                for value in ("45/5 MFD", "18.6 MFD", "2.1 MFD"):
                    self.assertIn(value, " ".join(ec.electrical_paragraphs(final)))
                    self.assertIn(value, " ".join(final.good_signs))
                for stale in ("does not yet isolate", "The quote does not show what findings support replacing the capacitor.",
                              "What capacitance did", "known-good capacitor", "±6%", "6% tolerance"):
                    self.assertNotIn(stale, html.unescape(response.text))
                self.assertEqual(response.text, main.build_report_html(final, 1))
                self.assertEqual(original.model_dump(), before)
                again = self.final(source, final)
                self.assertEqual(again.model_dump(), final.model_dump())

    def test_true_capacitor_contradiction_has_no_other_diagnosis(self):
        final = self.final(Path("electrical_capacitor_contradictory_test.txt").read_text())
        self.check_case(final, "electrical_capacitor_contradictory_test.txt")
        self.assertEqual(len(final.technical_assessments), 1)
        self.assertEqual(final.good_signs, [])
        self.assertEqual(compressor_items(final), [])
        self.assertEqual(main.refrigerant_items(final), [])
        self.assertEqual(sum(ec.electrical_question_purpose(q) is not None for q in final.contractor_questions), 1)
        self.assertIn("within the listed tolerance", " ".join(ec.electrical_paragraphs(final)))

    def test_cross_module_preserves_independent_compressor_evidence(self):
        source = Path("electrical_capacitor_compressor_bad_test.txt").read_text()
        original = raw()
        original.technical_assessments = [assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            subject="Claimed compressor failure", evidence=["Technician found the outdoor unit not running."])]
        partial = self.final(source, original)
        self.assertEqual(compressor_items(partial)[0].diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(ec.electrical_items(partial)[0].diagnostic_evidence_status, "CONFIRMED")
        source += "\nCompressor leads isolated.\nTesting directly at compressor terminals shows short to ground."
        supported = self.final(source, original)
        self.assertEqual(compressor_items(supported)[0].diagnostic_evidence_status, "CONFIRMED")
        self.assertEqual(ec.electrical_items(supported)[0].diagnostic_evidence_status, "CONFIRMED")
        self.assertEqual(main.refrigerant_items(supported)[0].diagnostic_evidence_status, "INCOMPLETE")

    def test_partial_cross_module_presentation_through_upload(self):
        filename = "electrical_capacitor_compressor_bad_test.txt"
        source = Path(filename).read_text()
        original = mixed_capacitor_raw()
        before = original.model_dump()
        responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                     for a in (classified(), original)]
        with TemporaryDirectory(prefix="cyt-capacitor-ownership-") as upload_dir, patch.object(
                main, "UPLOAD_DIR", Path(upload_dir)), contextlib.redirect_stdout(io.StringIO()), patch.object(
                main.client.beta.chat.completions, "parse", side_effect=responses) as api, patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload",
                data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
                files={"files": (filename, source.encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(api.call_count, 2)
        final = email.call_args.kwargs["analysis"]
        capacitor = ec.electrical_items(final)[0]
        self.assertEqual((capacitor.diagnostic_evidence_status, capacitor.scope_support), ("CONFIRMED", "APPROPRIATE"))
        # Presentation must not rewrite the independently partial compressor facts.
        self.assertEqual(compressor_items(final)[0].model_dump(), original.technical_assessments[0].model_dump())
        refrigerant = main.refrigerant_items(final)[0]
        self.assertEqual((refrigerant.diagnostic_evidence_status, refrigerant.scope_support), ("INCOMPLETE", "PARTIALLY_DEFINED"))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(main.commissioning_items(final), [])
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions],
                         ["compressor_evidence", "refrigerant_evidence", "pricing"])
        self.assertIn("$4,850", final.contractor_questions[-1])
        self.assertNotIn("known-good", html.unescape(response.text))
        self.assertNotIn("operational effectiveness", response.text)
        self.assertNotIn("What capacitance", response.text)
        self.assertIn("compressor itself", final.installation_concerns)
        self.assertIn("Capacitor and compressor replacement are included.", final.installation_concerns)

        def section(heading):
            return html.unescape(re.search(r"<h2>" + re.escape(heading) + r"</h2>(.*?)</div>", response.text, re.S).group(1))

        electrical = section("What Does the Electrical Evidence Show?")
        compressor = section("What Does the Compressor Evidence Show?")
        for value in ("45/5 MFD", "18.6 MFD", "2.1 MFD"):
            self.assertIn(value, electrical)
            self.assertIn(value, " ".join(final.good_signs))
            self.assertNotIn(value, compressor)
        self.assertNotIn("tested the dual run capacitor", compressor)
        self.assertIn("does not show testing that confirms the compressor itself has failed", compressor)
        self.assertIn("capacitor readings support replacing the capacitor", final.homeowner_takeaway)
        self.assertIn("does not yet show that the compressor is damaged", final.homeowner_takeaway)
        self.assertIn("refrigerant work is needed", final.homeowner_takeaway)
        self.assertIn("capacitor replacement is supported", final.bottom_line)
        self.assertIn("Before approving the compressor and refrigerant work", final.bottom_line)
        self.assertNotIn("keep investigating", final.bottom_line)
        self.assertEqual(response.text, main.build_report_html(final, 1))
        self.assertEqual(original.model_dump(), before)
        self.assertEqual(self.final(source, final).model_dump(), final.model_dump())

    def test_mixed_repair_verification_question_requires_independent_gap(self):
        source = Path("electrical_capacitor_compressor_bad_test.txt").read_text()
        for question in (
            "How will you verify the operational effectiveness of the new components after installation?",
            "What startup checks will be completed and documented before the job is closed out?",
        ):
            with self.subTest(question=question):
                original = mixed_capacitor_raw()
                original.contractor_questions = [question]
                final = self.final(source, original)
                self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions],
                                 ["compressor_evidence", "refrigerant_evidence", "pricing"])
        # An actual independent startup scope/gap must retain its own question.
        source += "\nStartup included; checks and documentation not described."
        final = self.final(source, mixed_capacitor_raw())
        self.assertEqual(main.commissioning_items(final)[0].scope_support, "PARTIALLY_DEFINED")
        self.assertIn("commissioning", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_known_good_substitution_is_not_a_required_sequence(self):
        source = Path("electrical_capacitor_compressor_bad_test.txt").read_text()
        for mandate in (
            "A critical step, testing with a known-good capacitor, was not documented.",
            "Testing with a known good capacitor is required before confirming compressor failure.",
            "The compressor must be tested with a known-good capacitor.",
        ):
            with self.subTest(mandate=mandate):
                original = mixed_capacitor_raw()
                original.installation_concerns = mandate
                final = self.final(source, original)
                self.assertNotRegex(final.installation_concerns, r"known.good|critical|required|must")
                self.assertIn("compressor-specific testing", final.installation_concerns)
                self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_completed_substitution_and_independent_compressor_findings_remain(self):
        original = mixed_capacitor_raw()
        fact = "The compressor ran during testing with a known-good capacitor."
        original.installation_concerns = fact
        original.technical_assessments[0].documented_evidence.append(fact)
        before = [a.model_dump() for a in original.technical_assessments]
        finalize_compressor_fields(original)
        self.assertEqual(original.installation_concerns, fact)
        self.assertIn(fact, " ".join(compressor_paragraphs(original)))
        self.assertEqual([a.model_dump() for a in original.technical_assessments], before)

    def test_mandatory_capacitor_sequence_removed_through_upload_only_changes_installation(self):
        filename = "electrical_capacitor_compressor_bad_test.txt"
        source = Path(filename).read_text()
        baseline = self.final(source, raw())
        for mandate in (
            "Testing after replacing the capacitor is vital to ascertain compressor functionality before proceeding with a costly compressor replacement.",
            "The capacitor must be replaced first, followed by compressor testing.",
            "Compressor operation needs to be checked after a new capacitor is installed.",
            "A post-capacitor-replacement check is essential before approving the compressor.",
            "Before condemning the compressor, replace the capacitor and retest.",
            "Testing with a known-good capacitor is required before confirming compressor failure.",
        ):
            with self.subTest(mandate=mandate):
                original = raw()
                original.installation_concerns = (
                    mandate + " The scope lacks crucial verification steps to ensure effective repairs.")
                before = original.model_dump()
                responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                             for a in (classified(), original)]
                with TemporaryDirectory(prefix="cyt-capacitor-sequence-") as upload_dir, patch.object(
                        main, "UPLOAD_DIR", Path(upload_dir)), contextlib.redirect_stdout(io.StringIO()), patch.object(
                        main.client.beta.chat.completions, "parse", side_effect=responses) as api, patch.object(main, "send_review_email") as email:
                    response = TestClient(main.app).post("/upload",
                        data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
                        files={"files": (filename, source.encode(), "text/plain")})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(api.call_count, 2)
                final = email.call_args.kwargs["analysis"]
                self.assertEqual(final.model_dump(exclude={"installation_concerns"}),
                                 baseline.model_dump(exclude={"installation_concerns"}))
                self.assertEqual(final.installation_concerns,
                                 "The quote does not show compressor-specific testing that establishes the compressor itself has failed.")
                report = html.unescape(response.text)
                self.assertNotIn(mandate, report)
                self.assertNotIn("known-good capacitor", report)
                self.assertNotIn("crucial verification", report)
                self.assertNotIn("ensure effective repairs", report)
                self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
                self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
                self.assertEqual(len(final.red_flags), 1)
                self.assertEqual(len(final.good_signs), 1)
                self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions],
                                 ["compressor_evidence", "refrigerant_evidence", "pricing"])
                for value in ("45/5 MFD", "18.6 MFD", "2.1 MFD"):
                    self.assertIn(value, report)
                self.assertIn("capacitor replacement is supported", final.bottom_line)
                self.assertEqual(response.text, main.build_report_html(final, 1))
                self.assertEqual(original.model_dump(), before)
                self.assertEqual(self.final(source, final).model_dump(), final.model_dump())

    def test_completed_or_optional_capacitor_testing_and_factual_scope_are_preserved(self):
        source = Path("electrical_capacitor_compressor_bad_test.txt").read_text()
        for fact in (
            "The compressor was tested after the capacitor was replaced.",
            "One possible diagnostic method is testing with a known-good capacitor.",
            "The quoted scope includes capacitor and compressor replacement.",
        ):
            with self.subTest(fact=fact):
                original = raw()
                original.installation_concerns = fact
                self.assertEqual(self.final(source, original).installation_concerns, fact)

    def test_unsupported_mixed_repair_summary_preserves_second_opinion(self):
        source = Path("electrical_capacitor_compressor_bad_test.txt").read_text()
        original = raw()  # Existing completeness recovery: no compressor findings.
        original.contractor_questions = ["How will you verify the new components after installation?"]
        final = self.final(source, original)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(len(final.red_flags), 1)
        self.assertIn("second opinion", final.bottom_line)
        self.assertIn("capacitor replacement is supported", final.bottom_line)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions],
                         ["compressor_evidence", "refrigerant_evidence", "pricing"])


if __name__ == "__main__":
    unittest.main()
