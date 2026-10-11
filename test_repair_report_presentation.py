"""Offline production /upload coverage for finalizer-owned repair evidence."""
import html
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import main
import electrical_controls as ec
from repair_diagnostics import RepairEvidenceSection, REPAIR_EVIDENCE_HEADING, present_isolated_repair_summary
from test_electrical_controls import raw
from test_repair_diagnostics import finalize, upload
from test_technical_support_derivation import analysis_with, assessment


def evidence_text(final):
    return " ".join(p for section in final.repair_evidence_sections for p in section.paragraphs)


class RepairReportPresentationTests(unittest.TestCase):
    def transformer(self, state):
        return self.repair("transformer", state)

    def repair(self, component, state):
        source = Path(f"electrical_controls_{component}_{state}_test.txt").read_text()
        original = raw(component, malicious=True)
        before = original.model_dump()
        final, report = upload(original, source)
        self.assertEqual(original.model_dump(), before)
        self.assertEqual(report, main.build_report_html(final, 1))
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
        self.assertEqual(sum(main.contractor_question_category(q) == "pricing"
                             for q in final.contractor_questions), 1)
        for field in ("project_overview", "banner_explanation", "equipment_analysis", "bottom_line"):
            self.assertIn(html.escape(getattr(final, field)), report)
        missing = main.missing_information_for_report(final.missing_information)
        if missing:
            self.assertIn(html.escape(missing), report)
        else:
            self.assertNotIn("<h2>Important Missing Information</h2>", report)
        for section in final.repair_evidence_sections:
            for paragraph in section.paragraphs:
                self.assertIn(html.escape(paragraph), report)
        return final, html.unescape(report), source

    def test_transformer_bad_upload_uses_authoritative_specific_evidence(self):
        final, report, _ = self.transformer("bad")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual((len(final.red_flags), len(final.good_signs)), (1, 0))
        self.assertIn("GET A SECOND OPINION", report)
        self.assertEqual(final.project_overview, "The quote proposes replacing the control transformer for $650.")
        evidence = evidence_text(final)
        for term in ("rated primary voltage", "secondary/control-voltage", "commonly provide about 24 VAC",
                     "A technician should verify", "downstream 24-volt", "the contractor may also need"):
            self.assertIn(term, evidence)
        self.assertNotIn("general context", evidence)
        self.assertNotIn("not a measured value from this quote", evidence)
        self.assertLess(len(evidence.split()), 85)
        self.assertIn("primary-input voltage", final.missing_information)
        self.assertIn("secondary-output voltage", final.missing_information)
        self.assertIn("transformer itself", final.missing_information)
        self.assertEqual(len(final.contractor_questions), 2)
        diagnostic, pricing = final.contractor_questions
        self.assertIn("What primary and secondary voltages", diagnostic)
        self.assertIn("if the secondary voltage was low or absent", diagnostic)
        self.assertIn("short or excessive load", diagnostic)
        self.assertIn("itemized breakdown", pricing)
        self.assertEqual(report.count(diagnostic), 1)
        self.assertNotIn("What testing showed that the control transformer itself has failed?", report)
        self.assertNotIn("The quote does not show what finding supports replacing the transformer.", report)
        self.assertNotIn("For the transformer diagnosis, examples", report)

    def test_bad_report_sections_have_distinct_purposes(self):
        final, report, _ = self.transformer("bad")
        self.assertIn("What Evidence Should Support This Diagnosis?", report)
        self.assertNotIn("What Does the Electrical Evidence Show?", report)
        self.assertNotIn("What This Means for You", report)
        self.assertNotIn("Installation / Repair Concerns", report)
        self.assertEqual(final.homeowner_takeaway, "")
        self.assertEqual(final.installation_concerns, "")
        self.assertIn("possible", final.equipment_analysis)
        self.assertNotIn("primary voltage", final.equipment_analysis)
        self.assertEqual(report.count(final.banner_explanation), 1)
        self.assertEqual(report.count(final.equipment_analysis), 1)
        self.assertEqual(report.count(final.missing_information), 1)
        self.assertIn("Ask for the missing electrical test results or get a second opinion", final.bottom_line)
        self.assertNotIn("diagnos", final.pricing_review.lower())

    def test_transformer_partial_upload_acknowledges_input_requests_only_output(self):
        final, report, _ = self.transformer("partial")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual((len(final.red_flags), len(final.good_signs)), (0, 0))
        self.assertIn("primary input power confirmed present", evidence_text(final))
        self.assertIn("secondary", final.missing_information)
        self.assertNotIn("primary-input voltage", final.missing_information)
        self.assertEqual(len(final.contractor_questions), 2)
        question = final.contractor_questions[0]
        self.assertIn("What voltage was measured on the transformer secondary", question)
        self.assertNotIn("What primary", question)
        self.assertNotIn("downstream", question)
        self.assertEqual(report.count(question), 1)
        self.assertNotIn("no testing was performed", report.lower())

    def test_transformer_good_upload_credits_evidence_without_technical_question(self):
        final, report, _ = self.transformer("good")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual((len(final.red_flags), len(final.good_signs)), (0, 1))
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("power was reaching the transformer", evidence_text(final))
        self.assertIn("measured no secondary output", evidence_text(final))
        self.assertNotIn("no output was measured", evidence_text(final))
        self.assertIn("No important technical information is missing", final.missing_information)
        self.assertNotIn("downstream", evidence_text(final))
        self.assertNotIn("What primary and secondary", report)

    def test_upload_omits_good_signs_when_no_positive_findings_exist(self):
        for state in ("bad", "partial"):
            with self.subTest(state=state):
                final, report, _ = self.transformer(state)
                self.assertEqual(final.good_signs, [])
                self.assertNotIn("<h2>Good Signs</h2>", report)
                self.assertNotIn("No major positive findings were specifically documented.", report)
                self.assertRegex(report, r"<strong>Positive Findings</strong>\s*0")

    def test_upload_keeps_good_signs_when_documented_positives_exist(self):
        final, report, _ = self.transformer("good")
        self.assertEqual(len(final.good_signs), 1)
        self.assertIn("<h2>Good Signs</h2>", report)
        self.assertIn(f"<li>{final.good_signs[0]}</li>", report)
        self.assertNotIn("No major positive findings were specifically documented.", report)
        self.assertRegex(report, r"<strong>Positive Findings</strong>\s*1")

    def test_nominal_context_is_not_an_invented_submitted_measurement(self):
        for state in ("bad", "partial", "good"):
            with self.subTest(state=state):
                final, report, _ = self.transformer(state)
                for fabricated in ("120 V", "208 V", "240 V", "998", "FAKE777", "terminal R1"):
                    self.assertNotIn(fabricated, report)
                self.assertNotRegex(report, r"(?:measured|confirmed|tested) (?:at )?24\s*(?:VAC|V|volts)")
                facts = " ".join(e for item in final.technical_assessments for e in item.documented_evidence)
                self.assertNotIn("24", facts)
                self.assertNotRegex(report, r"(?:you|homeowner) (?:should|must|can) (?:measure|probe|open|test)")

    def test_existing_partial_downstream_gap_does_not_reask_known_input_output(self):
        # Presentation consumes this existing PARTIAL state. It must not impose
        # a downstream check on the supported source-recovery boundary below.
        item = assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=ec.PREFIX + "transformer",
            evidence=["Transformer primary input power confirmed present.", "Transformer secondary output measured absent."],
            gaps=["Downstream short or excessive load has not been checked."])
        final = analysis_with([item])
        before = item.model_dump()
        main.finalize_repair_diagnostic_guidance(final)
        self.assertEqual(item.model_dump(), before)
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("downstream 24-volt circuit", final.contractor_questions[0])
        self.assertNotIn("What voltage", final.contractor_questions[0])
        self.assertEqual(main.repair_diagnostic_question_owner(final.contractor_questions[0]), "electrical_transformer")
        self.assertIn("downstream", final.missing_information)

    def test_supported_source_boundary_does_not_gain_downstream_requirement(self):
        final, _, _ = self.transformer("good")
        item = ec.electrical_items(final)[0]
        self.assertEqual((item.diagnostic_evidence_status, item.scope_support), ("CONFIRMED", "APPROPRIATE"))
        self.assertEqual(item.material_gaps, [])
        self.assertNotIn("downstream", " ".join(final.contractor_questions))

    def test_partial_with_documented_low_output_only_asks_remaining_downstream_gap(self):
        item = assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=ec.PREFIX + "transformer",
            evidence=["Transformer primary input power confirmed present.", "Transformer secondary voltage measured low at 14 volts."],
            gaps=["Downstream short or excessive load has not been checked."])
        final = analysis_with([item])
        before = item.model_dump()
        main.finalize_repair_diagnostic_guidance(final)
        self.assertEqual(item.model_dump(), before)
        self.assertIn("14 volts", evidence_text(final))
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("downstream 24-volt circuit", final.contractor_questions[0])
        self.assertNotIn("What voltage", final.contractor_questions[0])

    def test_contactor_upload_uses_shared_architecture_and_one_specific_question(self):
        source = Path("electrical_controls_contactor_bad_test.txt").read_text()
        final, report = upload(raw("contactor", malicious=True), source)
        report = html.unescape(report)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual((len(final.red_flags), len(final.good_signs)), (1, 0))
        self.assertEqual(len(final.contractor_questions), 2)
        question = final.contractor_questions[0]
        self.assertIn("control input", question)
        self.assertIn("contact-test", question)
        self.assertNotIn("What testing showed that the contactor itself has failed?", report)
        self.assertEqual(report.count(question), 1)
        self.assertIn("receiving the proper control voltage", evidence_text(final))
        self.assertIn("control voltage at the contactor coil", final.missing_information)
        self.assertNotIn("transformer", evidence_text(final))
        self.assertNotIn(final.banner_explanation, evidence_text(final))
        self.assertNotIn("What This Means for You", report)

    def test_finalized_evidence_is_rendered_without_recomposing_from_assessments(self):
        final, _, _ = self.transformer("bad")
        before = final.model_dump()
        with patch.object(main, "electrical_paragraphs", side_effect=AssertionError("stale renderer")), patch.object(
                main, "compressor_paragraphs", side_effect=AssertionError("stale renderer")), patch.object(
                main, "refrigerant_paragraphs", side_effect=AssertionError("stale renderer")):
            report = main.build_report_html(final, 1)
            # JSON/API consumers and the renderer receive the same snapshot.
            restored = main.HVACAnalysis.model_validate_json(final.model_dump_json())
            self.assertEqual(main.build_report_html(restored, 1), report)
        self.assertEqual(final.model_dump(), before)
        with self.assertRaises(ValidationError):
            final.repair_evidence_sections[0].title = "stale title"

    def test_raw_or_previously_finalized_snapshot_cannot_override_current_evidence(self):
        original = raw("transformer")
        original.repair_evidence_sections = [RepairEvidenceSection(
            owner="electrical", title="Injected stale section", paragraphs=("Transformer measured 998 volts.",))]
        final, report = upload(original, Path("electrical_controls_transformer_bad_test.txt").read_text())
        self.assertNotIn("998", report)
        self.assertNotIn("Injected stale", report)
        self.assertIn("rated primary voltage", evidence_text(final))
        self.assertEqual(original.repair_evidence_sections[0].title, "Injected stale section")

    def test_email_renderer_uses_the_same_finalized_html_without_network(self):
        for component, state in ((c, s) for c in ("transformer", "contactor") for s in ("bad", "partial", "good")):
            with self.subTest(component=component, state=state):
                final, upload_report, _ = self.repair(component, state)
                before = final.model_dump()
                with patch.dict(main.os.environ, {"EMAIL_APP_PASSWORD": "offline-test"}), patch.object(
                        main.smtplib, "SMTP_SSL") as transport:
                    main.send_review_email("Offline", "offline@example.com", "tier1", [component + ".txt"], final)
                sent = transport.return_value.__enter__.return_value.send_message.call_args.args[0]
                body = sent.get_body(preferencelist=("html",)).get_content()
                self.assertEqual(body.strip(), main.build_report_html(final, 1).strip())
                self.assertEqual(html.unescape(body).strip(), upload_report.strip())
                self.assertEqual("<h2>Good Signs</h2>" in body, bool(final.good_signs))
                self.assertNotIn("No major positive findings were specifically documented.", body)
                self.assertEqual("<h2>Red Flags</h2>" in body, bool(final.red_flags))
                self.assertNotIn("No major red flags were identified in the submitted proposal.", body)
                self.assertEqual("<h2>Important Missing Information</h2>" in body, state != "good")
                self.assertNotIn("<h2>Installation / Repair Concerns</h2>", body)
                self.assertIn(f"<h2>{REPAIR_EVIDENCE_HEADING}</h2>", body)
                self.assertNotIn("What Does the Electrical Evidence Show?", body)
                self.assertNotIn("What This Means for You", body)
                self.assertEqual(final.model_dump(), before)

    def test_six_repair_boundaries_keep_results_questions_and_component_bottom_line(self):
        expected = {
            "bad": ("UNSUPPORTED", "GET_A_SECOND_OPINION", 1, 0, 2),
            "partial": ("PARTIALLY_SUPPORTED", "REVIEW_BEFORE_APPROVING", 0, 0, 2),
            "good": ("SUPPORTED", "REVIEW_BEFORE_APPROVING", 0, 1, 1),
        }
        for component in ("transformer", "contactor"):
            for state, (support, verdict, flags, positives, questions) in expected.items():
                with self.subTest(component=component, state=state):
                    final, report, source = self.repair(component, state)
                    self.assertEqual(final.decision.technical_support, support)
                    self.assertEqual(final.decision.verdict, verdict)
                    self.assertEqual((len(final.red_flags), len(final.good_signs)), (flags, positives))
                    self.assertEqual(len(final.contractor_questions), questions)
                    self.assertIn(component, final.bottom_line)
                    self.assertNotIn("component diagnosis", final.bottom_line)
                    self.assertEqual(final.homeowner_takeaway, "")
                    self.assertIn(f"<h2>{REPAIR_EVIDENCE_HEADING}</h2>", report)
                    self.assertNotIn("What This Means for You", report)
                    self.assertNotIn("What Does the Electrical Evidence Show?", report)
                    self.assertEqual("<h2>Good Signs</h2>" in report, bool(positives))
                    self.assertNotIn("not a required checklist", report)
                    diagnostic = [q for q in final.contractor_questions if main.repair_diagnostic_question_owner(q)]
                    self.assertEqual(len(diagnostic), 0 if state == "good" else 1)
                    if diagnostic:
                        self.assertEqual(main.repair_diagnostic_question_owner(diagnostic[0]), "electrical_" + component)
                    self.assertEqual(main.contractor_question_category(final.contractor_questions[-1]), "pricing")
                    self.assertEqual(finalize(final, source).model_dump(), final.model_dump())

    def test_contactor_partial_starts_with_facts_then_only_remaining_evidence(self):
        final, _, _ = self.repair("contactor", "partial")
        evidence = evidence_text(final)
        for fact in ec.electrical_items(final)[0].documented_evidence:
            self.assertIn(fact, evidence)
            self.assertLess(evidence.index(fact), evidence.index("The remaining check"))
        self.assertIn("control voltage was reaching the contactor coil", evidence)
        self.assertNotIn("Damaged contacts", evidence)
        self.assertNotIn("excessive voltage drop", evidence)
        self.assertIn("What control voltage was measured", final.contractor_questions[0])
        self.assertEqual(final.bottom_line, "Have the contractor confirm the contactor diagnosis before approving the repair.")

    def test_contactor_bad_guidance_is_natural_and_keeps_alternative_evidence(self):
        final, report, _ = self.repair("contactor", "bad")
        evidence = evidence_text(final)
        for concept in ("A technician", "proper control voltage", "how it responds", "Damaged contacts",
                        "failed contact test", "voltage drop across closed contacts", "can also support"):
            self.assertIn(concept, evidence)
        self.assertNotIn("not a required checklist", report)
        self.assertEqual(final.bottom_line, "Ask for the missing contactor test results or get a second opinion before approving the replacement.")
        self.assertNotRegex(evidence, r"\d+\s*(?:V|volts?|amps?|ohms?)|(?i:you should|homeowner must)")

    def test_supported_contactor_preserves_concise_positive_and_actual_facts(self):
        final, _, _ = self.repair("contactor", "good")
        evidence = evidence_text(final)
        for concept in ("receiving the control signal", "did not pull in", "support replacing the contactor"):
            self.assertIn(concept, evidence)
        self.assertIn("control voltage reaching the contactor coil", final.good_signs[0])
        self.assertLess(len(final.good_signs[0]), len(evidence))
        self.assertIn("contactor replacement is technically supported", final.bottom_line)
        self.assertIn("itemized breakdown", final.bottom_line)
        self.assertNotIn("A technician should", evidence)

    def test_shared_summary_keeps_distinct_interpretation_but_omits_literal_repetition(self):
        final, _, _ = self.repair("contactor", "good")
        review = ec.electrical_evidence_review(ec.electrical_items(final)[0])
        generic = main.build_homeowner_takeaway(final.decision, final.red_flags, final.good_signs)
        generic_bottom = main.build_bottom_line(final.decision)
        for takeaway, expected in ((final.bottom_line, ""), (generic, ""),
                ("This repair addresses the starting problem, not the separate noise concern.",
                 "This repair addresses the starting problem, not the separate noise concern.")):
            final.homeowner_takeaway = takeaway
            before = final.model_dump(exclude={"homeowner_takeaway"})
            present_isolated_repair_summary(final, review, generic, generic_bottom)
            self.assertEqual(final.homeowner_takeaway, expected)
            self.assertEqual(final.model_dump(exclude={"homeowner_takeaway"}), before)

    def test_nonrepair_investigation_keeps_its_distinct_heading_and_takeaway(self):
        from test_refrigerant_system import raw as refrigerant_raw, source as refrigerant_source
        final, report = upload(refrigerant_raw("partial"), refrigerant_source("partial"))
        self.assertIn("What Does the Refrigerant Evidence Show?", report)
        self.assertNotIn(REPAIR_EVIDENCE_HEADING, report)
        self.assertIn("not a repair", final.homeowner_takeaway)
        self.assertIn("What This Means for You", report)

    def test_second_finalization_is_idempotent_for_transformer_boundaries(self):
        for state in ("bad", "partial", "good"):
            with self.subTest(state=state):
                final, _, source = self.transformer(state)
                self.assertEqual(finalize(final, source).model_dump(), final.model_dump())

    def test_transformer_evidence_cannot_satisfy_a_separate_contactor_diagnosis(self):
        source = ("Replace control transformer and replace contactor.\n"
                  "Transformer primary input power confirmed present.\n"
                  "Transformer secondary output measured absent.\nTotal: $950.")
        final, _ = upload(raw(), source)
        controls = {ec.component_subject(a.subject): a for a in ec.electrical_items(final)}
        self.assertEqual(controls["transformer"].scope_support, "APPROPRIATE")
        self.assertEqual(controls["contactor"].scope_support, "UNSUPPORTED")
        technical = [q for q in final.contractor_questions if main.repair_diagnostic_question_owner(q)]
        self.assertEqual(len(technical), 1)
        self.assertEqual(main.repair_diagnostic_question_owner(technical[0]), "electrical_contactor")

    def test_unknown_owner_is_not_assigned_a_component_evidence_profile(self):
        original = analysis_with([assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject="Unrecognized accessory concern")])
        original.missing_information = "Confirm the accessory identified by the contractor."
        original.contractor_questions = ["Which accessory is included?"]
        before = original.model_dump(exclude={"repair_evidence_sections"})
        main.finalize_repair_diagnostic_guidance(original)
        self.assertEqual(original.repair_evidence_sections, [])
        self.assertEqual(original.model_dump(exclude={"repair_evidence_sections"}), before)

    def test_included_scope_is_not_presented_as_a_repair_concern(self):
        source = Path("electrical_controls_transformer_bad_test.txt").read_text() + "\nReconnect the existing thermostat wiring."
        final, report = upload(raw(), source)
        self.assertEqual(final.installation_concerns, "")
        self.assertNotIn("<h2>Installation / Repair Concerns</h2>", report)
        self.assertEqual(len(final.contractor_questions), 2)

    def test_supported_transformer_only_displays_sections_with_findings(self):
        final, report, _ = self.transformer("good")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
        self.assertEqual((len(final.red_flags), len(final.good_signs)), (0, 1))
        for title in ("Red Flags", "Important Missing Information", "Installation / Repair Concerns"):
            self.assertNotIn(f"<h2>{title}</h2>", report)
        self.assertIn("<h2>Good Signs</h2>", report)
        self.assertNotIn("No important technical information is missing from the submitted diagnosis.", report)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["pricing"])
        self.assertIn("$650", final.project_overview)
        self.assertIn("$650", final.pricing_review)
        self.assertEqual(final.installation_concerns, "")
        for omitted_test_claim in ("no output was measured", "output was not measured", "output was never measured"):
            self.assertNotIn(omitted_test_claim, report)
        self.assertNotRegex(evidence_text(final), r"\b\d+(?:\.\d+)?\s*(?:VAC|volts?)\b")

    def test_partial_and_bad_transformer_keep_actual_missing_evidence_and_flags(self):
        for state, flags in (("partial", 0), ("bad", 1)):
            with self.subTest(state=state):
                final, report, _ = self.transformer(state)
                self.assertEqual((len(final.red_flags), len(final.good_signs)), (flags, 0))
                self.assertEqual("<h2>Red Flags</h2>" in report, bool(flags))
                self.assertIn("<h2>Important Missing Information</h2>", report)
                self.assertIn(final.missing_information, report)
                self.assertNotIn("<h2>Installation / Repair Concerns</h2>", report)
                self.assertNotIn("<h2>Good Signs</h2>", report)
                self.assertEqual(len(final.contractor_questions), 2)

    def test_missing_information_filter_preserves_real_concerns_and_compatibility_field(self):
        final, _, _ = self.transformer("good")
        no_gap = final.missing_information
        for text in ("", " \n\t", no_gap, "No material technical information is missing."):
            with self.subTest(text=text):
                final.missing_information = text
                before = final.model_dump()
                self.assertNotIn("<h2>Important Missing Information</h2>", main.build_report_html(final, 1))
                self.assertEqual(final.model_dump(), before)
        for text in ("The quote does not identify the replacement part's voltage rating.",
                     no_gap + " The quote does not explain whether damaged wiring will be corrected."):
            with self.subTest(text=text):
                final.missing_information = text
                report = html.unescape(main.build_report_html(final, 1))
                self.assertIn("<h2>Important Missing Information</h2>", report)
                self.assertIn(text, report)

    def test_independent_repair_scope_concern_is_preserved(self):
        source = Path("electrical_controls_transformer_good_test.txt").read_text()
        concern = "The quote does not explain whether the damaged wiring will be corrected."
        original = raw("transformer")
        original.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            materiality="MATERIAL_SECONDARY", subject="Repair scope: damaged wiring correction",
            gaps=[concern]))
        original.installation_concerns = concern
        final, report = upload(original, source + "\nDamaged wiring correction is not specified.")
        self.assertIn(concern, final.installation_concerns)
        self.assertIn("<h2>Installation / Repair Concerns</h2>", report)
        self.assertIn(concern, html.unescape(report))


if __name__ == "__main__":
    unittest.main()
