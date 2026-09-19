"""Offline Phase 2F production finalization and domain-boundary regressions."""
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from fastapi.testclient import TestClient
import main
from commissioning import (
    COMMISSIONING_RULES, COMMISSIONING_SUBJECT, commissioning_required,
    commissioning_text, commissioning_items, source_plan,
)
from test_duct_airflow import raw_case


def source(case):
    return Path(f"commissioning_{case}_test.txt").read_text()


class CommissioningTests(unittest.TestCase):
    def final(self, case):
        return main.finalize_customer_analysis(raw_case(), source(case), 1)

    def test_registry(self):
        self.assertEqual(main.ANALYSIS_MODULES[main.AnalysisModule.COMMISSIONING], COMMISSIONING_RULES)
        self.assertEqual(set(main.ANALYSIS_MODULES), set(main.AnalysisModule))
        self.assertNotIn("COMPRESSOR REPAIR ANALYSIS RULES", COMMISSIONING_RULES)

    def test_material_routing(self):
        for text in ("Full-system replacement", "New installation", "Replace furnace",
                     "Install heat pump", "Ductless mini-split installation", "Ducted mini-split installation",
                     "Package unit replacement", "Install VRF system", "Multi-zone installation",
                     "Major control conversion", "Compressor replacement"):
            with self.subTest(text=text):
                self.assertTrue(commissioning_required(text))

    def test_minor_repairs(self):
        for text in ("Replace capacitor", "Replace contactor", "Replace furnace igniter",
                     "Flame sensor replacement", "Heat pump capacitor replacement", "Replace blower motor"):
            with self.subTest(text=text):
                self.assertFalse(commissioning_required(text))
        final = main.finalize_customer_analysis(raw_case(), "Replace capacitor only.", 1)
        self.assertFalse(commissioning_items(final))
        self.assertNotIn("How Will Startup Be Verified?", main.build_report_html(final, 1))

    def test_subjects(self):
        for subject in (COMMISSIONING_SUBJECT, "System startup", "Startup verification", "Manufacturer startup",
                        "System checkout", "Final operational verification", "Post-installation testing",
                        "Startup documentation", "Final performance checks", "Commissioning record"):
            self.assertTrue(commissioning_text(subject), subject)
        for subject in ("Duct adequacy", "Equipment matching", "System sizing", "Refrigerant diagnosis", "Blower failure diagnosis"):
            self.assertFalse(commissioning_text(subject), subject)

    def test_good_preinstall_without_results(self):
        final = self.final("good")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertFalse(source_plan(source("good"))[-1])
        self.assertIn("How Will Startup Be Verified?", main.build_report_html(final, 1))

    def test_generic_partial(self):
        final = self.final("partial")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertEqual(main.contractor_question_category(final.contractor_questions[0]), "commissioning")
        self.assertIn("does not explain", final.missing_information)
        self.assertIn("startup", final.bottom_line)

    def test_no_startup_is_incomplete_not_absent(self):
        final = main.finalize_customer_analysis(raw_case(), source("partial").replace("Startup and test operation included.", ""), 1)
        self.assertEqual(commissioning_items(final)[0].diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_explicit_exclusion(self):
        final = self.final("bad")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("shutdown", main.build_report_html(final, 1))

    def test_completed_clean(self):
        final = self.final("documented_results")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertTrue(source_plan(source("documented_results"))[-1])

    def test_completed_clean_presentation(self):
        final = self.final("documented_results")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertIn("supplied startup record", final.homeowner_takeaway)
        self.assertIn("completed startup record", final.bottom_line)
        self.assertIn("startup information", final.missing_information)
        self.assertIn("equipment replacement scope and a completed startup record", final.installation_concerns)
        signs = [s for s in final.good_signs if commissioning_text(s)]
        self.assertEqual(signs, ["The supplied startup record documents completion of the applicable startup checks and final operating readings."])
        for text in (final.homeowner_takeaway, final.bottom_line, final.installation_concerns, *signs):
            for phrase in ("guarantee", "perfect", "every", "review scope before approval", "all readings are acceptable"):
                self.assertNotIn(phrase, text)

    def test_completed_presentation_preserves_state_and_other_cases(self):
        from commissioning import present_completed_commissioning
        for case in ("good", "partial", "bad", "documented_results", "failed_result"):
            final = self.final(case)
            before = final.model_dump()
            present_completed_commissioning(final, source(case), main.sizing_assessments(final),
                main.primary_equipment_matching_assessment(final), main.duct_items(final))
            self.assertEqual(final.model_dump(), before)

    def test_completed_presentation_requires_more_than_attached_sheet(self):
        from commissioning import present_completed_commissioning
        final = self.final("documented_results")
        commissioning_items(final)[0].documented_evidence = ["Startup sheet attached."]
        before = final.model_dump()
        present_completed_commissioning(final, source("documented_results"), main.sizing_assessments(final),
            main.primary_equipment_matching_assessment(final), main.duct_items(final))
        self.assertEqual(final.model_dump(), before)
        self.assertEqual(source_plan("Startup sheet attached.")[1], "PARTIALLY_DEFINED")

    def test_completed_presentation_cannot_override_domain_conflict(self):
        raw = raw_case("bad")
        final = main.finalize_customer_analysis(raw, source("documented_results"), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(commissioning_items(final)[0].scope_support, "APPROPRIATE")
        self.assertNotIn("No material issue in these reviewed areas", final.bottom_line)

    def test_failed_record(self):
        final = self.final("failed_result")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(len(final.contractor_questions), 1)

    def test_failed_record_section_roles_and_optional_question(self):
        from commissioning import commissioning_paragraphs
        raw = raw_case()
        raw.contractor_questions = ["What will be done to correct the failed startup check before closing out the job?",
            "Are there additional tests or inspections planned to ensure the system operates safely and efficiently?"]
        raw.missing_information = "All relevant information appears to be included."
        raw.good_signs = ["The replacement is a planned, homeowner-requested upgrade.", "Comprehensive documentation is supplied."]
        final = main.finalize_customer_analysis(raw, source("failed_result"), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["commissioning"])
        self.assertIn("correct and retest", final.contractor_questions[0])
        self.assertNotIn("additional tests", " ".join(final.contractor_questions))
        self.assertEqual(final.missing_information, "The startup record does not show that the failed safety-shutdown check was corrected and retested.")
        self.assertLess(len(final.banner_explanation.split()), 25)
        section = " ".join(commissioning_paragraphs(final))
        self.assertIn("completed startup record", section)
        self.assertNotIn(section, final.homeowner_takeaway)
        self.assertNotIn(section, final.missing_information)
        self.assertNotEqual(final.red_flags[0], final.banner_explanation)
        self.assertIn("before accepting", final.bottom_line)
        self.assertFalse(any(commissioning_text(s) for s in final.good_signs))
        self.assertNotIn("homeowner-requested", " ".join(final.good_signs))
        self.assertNotIn("Comprehensive", " ".join(final.good_signs))

    def test_failed_presentation_preserves_other_records_and_evidence(self):
        from commissioning import present_failed_commissioning
        for case in ("good", "partial", "bad", "documented_results", "failed_result"):
            final = self.final(case)
            before = final.model_dump()
            present_failed_commissioning(final, source(case))
            self.assertEqual(before, final.model_dump())
        raw = raw_case()
        raw.technical_assessments.append(main.TechnicalEvidenceAssessment(subject="Independent control uncertainty",
            materiality="MATERIAL_SECONDARY", diagnostic_evidence_status="INCOMPLETE", scope_support="PARTIALLY_DEFINED",
            material_gaps=["Control compatibility is not established."]))
        raw.contractor_questions = ["Can you confirm the control compatibility?"]
        final = main.finalize_customer_analysis(raw, source("failed_result"), 1)
        self.assertIn("compatibility", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_corrected_failed_check(self):
        text = source("documented_results") + "\nRequired startup check failed, corrected and retest passed."
        self.assertEqual(source_plan(text)[1], "APPROPRIATE")

    def test_deep_copy_and_idempotence(self):
        raw = raw_case()
        before = raw.model_dump()
        final = main.finalize_customer_analysis(raw, source("partial"), 1)
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(len(commissioning_items(final)), 1)
        again = main.finalize_customer_analysis(final, source("partial"), 1)
        self.assertEqual(final.model_dump(), again.model_dump())

    def test_legacy_cleanup_cannot_remove_gap(self):
        for case in ("partial", "bad"):
            final = self.final(case)
            before = final.model_dump()
            main.normalize_documented_replacement_startup(final, source("good"))
            self.assertEqual(final.model_dump(), before)

    def test_semantic_questions_deduplicated(self):
        raw = raw_case()
        raw.contractor_questions = ["What startup checks are included?", "Can I receive the commissioning record?"]
        final = main.finalize_customer_analysis(raw, source("partial"), 1)
        self.assertEqual(len(final.contractor_questions), 1)

    def test_manufacturer_and_sheet_distinctions(self):
        self.assertEqual(source_plan("Startup per manufacturer instructions.")[1], "PARTIALLY_DEFINED")
        self.assertEqual(source_plan("Complete manufacturer startup procedure and document final readings.")[1], "APPROPRIATE")
        self.assertFalse(source_plan("Startup sheet will be provided")[-1])
        self.assertTrue(source_plan("Startup sheet attached")[-1])
        self.assertEqual(source_plan("Factory startup")[1], "PARTIALLY_DEFINED")

    def test_domain_readings_do_not_change_startup_status(self):
        for reading in ("Static pressure 1.8 in. w.c.", "Superheat 40 F.", "Voltage 208V; breaker 20A.", "Combustion CO 300 ppm."):
            self.assertEqual(source_plan(source("documented_results") + reading)[1], "APPROPRIATE")

    def test_independent_domain_unsupported_stays_unsupported(self):
        raw = raw_case("bad")
        final = main.finalize_customer_analysis(raw, source("bad"), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(raw.technical_assessments[0].model_dump(), final.technical_assessments[0].model_dump())

    def test_no_universal_checklist_or_factory_claims(self):
        for case in ("good", "partial", "documented_results"):
            final = self.final(case)
            text = " ".join([final.missing_information, *final.contractor_questions, *final.good_signs])
            for phrase in ("must provide superheat", "factory certified", "warranty activated", "commissioning methodology"):
                self.assertNotIn(phrase, text)

    def test_pricing_independent(self):
        final = self.final("partial")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")

    def test_unchanged_legacy_fixtures_have_new_whole_report_gap(self):
        # No isolation mocks: previously accepted domain findings remain supported,
        # but generic startup wording no longer proves the whole report is PROCEED.
        for filename in ("duct_airflow_good_test.txt", "duct_airflow_return_restriction_test.txt",
                         "system_sizing_good_test.txt", "equipment_matching_good_test.txt",
                         "replacement_basis_good_test.txt"):
            with self.subTest(filename=filename):
                text = Path(filename).read_text()
                final = main.finalize_customer_analysis(raw_case(), text, 1)
                item = commissioning_items(final)[0]
                self.assertEqual(item.diagnostic_evidence_status, "INCOMPLETE")
                self.assertEqual(item.scope_support, "PARTIALLY_DEFINED")
                self.assertNotEqual(final.decision.verdict, "PROCEED")

    def test_actual_legacy_duct_good_only_new_startup_gap(self):
        final = main.finalize_customer_analysis(raw_case(), Path("duct_airflow_good_test.txt").read_text(), 1)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(len(final.contractor_questions), 1)
        for item in final.technical_assessments:
            if not commissioning_text(item.subject):
                self.assertEqual(item.scope_support, "APPROPRIATE")

    def test_source_routing_recovers_classifier_omission_without_mutation(self):
        classification = main.QuoteClassification(quote_type="replacement", system_type="ductless",
            primary_scope="Install mini-split", components=[], diagnoses=[], scope_items=[], missing_info=[],
            modules_required=[], confidence="high")
        before = classification.model_dump()
        knowledge = main.get_analysis_knowledge(classification, "Install a ductless mini-split.")
        self.assertIn(COMMISSIONING_RULES.strip(), knowledge)
        self.assertEqual(classification.model_dump(), before)

    def test_noncanonical_assessment_replaced_once(self):
        raw = raw_case()
        raw.technical_assessments.extend([
            main.TechnicalEvidenceAssessment(subject=subject, materiality="PRIMARY",
                diagnostic_evidence_status="ABSENT", scope_support="UNSUPPORTED")
            for subject in ("System checkout", "Manufacturer startup")])
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertEqual(len(commissioning_items(final)), 1)
        self.assertEqual(commissioning_items(final)[0].subject, COMMISSIONING_SUBJECT)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_no_startup_listed_is_not_explicit_exclusion(self):
        self.assertEqual(source_plan("No startup listed.")[1], "PARTIALLY_DEFINED")
        self.assertEqual(source_plan("No startup/testing will be performed.")[1], "UNSUPPORTED")

    def test_documentation_exclusion_does_not_equal_testing_exclusion(self):
        self.assertEqual(source_plan("Startup per manufacturer instructions. Startup sheet will not be provided.")[1], "PARTIALLY_DEFINED")

    def test_equipment_appropriate_plans(self):
        for equipment in ("ductless mini-split", "package unit", "VRF system", "electric air handler"):
            text = f"Install {equipment}. Complete manufacturer startup procedure and document final readings."
            self.assertEqual(source_plan(text)[1], "APPROPRIATE")

    def test_does_not_demand_unsupported_optional_tests(self):
        raw = raw_case()
        raw.technical_assessments.append(main.TechnicalEvidenceAssessment(
            subject="Startup plan", materiality="PRIMARY", diagnostic_evidence_status="ABSENT",
            scope_support="UNSUPPORTED", material_gaps=["Final combustion and static results do not exist yet."]))
        raw.red_flags = ["Startup lacks final temperature rise and combustion readings."]
        raw.contractor_questions = ["Why are the startup combustion readings missing?"]
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertEqual(final.decision.verdict, "PROCEED")

    def test_generic_whole_system_question_dedup(self):
        raw = raw_case()
        raw.contractor_questions = ["How will you verify heating and cooling operation after installation?",
                                    "What startup checks are included?"]
        final = main.finalize_customer_analysis(raw, source("partial"), 1)
        self.assertEqual(len(final.contractor_questions), 1)

    def test_different_domain_questions_are_not_startup(self):
        for q in ("How will you verify refrigerant charge after repair?", "What explains the high duct static pressure?",
                  "How was the failed blower motor diagnosed?", "Does the breaker match the required rating?"):
            self.assertNotEqual(main.contractor_question_category(q), "commissioning")

    def test_pricing_question_survives_after_startup_question(self):
        text = source("partial").replace("Equipment: $11,000.", "").replace("Labor and installation materials: $7,000.", "")
        raw = raw_case()
        raw.decision.pricing_transparency = "LIMITED"
        final = main.finalize_customer_analysis(raw, text, 1)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["commissioning", "pricing"])

    def test_source_failure_survives_supported_ai(self):
        raw = raw_case()
        raw.technical_assessments.append(main.TechnicalEvidenceAssessment(
            subject=COMMISSIONING_SUBJECT, materiality="PRIMARY", diagnostic_evidence_status="ADEQUATE", scope_support="APPROPRIATE"))
        final = main.finalize_customer_analysis(raw, source("failed_result"), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_nonstartup_assessments_not_mutated_by_commissioning(self):
        from commissioning import normalize_commissioning_assessments
        raw = raw_case("bad")
        before = [a.model_dump() for a in raw.technical_assessments]
        normalize_commissioning_assessments(raw, source("good"), main.TechnicalEvidenceAssessment)
        self.assertEqual([a.model_dump() for a in raw.technical_assessments[:-1]], before)

    def test_visible_completed_and_planned_language_distinct(self):
        self.assertIn("final readings are not expected", main.build_report_html(self.final("good"), 1))
        self.assertIn("supplied startup record documents completed", main.build_report_html(self.final("documented_results"), 1))

    def test_real_upload_path(self):
        classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
            primary_scope="Full system replacement", components=[], diagnoses=[], scope_items=[], missing_info=[], modules_required=[], confidence="high")
        # Reuse the valid model shape instead of calling any external service.
        for case, verdict in (("good", "PROCEED"), ("partial", "REVIEW BEFORE APPROVING"), ("bad", "GET A SECOND OPINION")):
            parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=raw_case()))])
            with patch.object(main, "classify_quotes", return_value=classification), patch.object(main.client.beta.chat.completions, "parse", return_value=parsed), patch.object(main, "send_review_email"):
                response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"}, files={"files": (f"commissioning_{case}_test.txt", source(case).encode(), "text/plain")})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("How Will Startup Be Verified?", response.text)
            self.assertIn(verdict, response.text)
            self.assertNotIn(f"commissioning_{case}_test.txt", response.text)
            self.assertNotIn("File Name:", response.text)

    def test_good_presentation_preserves_all_startup_facts(self):
        from commissioning import commissioning_paragraphs
        final = self.final("good")
        prose = " ".join(commissioning_paragraphs(final))
        for fact in ("manufacturer startup procedure", "heating and cooling operation", "controls and safeties",
                     "final operating readings", "correct startup deficiencies before closeout", "provide a startup sheet",
                     "system has not been installed"):
            self.assertIn(fact, prose)
        self.assertNotIn("Planned startup:", prose)
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])

    def test_good_whole_report_presentation(self):
        raw = raw_case()
        raw.good_signs = ["A planned homeowner-requested upgrade."]
        raw.missing_information = "All necessary information is presented clearly."
        raw.installation_concerns = "The installation scope appears complete."
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertIn("startup plan", final.homeowner_takeaway)
        self.assertIn("duct measurements", final.homeowner_takeaway)
        self.assertIn("startup plan", final.bottom_line)
        self.assertIn("document", final.bottom_line)
        self.assertIn("No material sizing, duct or startup", final.missing_information)
        self.assertIn("removal of the existing equipment", final.installation_concerns)
        self.assertIn("installation of the new equipment", final.installation_concerns)
        signs = " ".join(final.good_signs)
        self.assertIn("final operating readings", signs)
        self.assertIn("0.62", signs)
        self.assertIn("1,100", signs)
        self.assertNotIn("homeowner-requested", signs)
        for value in (final.homeowner_takeaway, final.bottom_line, final.installation_concerns, signs):
            for phrase in ("guarantee", "scope appears complete", "all necessary", "readings passed"):
                self.assertNotIn(phrase, value)

    def test_presentation_does_not_mutate_technical_state(self):
        from commissioning import present_supported_commissioning
        for case in ("good", "partial", "bad", "documented_results", "failed_result"):
            final = self.final(case)
            before = final.model_dump()
            present_supported_commissioning(final, source(case), main.sizing_assessments(final),
                main.primary_equipment_matching_assessment(final), main.duct_items(final))
            after = final.model_dump()
            for field in ("decision", "technical_assessments", "contractor_questions", "market_price_context", "red_flags"):
                self.assertEqual(before[field], after[field])
            if case != "good":
                self.assertEqual(before, after)

    def test_metadata_filter_preserves_legitimate_document_references(self):
        from commissioning import customer_source_text
        text = ("File Name: commissioning_good_test.txt\nLocal Path: /tmp/quote.txt\n"
                "Manufacturer Startup Guide S-17.pdf; AHRI reference 123456; EX-C36. Proposal: Smith HVAC replacement.")
        clean = customer_source_text(text)
        self.assertNotIn("commissioning_good_test", clean)
        self.assertNotIn("/tmp/", clean)
        for reference in ("Manufacturer Startup Guide S-17.pdf", "AHRI reference 123456", "EX-C36", "Smith HVAC replacement"):
            self.assertIn(reference, clean)

    def test_partial_fixture_production_upload_only_startup_unresolved(self):
        text = source("partial")
        raw = raw_case()
        # Reproduce the live failure: NO supported duct assessment in the AI result.
        raw.technical_assessments = [a for a in raw.technical_assessments if a not in main.duct_items(raw)]
        raw.technical_assessments.append(main.TechnicalEvidenceAssessment(
            subject=COMMISSIONING_SUBJECT, materiality="PRIMARY", diagnostic_evidence_status="INCOMPLETE",
            scope_support="PARTIALLY_DEFINED", material_gaps=["Startup checks and documentation are unclear."]))
        raw.missing_information = "All necessary performance metrics and documentation appear to be present."
        raw.installation_concerns = "The installation scope seems appropriate and complete."
        raw.good_signs = ["The proposal clearly identifies the replacement as a planned, homeowner-requested upgrade."]
        before = raw.model_dump()
        classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
            primary_scope="Full system replacement", components=[], diagnoses=[], scope_items=[], missing_info=[],
            modules_required=[main.AnalysisModule.COMMISSIONING, main.AnalysisModule.DUCT_AIRFLOW,
                              main.AnalysisModule.SYSTEM_SIZING, main.AnalysisModule.EQUIPMENT_MATCHING,
                              main.AnalysisModule.REPAIR_VS_REPLACE], confidence="high")
        parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=raw))])
        # Mock only external AI/email; no completeness or finalization bypass.
        with patch.object(main, "classify_quotes", return_value=classification), patch.object(
                main.client.beta.chat.completions, "parse", return_value=parsed), patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                files={"files": ("commissioning_partial_test.txt", text.encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(final.red_flags, [])
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["commissioning"])
        self.assertEqual(len(final.technical_assessments), 5)
        for item in final.technical_assessments:
            if commissioning_text(item.subject):
                self.assertEqual((item.diagnostic_evidence_status, item.scope_support), ("INCOMPLETE", "PARTIALLY_DEFINED"))
            else:
                self.assertIn(item.diagnostic_evidence_status, {"ADEQUATE", "CONFIRMED"})
                self.assertEqual(item.scope_support, "APPROPRIATE")
        self.assertIn("after installation", final.homeowner_takeaway)
        self.assertIn("startup checks", final.bottom_line)
        self.assertNotIn("duct", final.missing_information.lower())
        self.assertNotIn("All necessary", final.missing_information)
        self.assertEqual(final.installation_concerns, "The proposal includes removal of the existing equipment and installation of the new equipment.")
        self.assertFalse(any(commissioning_text(s) or "homeowner-requested" in s for s in final.good_signs))
        self.assertIn("How Will Startup Be Verified?", response.text)
        self.assertNotIn("commissioning_partial_test.txt", response.text)
        self.assertNotIn("provide final readings before", response.text.lower())
        from duct_airflow import duct_paragraphs
        duct_prose = " ".join(duct_paragraphs(final))
        self.assertEqual(duct_prose.count("0.62"), 1)
        self.assertEqual(duct_prose.count("1,100 CFM"), 2)  # measured and target, once each
        for phrase in ("recovery", "fallback", "duct-review measurements", "fan table"):
            self.assertNotIn(phrase, duct_prose)

    def test_partial_duct_presentation_deduplicates_without_mutation(self):
        from duct_airflow import duct_paragraphs
        final = self.final("partial")
        item = main.duct_items(final)[0]
        item.documented_evidence *= 3
        before = final.model_dump()
        prose = " ".join(duct_paragraphs(final))
        self.assertEqual(prose.count("0.62"), 1)
        self.assertEqual(prose.count("1,100 CFM"), 2)
        self.assertEqual(before, final.model_dump())
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["commissioning"])

    def test_bad_live_question_contamination_through_upload(self):
        raw = raw_case()
        raw.technical_assessments = [a for a in raw.technical_assessments if a not in main.duct_items(raw)]
        raw.contractor_questions = [
            "Will you include the startup check required by the submitted documentation?",
            "What is your plan to address the exclusion of the final safety shutdown verification?",
            "Can you clarify the warranty coverage for the proposed equipment?"]
        raw.missing_information = "Missing manufacturer warranty details. All necessary documentation is present."
        raw.installation_concerns = "This is a critical verification test necessary before the system can be deemed fully operational."
        raw.red_flags = ["The final safety shutdown verification is excluded."]
        raw.good_signs = ["The proposal clearly identifies the replacement as a planned, homeowner-requested upgrade.",
                          "Appropriate capacity ratings align with the home's cooling and heating requirements.",
                          "Submitted manufacturer documentation supports the matched equipment combination."]
        before = raw.model_dump()
        classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
            primary_scope="Full replacement", modules_required=[main.AnalysisModule.COMMISSIONING], confidence="high")
        parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=raw))])
        with patch.object(main, "classify_quotes", return_value=classification), patch.object(main.client.beta.chat.completions,
                "parse", return_value=parsed), patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                files={"files": ("commissioning_bad_test.txt", source("bad").encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(len(final.technical_assessments), 5)
        for a in final.technical_assessments:
            self.assertEqual(a.scope_support, "UNSUPPORTED" if commissioning_text(a.subject) else "APPROPRIATE")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["commissioning"])
        self.assertIn("safety-shutdown", final.contractor_questions[0])
        self.assertEqual(final.missing_information, final.red_flags[0])
        self.assertNotIn("warranty", final.missing_information.lower())
        self.assertIn("startup requirement", final.bottom_line)
        self.assertNotIn("homeowner-requested", " ".join(final.good_signs))
        self.assertNotIn("capacity ratings align", " ".join(final.good_signs))
        self.assertNotIn("fully operational", final.installation_concerns)

    def test_bad_preserves_independent_material_warranty_gap(self):
        raw = raw_case()
        raw.technical_assessments.append(main.TechnicalEvidenceAssessment(subject="Warranty exclusion",
            materiality="MATERIAL_SECONDARY", diagnostic_evidence_status="INCOMPLETE", scope_support="PARTIALLY_DEFINED",
            material_gaps=["Clarify the material labor warranty exclusion."]))
        raw.contractor_questions = ["Does the warranty exclusion apply to labor?"]
        raw.missing_information = "The labor warranty exclusion needs clarification."
        final = main.finalize_customer_analysis(raw, source("bad"), 1)
        self.assertIn("warranty", [main.contractor_question_category(q) for q in final.contractor_questions])
        self.assertIn("warranty", final.missing_information)

    def test_excluded_check_question_semantic_purpose(self):
        for q in ("Will you include the required startup check?",
                  "What is your plan to address the exclusion of the final safety shutdown verification?",
                  "Will you complete required startup verification?"):
            self.assertEqual(main.contractor_question_category(q), "commissioning")

    def test_supported_duct_presentation_uses_dynamic_values(self):
        from duct_airflow import duct_paragraphs
        final = self.final("partial")
        item = main.duct_items(final)[0]
        item.documented_evidence = [s.replace("0.62", "0.57").replace("0.80", "0.75").replace("1,100", "1,250") for s in item.documented_evidence]
        prose = " ".join(duct_paragraphs(final))
        self.assertEqual(prose.count("0.57"), 1)
        self.assertEqual(prose.count("1,250 CFM"), 2)
        self.assertIn("0.75", prose)

    def test_partial_presentation_leaves_other_cases_and_duct_gaps_alone(self):
        from commissioning import present_partial_commissioning
        for case in ("good", "bad", "documented_results", "failed_result"):
            final = self.final(case)
            before = final.model_dump()
            present_partial_commissioning(final, source(case), main.sizing_assessments(final),
                main.primary_equipment_matching_assessment(final), main.duct_items(final))
            self.assertEqual(final.model_dump(), before)
        raw = raw_case("partial")
        final = main.finalize_customer_analysis(raw, Path("duct_airflow_partial_test.txt").read_text(), 1)
        self.assertEqual(main.duct_items(final)[0].scope_support, "PARTIALLY_DEFINED")
        self.assertIn("duct_airflow", [main.contractor_question_category(q) for q in final.contractor_questions])


if __name__ == "__main__":
    unittest.main()
