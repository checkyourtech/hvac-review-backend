"""Phase 2H offline evidence, ownership and real upload-to-report regressions."""
import html
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
import compressor as cp
from commissioning import commissioning_items
from test_technical_support_derivation import assessment, analysis_with


CASES = {
    "good": ("CONFIRMED", "APPROPRIATE", "SUPPORTED", "PROCEED", 0, 0),
    "partial": ("INCOMPLETE", "PARTIALLY_DEFINED", "PARTIALLY_SUPPORTED", "REVIEW_BEFORE_APPROVING", 0, 1),
    "bad": ("CONTRADICTORY", "UNSUPPORTED", "UNSUPPORTED", "GET_A_SECOND_OPINION", 1, 1),
    "capacitor_boundary": ("ABSENT", "UNSUPPORTED", "UNSUPPORTED", "GET_A_SECOND_OPINION", 1, 1),
    "cross_module": ("CONFIRMED", "APPROPRIATE", "PARTIALLY_SUPPORTED", "REVIEW_BEFORE_APPROVING", 0, 0),
}


def source(case):
    return Path(f"compressor_{case}_test.txt").read_text()


def raw(case):
    result = analysis_with([], ai_support="SUPPORTED")
    if case in {"capacitor_boundary", "bad"}:
        result.technical_assessments.append(assessment("CONFIRMED", subject="Run capacitor failure",
            evidence=["45 microfarad capacitor measured 18 microfarads."]))
        result.good_signs.append("The capacitor measurement is compared with its labeled rating.")
    return result


def classified(modules=None):
    return main.QuoteClassification(quote_type="repair", system_type="split AC",
        primary_scope="Compressor work", modules_required=modules or [])


