"""Shared repair evidence: owner isolation, unchanged policy and offline HTML parity."""
import contextlib
import html
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
import electrical_controls as ec
import compressor as cp
import refrigerant_system as rs
from repair_diagnostics import REPAIR_DIAGNOSTIC_RULES, review_repair_evidence, apply_repair_evidence_guidance
from test_technical_support_derivation import analysis_with, assessment
from test_electrical_controls import CASES, classified, raw as electrical_raw
from test_compressor import raw as compressor_raw
from test_refrigerant_system import raw as refrigerant_raw


def finalize(raw, source="", classification=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw, source, 1, classification)


def upload(raw, source, classification=None):
    """The real extraction/classify/analyze/finalize/HTML path; only I/O mocked."""
    responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
                 for value in (classification or classified(), raw)]
    with TemporaryDirectory(prefix="cyt-shared-diagnostic-") as directory, patch.object(
            main, "UPLOAD_DIR", Path(directory)), contextlib.redirect_stdout(io.StringIO()), patch.object(
            main.client.beta.chat.completions, "parse", side_effect=responses) as api, patch.object(main, "send_review_email") as email:
        response = TestClient(main.app).post("/upload",
            data={"package": "tier1", "customer_name": "Offline", "customer_email": "offline@example.com"},
            files={"files": ("repair-evidence.txt", source.encode(), "text/plain")})
    assert response.status_code == 200, response.text
    assert api.call_count == 2
    return email.call_args.kwargs["analysis"], response.text