class CompressorTests(unittest.TestCase):
    def final(self, case):
        return main.finalize_customer_analysis(raw(case), source(case), 1)

    def check_case(self, final, case):
        status, scope, support, verdict, flags, questions = CASES[case]
        items = cp.compressor_items(final)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].subject, cp.COMPRESSOR_SUBJECT)
        self.assertEqual(items[0].materiality, "PRIMARY")
        self.assertEqual((items[0].diagnostic_evidence_status, items[0].scope_support), (status, scope))
        self.assertEqual((final.decision.technical_support, final.decision.verdict), (support, verdict))
        self.assertEqual(len(final.red_flags), flags)
        self.assertEqual(sum(main.contractor_question_category(q) == "compressor_evidence" for q in final.contractor_questions), questions)
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")

    def test_registry_and_prompt_ownership(self):
        self.assertEqual(main.ANALYSIS_MODULES[main.AnalysisModule.COMPRESSOR], cp.COMPRESSOR_RULES)
        self.assertEqual(set(main.ANALYSIS_MODULES), set(main.AnalysisModule))
        self.assertNotIn("If no winding-to-ground", cp.COMPRESSOR_RULES)
        for phrase in ("PRIMARY", "universal checklist", "ABSENT / UNSUPPORTED", "not high current", "warranty entitlement"):
            self.assertIn(phrase, cp.COMPRESSOR_RULES)

    def test_good(self):
        final = self.final("good")
        self.check_case(final, "good")
        self.assertEqual(final.contractor_questions, [])
        self.assertIn("short to ground", " ".join(cp.compressor_paragraphs(final)))

    def test_partial(self):
        final = self.final("partial")
        self.check_case(final, "partial")
        self.assertEqual(len(final.contractor_questions), 1)

    def test_partial_live_prose_through_upload(self):
        original = raw("partial")
        original.equipment_analysis = "The diagnosis leans more towards INCOMPLETE. The scope is APPROPRIATE."
        original.missing_information = "INCOMPLETE. Further electrical testing is needed. More details are needed."
        original.decision.pricing_transparency = "LIMITED"
        original.decision.required_actions = ["Request more itemization and labor hours."]
        original.pricing_review = "More detail about markup is required."
        original.contractor_questions = [
            "Is there any warranty provided on the new compressor and the labor for its installation?",
            "What startup checks will be performed?", "What electrical test results are missing?",
            "Can you itemize the price?", "What evidence supports compressor replacement?"]
        before = original.model_dump()
        parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=original))])
        with patch.object(main, "classify_quotes", return_value=classified()), patch.object(
                main.client.beta.chat.completions, "parse", return_value=parsed), patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                files={"files": ("compressor_partial_test.txt", source("partial").encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.check_case(final, "partial")
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("voltage drop", final.contractor_questions[0])
        self.assertEqual(final.market_price_context.status, main.MarketPriceStatus.NOT_EVALUATED)
        self.assertEqual(original.model_dump(), before)
        self.assertEqual(response.text, main.build_report_html(final, 1))
        for token in ("INCOMPLETE", "ADEQUATE", "CONFIRMED", "PARTIALLY_DEFINED", "APPROPRIATE", "markup", "labor hours"):
            self.assertNotIn(token, response.text)
        self.assertEqual(final.missing_information, "The quote does not show what is causing the voltage drop during the compressor start attempt.")
        self.assertIn("isolate the cause", final.bottom_line)
        self.assertEqual(len(cp.compressor_paragraphs(final)), 1)
        self.assertIn("does not", final.equipment_analysis)
        self.assertIn("How Will Startup Be Verified?", response.text)

    def test_partial_preserves_independent_structured_warranty_gap(self):
        original = raw("partial")
        original.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED", materiality="MATERIAL_SECONDARY",
            subject="Warranty coverage", gaps=["The stated warranty coverage conflicts with the exclusion and needs clarification."]))
        original.contractor_questions = ["What warranty coverage applies given the stated exclusion?"]
        final = main.finalize_customer_analysis(original, source("partial"), 1)
        self.assertIn("warranty", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_bad(self):
        final = self.final("bad")
        self.check_case(final, "bad")
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("normally", " ".join(cp.compressor_paragraphs(final)))

    def test_bad_live_refrigerant_scope_contamination_through_upload(self):
        original = raw("bad")
        original.installation_concerns = "Refrigerant recovery and restoration are included in compressor replacement."
        original.equipment_analysis = "The quote includes refrigerant work."
        original.contractor_questions = [
            "What testing or measurements established that the system is low on refrigerant?",
            "Was the cause of the low charge evaluated, and was leak investigation performed or recommended when appropriate?",
            "How will the refrigerant charge and cooling performance be verified after the work?",
            "Is there a warranty?", "What startup checks are included?", "What electrical testing will be performed?"]
        before = original.model_dump()
        parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=original))])
        with patch.object(main, "classify_quotes", return_value=classified()), patch.object(
                main.client.beta.chat.completions, "parse", return_value=parsed), patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                files={"files": ("compressor_bad_test.txt", source("bad").encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.check_case(final, "bad")
        self.assertEqual(final.contractor_questions, ["What evidence shows the compressor itself has failed and still needs replacement?"])
        self.assertEqual(final.missing_information, "The quote does not show a compressor-specific failure finding after the failed starting capacitor was replaced.")
        self.assertEqual(response.text, main.build_report_html(final, 1))
        self.assertEqual(before, original.model_dump())
        for phrase in ("rated 45 microfarads +/-6% measured 18 microfarads", "started and ran normally", "$4,950", "$3,000", "$1,950"):
            self.assertIn(phrase, response.text)
        for question in original.contractor_questions:
            self.assertNotIn(html.escape(question), response.text)
        self.assertEqual(final.model_dump(), main.finalize_customer_analysis(final, source("bad"), 1).model_dump())

    def test_compressor_does_not_suppress_independent_refrigerant_gap(self):
        import refrigerant_system as rs
        original = raw("bad")
        original.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            subject=rs.CONDITION, evidence=["Separate low-charge observations need interpretation."],
            gaps=["Refrigerant condition remains unresolved."]))
        final = main.finalize_customer_analysis(original, source("bad") + "\nSeparate diagnosis: low refrigerant charge.", 1)
        self.assertTrue(main.refrigerant_context(final, source("bad")))
        self.assertIn("refrigerant_evidence", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_capacitor_does_not_rescue_compressor(self):
        final = self.final("capacitor_boundary")
        self.check_case(final, "capacitor_boundary")
        capacitor = next(a for a in final.technical_assessments if a.subject == "Run capacitor failure")
        self.assertEqual(capacitor.diagnostic_evidence_status, "CONFIRMED")
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("capacitor measurement", " ".join(final.good_signs))

    def test_other_domain_gap_does_not_rewrite_compressor(self):
        final = self.final("cross_module")
        self.check_case(final, "cross_module")
        self.assertEqual(commissioning_items(final)[0].scope_support, "PARTIALLY_DEFINED")
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["commissioning"])

    def test_real_upload_all_fixtures_no_completeness_bypass(self):
        for case in CASES:
            with self.subTest(case=case):
                original = raw(case)
                before = original.model_dump()
                parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=original))])
                captured = {}
                def email(**kwargs):
                    captured["analysis"] = kwargs["analysis"]
                    captured["html"] = main.build_report_html(kwargs["analysis"], 1)
                with patch.object(main, "classify_quotes", return_value=classified()), patch.object(
                        main.client.beta.chat.completions, "parse", return_value=parsed), patch.object(main, "send_review_email", side_effect=email):
                    response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                        files={"files": (f"compressor_{case}_test.txt", source(case).encode(), "text/plain")})
                self.assertEqual(response.status_code, 200)
                self.check_case(captured["analysis"], case)
                self.assertEqual(response.text, captured["html"])
                self.assertEqual(original.model_dump(), before)
                self.assertIsNot(original, captured["analysis"])
                self.assertIn("What Does the Compressor Evidence Show?", response.text)
                self.assertIn(html.escape(main.verdict_display_name(captured["analysis"].decision.verdict)), response.text)
                self.assertEqual(captured["analysis"].market_price_context.status, main.MarketPriceStatus.NOT_EVALUATED)

    def test_raw_immutable_and_finalization_idempotent(self):
        for case in CASES:
            original = raw(case)
            before = original.model_dump()
            final = main.finalize_customer_analysis(original, source(case), 1)
            self.assertEqual(original.model_dump(), before)
            self.assertEqual(final.model_dump(), main.finalize_customer_analysis(final, source(case), 1).model_dump())

    def test_renderer_does_not_mutate_analysis(self):
        final = self.final("bad")
        before = final.model_dump()
        main.build_report_html(final, 1)
        self.assertEqual(final.model_dump(), before)

    def test_routing_material(self):
        for text in ("Replace compressor", "Compressor diagnosis: grounded", "Compressor failure",
                     "Locked compressor", "Compressor winding failure", "Confirmed open compressor winding.",
                     "Compressor intermittently fails to start"):
            self.assertTrue(cp.compressor_required(text), text)

    def test_routing_nonmaterial(self):
        for text in ("Replace condenser fan motor", "Replace capacitor", "Replace contactor",
                     "Refrigerant leak at evaporator", "New heat pump includes inverter compressor",
                     "Replace full system with scroll compressor", "No compressor replacement proposed"):
            self.assertFalse(cp.compressor_required(text), text)
            final = main.finalize_customer_analysis(analysis_with([]), text, 1)
            self.assertNotIn("What Does the Compressor Evidence Show?", main.build_report_html(final, 1))

    def test_classifier_omission_and_false_positive(self):
        for text, selected, expected in ((source("good"), [], True),
                ("Replace failed capacitor only", [main.AnalysisModule.COMPRESSOR], False)):
            parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=classified(selected)))])
            with patch.object(main.client.beta.chat.completions, "parse", return_value=parsed):
                result = main.classify_quotes(text)
            self.assertEqual(main.AnalysisModule.COMPRESSOR in result.modules_required, expected)

    def test_knowledge_recovery_does_not_mutate_classification(self):
        original = classified()
        before = original.model_dump()
        self.assertIn(cp.COMPRESSOR_RULES, main.get_analysis_knowledge(original, source("good")))
        self.assertEqual(before, original.model_dump())

    def test_mislabeled_assessment_normalized_on_final_copy(self):
        original = analysis_with([assessment("ABSENT", "UNSUPPORTED", subject="Compressor electrical diagnosis")])
        before = original.model_dump()
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.check_case(final, "good")
        self.assertEqual(original.model_dump(), before)

    def test_direct_open_winding_needs_no_ground_test(self):
        facts = cp.compressor_source_facts("Technician confirmed open compressor winding.\nReplace the compressor.")
        self.assertEqual(facts["diagnostic_evidence_status"], "CONFIRMED")
        self.assertEqual(facts["material_gaps"], [])

    def test_isolated_ground_needs_no_resistance_checklist(self):
        facts = cp.compressor_source_facts("Compressor leads were isolated.\nTesting directly at compressor terminals confirmed a short to ground.\nReplace compressor.")
        self.assertEqual(facts["scope_support"], "APPROPRIATE")
        self.assertEqual(facts["material_gaps"], [])

    def test_isolated_insulation_failure(self):
        facts = cp.compressor_source_facts("Isolated compressor failed insulation-to-ground test.\nReplace compressor.")
        self.assertEqual(facts["scope_support"], "APPROPRIATE")

    def test_planned_or_negated_results_cannot_recover_support(self):
        for text in ("Will test isolated compressor for short to ground.", "No confirmed open compressor winding.",
                     "Possible short to ground at isolated compressor.", "If isolated compressor has a short to ground, replace it."):
            self.assertNotEqual(cp.compressor_source_facts(text + "\nReplace compressor.")["scope_support"], "APPROPRIATE", text)

    def test_assertions_and_other_component_facts_not_direct_evidence(self):
        for text in ("Compressor bad.", "Failed capacitor.", "Breaker trips.", "No cooling.", "System is 20 years old.",
                     "Compressor fault code E9.", "Compressor locked up.", "Low refrigerant."):
            self.assertNotEqual(cp.compressor_source_facts(text + "\nReplace compressor.")["scope_support"], "APPROPRIATE", text)

    def test_meaningful_nonisolated_start_problem_is_partial_not_contradiction(self):
        facts = cp.compressor_source_facts("Compressor hums on attempted start; input voltage not yet isolated.\nReplace compressor.")
        self.assertEqual((facts["diagnostic_evidence_status"], facts["scope_support"]), ("INCOMPLETE", "PARTIALLY_DEFINED"))
        self.assertFalse(facts["contradictions"])

    def test_negative_ground_test_conflicts_with_ground_claim(self):
        facts = cp.compressor_source_facts("Diagnosis: Compressor shorted to ground.\nIsolated compressor terminal testing shows no ground fault.\nReplace compressor.")
        self.assertEqual(facts["diagnostic_evidence_status"], "CONTRADICTORY")

    def test_no_cross_quote_evidence_join(self):
        text = "QUOTE 1\nConfirmed open compressor winding.\nQUOTE 2\nReplace compressor."
        self.assertIsNone(cp.compressor_source_facts(text))

    def test_structured_manufacturer_evidence_preserved(self):
        item = assessment("CONFIRMED", subject=cp.COMPRESSOR_SUBJECT,
            evidence=["Submitted manufacturer bulletin K specifies this measured winding pattern as failure."])
        original = analysis_with([item])
        final = main.finalize_customer_analysis(original, "Replace compressor.\nManufacturer startup procedure and document final readings.", 1)
        self.assertEqual(cp.compressor_items(final)[0].documented_evidence, item.documented_evidence)
        self.assertEqual(cp.compressor_items(final)[0].scope_support, "APPROPRIATE")

    def test_real_structured_conflict_not_erased_by_source_support(self):
        item = assessment("CONTRADICTORY", "UNSUPPORTED", subject=cp.COMPRESSOR_SUBJECT,
            contradictions=["The proposed replacement is a different compressor from the one tested."])
        final = main.finalize_customer_analysis(analysis_with([item]), source("good"), 1)
        self.assertEqual(cp.compressor_items(final)[0].scope_support, "UNSUPPORTED")

    def test_bare_supported_ai_assessment_cannot_pass_unsupported_source(self):
        original = analysis_with([assessment(subject=cp.COMPRESSOR_SUBJECT, evidence=["Compressor bad."])])
        final = main.finalize_customer_analysis(original, source("capacitor_boundary"), 1)
        self.assertEqual(cp.compressor_items(final)[0].diagnostic_evidence_status, "ABSENT")

    def test_question_deduplication_and_domain_firewall(self):
        final = self.final("partial")
        final.contractor_questions += ["What evidence supports compressor replacement?", "How was compressor failure isolated?",
                                      "Why does this need a new compressor?"]
        questions = main.build_contractor_questions(final, 1, source("partial"))
        self.assertEqual(len(questions), 1)
        self.assertEqual(main.contractor_question_category(questions[0]), "compressor_evidence")
        for q in ("What does the compressor warranty cover?", "Can you itemize compressor replacement pricing?",
                  "How will refrigerant charge be verified after compressor replacement?", "What startup checks follow compressor replacement?",
                  "What does compressor replacement cost?", "Why replace the whole system rather than the compressor?"):
            self.assertFalse(cp.compressor_question(q), q)

    def test_other_motor_ground_fault_cannot_support_compressor(self):
        facts = cp.compressor_source_facts("Compressor leads were isolated.\nBlower motor test confirmed a short to ground.\nReplace compressor.")
        self.assertNotEqual(facts["scope_support"], "APPROPRIATE")

    def test_repair_procedures_do_not_establish_failure(self):
        facts = cp.compressor_source_facts("Proposed repair: Replace compressor.\nScope includes compressor startup testing, evacuation and charging.")
        self.assertEqual(facts["diagnostic_evidence_status"], "ABSENT")

    def test_direct_failure_removes_unnecessary_compressor_checklist(self):
        original = analysis_with([assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=cp.COMPRESSOR_SUBJECT,
            gaps=["Compressor winding resistance readings are missing."])],
            required_actions=["Obtain compressor winding resistance readings."])
        original.contractor_questions = ["Can you supply compressor winding resistance readings?"]
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.contractor_questions, [])

    def test_pricing_independent(self):
        original = raw("good")
        original.decision.pricing_transparency = "LIMITED"
        original.decision.required_actions = ["Clarify the additional service fee not included in the quoted total."]
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["pricing"])

    def test_good_itemized_repair_pricing_through_upload(self):
        for quoted in (source("good"), source("good").replace(
            "Total repair price: $4,950.\nCompressor and materials: $3,000.\nLabor: $1,950.",
            "Total repair price: $3,600.\nCompressor: $1,900.\nLabor: $1,100.\nRefrigerant: $350.\nMaterials: $250.")):
            with self.subTest(quoted=quoted):
                original = raw("good")
                original.decision.pricing_transparency = "LIMITED"
                original.decision.required_actions = ["Provide contractor markup and labor hours.", "Request a more itemized breakdown."]
                original.decision.verdict_reasons = ["Pricing transparency is limited without hourly labor rates."]
                original.pricing_review = "The breakdown is clear but lacks labor hours and contractor markup."
                original.good_signs = ["The pricing breakdown is clear.", "The compressor failure is supported."]
                original.contractor_questions = ["Can you provide a more itemized breakdown of compressor replacement labor, materials, and refrigerant charges?"]
                original.installation_concerns = "The included steps ensure a successful repair."
                before = original.model_dump()
                parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=original))])
                with patch.object(main, "classify_quotes", return_value=classified()), patch.object(
                        main.client.beta.chat.completions, "parse", return_value=parsed), patch.object(main, "send_review_email") as email:
                    response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                        files={"files": ("compressor_good_test.txt", quoted.encode(), "text/plain")})
                final = email.call_args.kwargs["analysis"]
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, main.build_report_html(final, 1))
                self.assertEqual(original.model_dump(), before)
                self.check_case(final, "good")
                self.assertEqual(final.contractor_questions, [])
                self.assertEqual(final.decision.required_actions, [])
                self.assertEqual(final.market_price_context.status, main.MarketPriceStatus.NOT_EVALUATED)
                for phrase in ("labor hours", "markup", "hourly labor", "successful repair", "requested price breakdown", "more itemized"):
                    self.assertNotIn(phrase, response.text.lower())
                self.assertIn("compressor failure", final.bottom_line)
                self.assertIn("evacuation", final.installation_concerns)
                for amount in (["$3,600", "$1,900", "$1,100", "$350", "$250"] if "$3,600" in quoted else ["$4,950", "$3,000", "$1,950"]):
                    self.assertIn(amount, final.pricing_review)

    def test_supported_replacement_basis_cannot_rescue_bad_compressor(self):
        original = raw("bad")
        original.technical_assessments.append(assessment(subject="Basis for full-system replacement"))
        final = main.finalize_customer_analysis(original, source("bad"), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(cp.compressor_items(final)[0].diagnostic_evidence_status, "CONTRADICTORY")

    def test_replacement_basis_gap_cannot_change_supported_compressor(self):
        original = raw("good")
        original.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject="Basis for full-system replacement"))
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(cp.compressor_items(final)[0].scope_support, "APPROPRIATE")


if __name__ == "__main__":
    unittest.main()