class RepairDiagnosticTests(unittest.TestCase):
    def assert_parity(self, final, report):
        before = final.model_dump()
        self.assertEqual(report, main.build_report_html(final, 1))
        self.assertEqual(before, final.model_dump())
        self.assertIn(final.equipment_analysis, html.unescape(report))
        for question in final.contractor_questions:
            self.assertIn(question, html.unescape(report))

    def test_shared_prompt_is_always_on_once_with_owner_examples(self):
        for module in main.AnalysisModule:
            with self.subTest(module=module), contextlib.redirect_stdout(io.StringIO()):
                knowledge = main.get_analysis_knowledge(classified([module]))
                self.assertEqual(knowledge.count(REPAIR_DIAGNOSTIC_RULES.strip()), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            for module, definitions in ((main.AnalysisModule.MOTORS, main.MOTOR_EVIDENCE_DEFINITIONS),
                                        (main.AnalysisModule.FURNACE_COMBUSTION, main.COMBUSTION_EVIDENCE_DEFINITIONS)):
                knowledge = main.get_analysis_knowledge(classified([module]))
                for definition in definitions.values():
                    self.assertIn(definition.expected_evidence, knowledge)

    def test_shared_review_consumes_assessment_without_mutating_policy(self):
        definitions = [cp.COMPRESSOR_EVIDENCE, *rs.REFRIGERANT_EVIDENCE.values(),
                       *main.MOTOR_EVIDENCE_DEFINITIONS.values(), *main.COMBUSTION_EVIDENCE_DEFINITIONS.values(),
                       main.HEAT_EXCHANGER_EVIDENCE]
        for name in ec.CONTROL_EVIDENCE_EXAMPLES:
            item = assessment("ABSENT", "UNSUPPORTED", subject=ec.PREFIX + name)
            definitions.append(ec.electrical_evidence_review(item).definition)
        for definition in definitions:
            for status, scope in (("ABSENT", "UNSUPPORTED"), ("INCOMPLETE", "PARTIALLY_DEFINED"), ("CONFIRMED", "APPROPRIATE")):
                with self.subTest(component=definition.component, status=status):
                    item = assessment(status, scope, subject=definition.component,
                                      evidence=["Submitted finding."], gaps=[] if status == "CONFIRMED" else ["Unresolved diagnosis."])
                    before = item.model_dump()
                    view = review_repair_evidence(item, definition, "Submitted finding.")
                    self.assertEqual(item.model_dump(), before)
                    self.assertEqual(view.documented_evidence, ("Submitted finding.",))
                    self.assertEqual(bool(view.contractor_question), status != "CONFIRMED")
                    self.assertEqual(view.supported, status == "CONFIRMED")
                    if status != "CONFIRMED":
                        self.assertIn("The contractor can support", view.evidence_explanation)
                        self.assertIn(definition.expected_evidence.rstrip("."), view.evidence_explanation)
                        self.assertNotIn("not a required checklist", view.evidence_explanation)
                    else:
                        self.assertEqual(view.evidence_explanation, "")

    def test_source_guard_does_not_invent_or_copy_unsubmitted_measurements(self):
        item = assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject="Blower motor failure",
            evidence=["Blower motor power is present.", "Blower motor measured 998 volts.", "Fault code FAKE777."])
        definition = main.MOTOR_EVIDENCE_DEFINITIONS["blower_motor"]
        view = review_repair_evidence(item, definition, "Blower motor power is present.")
        self.assertEqual(view.documented_evidence, ("Blower motor power is present.",))
        self.assertNotIn("998", view.evidence_explanation)
        self.assertNotIn("FAKE777", view.evidence_explanation)

    def test_future_tests_and_homeowner_instructions_are_not_evidence(self):
        instructions = ["You should measure the motor voltage with power on.",
                        "The homeowner can probe the gas valve terminals.",
                        "The contractor will test motor windings."]
        item = assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject="Blower motor failure", evidence=instructions)
        view = review_repair_evidence(item, main.MOTOR_EVIDENCE_DEFINITIONS["blower_motor"], "\n".join(instructions))
        self.assertEqual(view.documented_evidence, ())

    def test_duplicate_owner_does_not_hide_an_unresolved_assessment(self):
        definition = main.MOTOR_EVIDENCE_DEFINITIONS["blower_motor"]
        good = review_repair_evidence(assessment("CONFIRMED"), definition)
        bad = review_repair_evidence(assessment("ABSENT", "UNSUPPORTED"), definition)
        for records in ([good, bad], [bad, good]):
            a = analysis_with([])
            apply_repair_evidence_guidance(a, records, main.repair_diagnostic_question_owner)
            self.assertEqual(a.contractor_questions, [definition.question])
            self.assertEqual(len(a.red_flags), 1)

    def test_all_electrical_fixtures_keep_support_price_counts_and_upload_parity(self):
        for filename, (component, status, support, flags) in CASES.items():
            with self.subTest(fixture=filename):
                raw = electrical_raw(component, malicious=True)
                before = raw.model_dump()
                final, report = upload(raw, Path(filename).read_text())
                self.assertEqual(final.decision.technical_support, support)
                self.assertEqual(final.decision.pricing_transparency, "LIMITED")
                self.assertEqual(len(final.red_flags), flags)
                self.assertEqual(raw.model_dump(), before)
                diagnostic = [q for q in final.contractor_questions if main.repair_diagnostic_question_owner(q)]
                self.assertEqual(len(diagnostic), int(support != "SUPPORTED"))
                self.assertEqual(sum(main.contractor_question_category(q) == "pricing" for q in final.contractor_questions), 1)
                if support != "SUPPORTED":
                    evidence = " ".join(p for section in final.repair_evidence_sections for p in section.paragraphs)
                    self.assertIn(component, evidence)
                    self.assertNotIn("examples of contractor evidence", final.equipment_analysis)
                    self.assertNotIn("The contractor can support", final.equipment_analysis)
                self.assert_parity(final, report)

    def test_compressor_and_refrigerant_production_boundaries_are_unchanged(self):
        for module, factory, states in (
            ("compressor", compressor_raw, ("good", "partial", "bad")),
            ("refrigerant_system", refrigerant_raw, ("good", "partial", "bad")),
        ):
            for state in states:
                with self.subTest(module=module, state=state):
                    source = Path(f"{module}_{state}_test.txt").read_text()
                    raw = factory(state)
                    # Compare with the same production finalizer with only this
                    # new presentation pass omitted, not with a second policy.
                    with patch.object(main, "finalize_repair_diagnostic_guidance"):
                        baseline = finalize(raw, source)
                    final, report = upload(raw, source)
                    self.assertEqual(final.decision.model_dump(), baseline.decision.model_dump())
                    self.assertEqual(final.technical_assessments, baseline.technical_assessments)
                    self.assertEqual(final.red_flags, baseline.red_flags)
                    self.assertEqual(final.pricing_review, baseline.pricing_review)
                    self.assert_parity(final, report)

    def test_diagnostic_only_refrigerant_is_not_treated_as_a_quoted_repair(self):
        source = Path("refrigerant_system_partial_test.txt").read_text()
        final = finalize(refrigerant_raw("partial"), source)
        self.assertNotIn("examples of contractor evidence", final.equipment_analysis)
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("diagnostic visit", final.contractor_questions[0])
        self.assertEqual(final.red_flags, [])

    def test_partial_board_question_requests_only_missing_call_and_output(self):
        source = Path("electrical_controls_board_partial_test.txt").read_text()
        final = finalize(electrical_raw(), source)
        self.assertIn("power", " ".join(ec.electrical_paragraphs(final)))
        self.assertIn("E17", " ".join(ec.electrical_paragraphs(final)))
        question = final.contractor_questions[0]
        self.assertIn("thermostat call", question)
        self.assertIn("what output", question)
        self.assertNotIn("power", question)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_partial_transformer_and_contactor_do_not_reask_known_input(self):
        transformer = finalize(electrical_raw(), Path("electrical_controls_transformer_partial_test.txt").read_text())
        self.assertIn("secondary", transformer.contractor_questions[0])
        self.assertNotIn("What primary", transformer.contractor_questions[0])
        contactor = finalize(electrical_raw(), Path("electrical_controls_contactor_partial_test.txt").read_text())
        self.assertIn("at the contactor coil", contactor.contractor_questions[0])
        self.assertNotIn("Is the thermostat calling", contactor.contractor_questions[0])

    def test_partial_capacitor_does_not_reask_submitted_reading(self):
        final = finalize(electrical_raw(), "Replace capacitor.\nCapacitor rated 45 µF measured 43 µF.")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertIn("tolerance", final.contractor_questions[0])
        self.assertNotIn("What capacitance", final.contractor_questions[0])

    def test_legacy_motor_and_combustion_bad_partial_good_production_paths(self):
        for definitions in (main.MOTOR_EVIDENCE_DEFINITIONS, main.COMBUSTION_EVIDENCE_DEFINITIONS):
            for definition in definitions.values():
                for status, scope, support in (("ABSENT", "UNSUPPORTED", "UNSUPPORTED"),
                        ("INCOMPLETE", "PARTIALLY_DEFINED", "PARTIALLY_SUPPORTED"),
                        ("CONFIRMED", "APPROPRIATE", "SUPPORTED")):
                    with self.subTest(component=definition.component, status=status):
                        # No source-calibration changes for these legacy modules:
                        # the mock supplies their existing structured assessment.
                        evidence = [] if status == "ABSENT" else [f"The contractor documented {definition.component} operating-test observations."]
                        source = f"Proposed repair: service the {definition.component}.\n" + "\n".join(evidence)
                        raw = analysis_with([assessment(status, scope, subject=definition.component + " failure", evidence=evidence)], pricing="LIMITED")
                        raw.contractor_questions = [definition.question, definition.question.replace("What", "Which")]
                        before = raw.model_dump()
                        final, report = upload(raw, source)
                        self.assertEqual(final.decision.technical_support, support)
                        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
                        self.assertEqual(final.technical_assessments[0].model_dump(), raw.technical_assessments[0].model_dump())
                        self.assertEqual(len(final.red_flags), int(status == "ABSENT"))
                        technical = [q for q in final.contractor_questions if main.repair_diagnostic_question_owner(q) == definition.key]
                        self.assertEqual(len(technical), int(status != "CONFIRMED"))
                        for finding in evidence:
                            self.assertIn(finding, " ".join(p for section in final.repair_evidence_sections for p in section.paragraphs))
                        self.assertEqual(raw.model_dump(), before)
                        self.assert_parity(final, report)

    def test_one_legacy_diagnosis_deduplicates_missing_tests_into_one_flag(self):
        item = assessment("ABSENT", "UNSUPPORTED", subject="Condenser fan motor failure")
        raw = analysis_with([item])
        raw.red_flags = ["Condenser fan motor voltage test missing.", "Condenser fan motor winding test missing."]
        final = finalize(raw, "Replace condenser fan motor.")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_independent_motor_diagnoses_keep_distinct_questions(self):
        raw = analysis_with([
            assessment("ABSENT", "UNSUPPORTED", subject="Blower motor failure"),
            assessment("ABSENT", "UNSUPPORTED", subject="Condenser fan motor failure"),
        ], pricing="LIMITED")
        final = finalize(raw, "Replace blower motor and replace condenser fan motor.")
        self.assertEqual([main.repair_diagnostic_question_owner(q) for q in final.contractor_questions],
                         ["motor_blower", "motor_condenser_fan", None])
        self.assertEqual(len(final.red_flags), 2)

    def test_motor_failure_is_not_a_refrigerant_coil_diagnosis(self):
        for subject in ("Condenser fan motor failure", "Outdoor fan motor replacement", "ECM module failure", "Inducer motor failure"):
            self.assertIsNone(rs.subject_kind(subject))
        self.assertEqual(rs.subject_kind("Condenser coil failure"), rs.COIL)
        self.assertEqual(rs.subject_kind("Evaporator coil leak"), rs.COIL)

    def test_actual_legacy_motor_fixtures_preserve_documented_findings_and_support(self):
        for component in ("blower", "condenser_fan", "inducer"):
            for state in ("bad", "good"):
                with self.subTest(component=component, state=state):
                    source = Path(f"electrical_{component}_{state}_test.txt").read_text()
                    lines = source.split("Diagnostic findings:", 1)
                    findings = [] if state == "bad" or len(lines) < 2 else [
                        s.strip(" -\t") for s in lines[1].split("Recommended repair:", 1)[0].splitlines() if s.strip()]
                    status, scope = ("ABSENT", "UNSUPPORTED") if state == "bad" else ("CONFIRMED", "APPROPRIATE")
                    raw = analysis_with([assessment(status, scope, subject=component.replace("_", " ") + " motor failure", evidence=findings)], pricing="LIMITED")
                    final, report = upload(raw, source)
                    self.assertEqual(final.decision.technical_support, "UNSUPPORTED" if state == "bad" else "SUPPORTED")
                    self.assertEqual(rs.refrigerant_items(final), [])
                    self.assertEqual(final.technical_assessments[0].documented_evidence, findings)
                    for finding in findings:
                        self.assertIn(finding, html.unescape(report))
                    self.assert_parity(final, report)

    def test_defrost_board_uses_existing_electrical_owner_not_a_new_heat_pump_module(self):
        source = "Replace defrost board.\nThe outdoor coil is iced."
        final = finalize(electrical_raw(), source)
        self.assertEqual(ec.electrical_items(final)[0].subject, ec.PREFIX + "control board")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        evidence = " ".join(p for section in final.repair_evidence_sections for p in section.paragraphs)
        self.assertIn("documented defrost sequence", evidence)
        self.assertIn("sensor findings", evidence)
        self.assertEqual(len([q for q in final.contractor_questions if main.repair_diagnostic_question_owner(q)]), 1)

    def test_capacitor_evidence_does_not_satisfy_compressor_diagnosis(self):
        source = Path("electrical_capacitor_compressor_bad_test.txt").read_text()
        final = finalize(electrical_raw(), source)
        self.assertEqual(ec.electrical_items(final)[0].scope_support, "APPROPRIATE")
        self.assertTrue(any(a.scope_support == "UNSUPPORTED" for a in cp.compressor_items(final)))
        self.assertNotIn("electrical_capacitor", [main.repair_diagnostic_question_owner(q) for q in final.contractor_questions])
        self.assertIn("compressor_evidence", [main.repair_diagnostic_question_owner(q) for q in final.contractor_questions])

    def test_heat_exchanger_evidence_methods_are_alternatives_not_a_checklist(self):
        raw = analysis_with([assessment("ABSENT", "UNSUPPORTED", subject="Heat exchanger failure")])
        final = finalize(raw)
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(len(final.contractor_questions), 1)
        evidence = " ".join(p for section in final.repair_evidence_sections for p in section.paragraphs)
        self.assertIn("crack, hole, separation", evidence)
        self.assertIn("The contractor can support", evidence)
        self.assertIn(" or ", evidence)
        self.assertNotIn("not a required checklist", evidence)
        self.assertNotIn("temperature-rise", evidence)
        good = finalize(analysis_with([assessment("CONFIRMED", subject="Heat exchanger failure",
            evidence=["A photo shows a crack in the heat exchanger."])]))
        self.assertEqual(good.decision.technical_support, "SUPPORTED")
        self.assertEqual(good.contractor_questions, [])
        self.assertNotIn("examples of contractor evidence", good.equipment_analysis)

    def test_component_examples_never_direct_homeowner_to_perform_tests(self):
        definitions = [cp.COMPRESSOR_EVIDENCE, *rs.REFRIGERANT_EVIDENCE.values(),
            *main.MOTOR_EVIDENCE_DEFINITIONS.values(), *main.COMBUSTION_EVIDENCE_DEFINITIONS.values(), main.HEAT_EXCHANGER_EVIDENCE]
        for name in ec.CONTROL_EVIDENCE_EXAMPLES:
            definitions.append(ec.electrical_evidence_review(assessment("ABSENT", "UNSUPPORTED", subject=ec.PREFIX + name)).definition)
        for definition in definitions:
            view = review_repair_evidence(assessment("ABSENT", "UNSUPPORTED"), definition)
            self.assertIn("The contractor can support", view.evidence_explanation)
            self.assertNotRegex(view.evidence_explanation + definition.question,
                                r"(?i)you (?:should|must|need to) (?:test|measure|open|probe)|test it yourself")
            self.assertNotRegex(view.evidence_explanation, r"\d+\s*(?:V|volts|PSIG|µF|ohms|amps)")

    def test_repeated_finalization_and_rendering_are_idempotent(self):
        for filename in ("electrical_controls_transformer_partial_test.txt", "electrical_controls_board_partial_test.txt",
                         "compressor_partial_test.txt", "refrigerant_system_bad_test.txt"):
            with self.subTest(filename=filename):
                source = Path(filename).read_text()
                final = finalize(electrical_raw(), source)
                self.assertEqual(final.model_dump(), finalize(final, source).model_dump())
                self.assert_parity(final, main.build_report_html(final, 1))

    def test_nonrepair_and_diagnostic_only_dimensions_do_not_receive_repair_guides(self):
        for subject in ("Equipment matching", "System sizing", "Duct airflow", "Warranty coverage", "Startup and commissioning plan", "Basis for full-system replacement"):
            self.assertIsNone(main.legacy_repair_evidence_definition(subject))
        self.assertIn("heat_pump_reversing_valve_defrost", main.PHASE_2_MODULE_GAPS)
        self.assertNotIn("heat_pump_reversing_valve_defrost", [m.value for m in main.AnalysisModule])


if __name__ == "__main__":
    unittest.main()
