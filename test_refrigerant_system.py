"""Offline Phase 2G: source recovery, independent evidence and real renderer paths."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
import main
import refrigerant_system as rs
from test_technical_support_derivation import analysis_with, assessment


def source(case):
    return Path(f"refrigerant_system_{case}_test.txt").read_text()


def raw(case):
    result = analysis_with([], ai_support="SUPPORTED")
    if case == "cross_module":
        result.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            subject="Startup and commissioning plan", gaps=["Startup documentation is not promised."]))
    return result


class RefrigerantSystemTests(unittest.TestCase):
    def final(self, case):
        return main.finalize_customer_analysis(raw(case), source(case), 1)

    def test_registry(self):
        self.assertEqual(main.ANALYSIS_MODULES[main.AnalysisModule.REFRIGERANT_SYSTEM], rs.REFRIGERANT_SYSTEM_RULES)
        self.assertEqual(set(main.ANALYSIS_MODULES), set(main.AnalysisModule))
        self.assertIn("A test name is not a test result", rs.REFRIGERANT_SYSTEM_RULES)

    def test_route_material(self):
        for text in ("Diagnosis: low refrigerant", "Suspected refrigerant leak", "Replace leaking evaporator coil",
                     "Replace restricted TXV", "Recharge after repair", "The circuit was opened for repair"):
            self.assertTrue(rs.refrigerant_required(text), text)

    def test_type_and_startup_mentions_do_not_route(self):
        for text in ("R-410A", "R-454B refrigerant", "R-32 heat pump", "Factory charge 6 pounds",
                     "Charge per manufacturer", "Equipment refrigerant compatibility", "Lineset size 3/4 inch"):
            self.assertFalse(rs.refrigerant_required(text), text)
            final = main.finalize_customer_analysis(analysis_with([]), text, 1)
            self.assertFalse(rs.refrigerant_items(final))
            self.assertNotIn("What Does the Refrigerant Evidence Show?", main.build_report_html(final, 1))

    def test_semantic_subjects(self):
        for name in (*rs.SUBJECTS, "Low refrigerant", "Low charge", "Refrigerant loss", "Leak source",
                     "Evaporator leak", "TXV", "Restriction", "Charging issue", "Charge verification", "System charge"):
            self.assertIsNotNone(rs.subject_kind(name), name)
        for name in ("Compressor electrical failure", "Duct airflow", "System sizing", "Equipment matching",
                     "Startup verification", "Pricing transparency"):
            self.assertIsNone(rs.subject_kind(name), name)

    def test_good(self):
        final = self.final("good")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])

    def test_partial(self):
        final = self.final("partial")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.contractor_questions), 1)

    def test_diagnostic_only_partial_production_path(self):
        original = analysis_with([assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=rs.CONDITION,
            evidence=["Suction pressure: 102 PSIG.", "Superheat: 29 F."],
            gaps=["Refrigerant repair not established."])], pricing="LIMITED")
        original.missing_information = "1. 2. Refrigerant repair evidence missing."
        original.pricing_review = "Request an itemized breakdown of refrigerant and labor."
        original.contractor_questions = ["What refrigerant readings establish the condition?",
                                        "How will refrigerant work be verified?", "Can you itemize the price?",
                                        "What steps will be taken to verify the success of any recharging or repairs after initial investigation?"]
        before = original.model_dump()
        parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=original))])
        classification = main.QuoteClassification(quote_type="repair", system_type="AC",
            primary_scope="Refrigerant investigation", modules_required=[main.AnalysisModule.REFRIGERANT_SYSTEM])
        with patch.object(main, "classify_quotes", return_value=classification), patch.object(
                main.client.beta.chat.completions, "parse", return_value=parsed), patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                files={"files": ("refrigerant_system_partial_test.txt", source("partial").encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(original.model_dump(), before)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("diagnostic visit", final.contractor_questions[0])
        self.assertNotEqual(main.contractor_question_category(final.contractor_questions[0]), "pricing")
        for value in ("102 PSIG", "29 °F", "quoted work is for further diagnosis"):
            self.assertIn(value, response.text)
        for value in ("1. 2.", "itemized breakdown of the refrigerant", "target superheat"):
            self.assertNotIn(value, response.text)
        self.assertIn("not a repair", final.homeowner_takeaway)
        self.assertNotIn("verify the success", response.text)
        self.assertEqual(final.model_dump(), main.finalize_customer_analysis(final, source("partial"), 1).model_dump())

    def test_diagnostic_only_removes_future_repair_verification_purposes(self):
        final = self.final("partial")
        expected = list(final.contractor_questions)
        for question in (
            "What steps will be taken to verify the success of any recharging or repairs after initial investigation?",
            "How will you confirm that a future repair is effective?",
            "What follow-up testing will confirm successful recharging?",
        ):
            with self.subTest(question=question):
                candidate = final.model_copy(deep=True)
                candidate.contractor_questions.append(question)
                self.assertEqual(main.build_contractor_questions(candidate, 1, source("partial")), expected)

    def test_actual_repair_retains_material_verification_question(self):
        original = analysis_with([assessment("INCOMPLETE", "PARTIALLY_DEFINED", subject=rs.VERIFICATION,
            gaps=["Post-repair refrigerant results need clarification."])])
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.assertIn("refrigerant_verification", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_diagnostic_price_does_not_hide_specific_repair(self):
        self.assertIsNone(rs.diagnostic_only_scope(source("partial") + "\nRecommended repair: Replace evaporator coil."))

    def test_diagnostic_fee_preserves_independent_pricing_gap(self):
        original = raw("partial")
        original.decision.pricing_transparency = "LIMITED"
        original.pricing_review = "Additional fees are unclear."
        final = main.finalize_customer_analysis(original, source("partial"), 1)
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
        self.assertIn("pricing", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_bad(self):
        final = self.final("bad")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("valve", " ".join(rs.refrigerant_paragraphs(final)))

    def test_bad_live_shape_through_upload(self):
        original = analysis_with([
            assessment("ABSENT", "UNSUPPORTED", subject=rs.CAUSE,
                contradictions=["Justification for not repairing the leaking valve.",
                                "Confirmation that the evaporator coil has failed and requires replacement."]),
            assessment(subject="Startup and commissioning plan", evidence=["Manufacturer startup and final readings."]),
        ])
        original.missing_information = "The leak location is unclear."
        original.good_signs = ["The proposed startup work includes documentation of the checks after installation."]
        original.contractor_questions = ["Where was the leak confirmed?", "How will startup be verified?", "Is there a warranty?"]
        before = original.model_dump()
        parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=original))])
        classification = main.QuoteClassification(quote_type="repair", system_type="AC", primary_scope="Evaporator coil replacement",
            modules_required=[main.AnalysisModule.REFRIGERANT_SYSTEM, main.AnalysisModule.COMMISSIONING])
        with patch.object(main, "classify_quotes", return_value=classification), patch.object(
                main.client.beta.chat.completions, "parse", return_value=parsed), patch.object(main, "send_review_email") as email:
            response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                files={"files": ("refrigerant_system_bad_test.txt", source("bad").encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(before, original.model_dump())
        self.assertEqual(rs.refrigerant_items(final)[0].scope_support, "UNSUPPORTED")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("evaporator coil", final.contractor_questions[0])
        for phrase in ("How Will Startup Be Verified?", "leak location is unclear", "Justification for not repairing",
                       "Confirmation that the evaporator", "target superheat"):
            self.assertNotIn(phrase, response.text)
        for phrase in ("liquid-line service valve", "$4,850", "$3,000", "$1,850"):
            self.assertIn(phrase, response.text)
        self.assertFalse(any("startup" in s.lower() for s in final.good_signs))
        self.assertEqual(final.model_dump(), main.finalize_customer_analysis(final, source("bad"), 1).model_dump())

    def test_independent_failed_startup_not_suppressed_for_coil_work(self):
        import commissioning
        text = source("bad") + "\nCompleted startup record: required safety check failed."
        self.assertTrue(commissioning.commissioning_required(text))

    def test_circuit_work(self):
        final = self.final("circuit_work")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.contractor_questions, [])
        self.assertEqual(final.red_flags, [])
        self.assertIn("opened", " ".join(rs.refrigerant_paragraphs(final)))

    def test_cross_module(self):
        final = self.final("cross_module")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(rs.refrigerant_items(final)[0].scope_support, "APPROPRIATE")
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["commissioning"])
        self.assertIn("supports the proposed repair", " ".join(rs.refrigerant_paragraphs(final)))
        self.assertFalse(any("low-charge" in f for f in final.red_flags))

    def test_raw_immutable_and_idempotent(self):
        for case in ("good", "partial", "bad", "circuit_work", "cross_module"):
            original = raw(case)
            before = original.model_dump()
            final = main.finalize_customer_analysis(original, source(case), 1)
            self.assertEqual(before, original.model_dump())
            self.assertEqual(final.model_dump(), main.finalize_customer_analysis(final, source(case), 1).model_dump())

    def test_good_evidence_hierarchy_and_visible_readings(self):
        final = self.final("good")
        prose = " ".join(rs.refrigerant_paragraphs(final))
        self.assertLess(prose.index("leak detector"), prose.index("102 PSIG"))
        for value in ("102 PSIG", "2 °F", "29 °F", "equipment and test conditions"):
            self.assertIn(value, prose)
            self.assertIn(value, main.build_report_html(final, 1))
        self.assertNotIn("target", prose)

    def test_good_copy_is_scoped_and_factual(self):
        original = raw("good")
        original.equipment_analysis = "This evidence is solid and logically follows."
        original.missing_information = "All necessary diagnostic details are provided."
        original.installation_concerns = "Adequate steps ensure the repair is completed successfully."
        original.good_signs = ["Documented refrigerant findings support the proposed work.",
                               "A leak at the service valve is documented.",
                               "The warranty provides assurance."]
        final = main.finalize_customer_analysis(original, source("good"), 1)
        text = " ".join([final.equipment_analysis, final.missing_information,
                         final.installation_concerns, *final.good_signs])
        for phrase in ("solid", "logically follows", "all necessary", "ensure", "successfully", "assurance"):
            self.assertNotIn(phrase, text.lower())
        self.assertEqual(sum("leak" in s.lower() for s in final.good_signs), 1)
        self.assertIn("The proposal includes a 1-year parts and labor warranty.", final.good_signs)
        self.assertIn("actual leak at the liquid-line service valve", final.homeowner_takeaway)
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])

    def test_no_targets_invented_in_leak_presentation(self):
        evidence = ["Electronic leak detector confirmed a leak at the liquid-line service valve.",
                    "Measured suction pressure: 102 PSIG.", "Measured subcooling: 2 F.",
                    "Measured superheat: 29 F."]
        _, prose, _ = rs.located_leak_presentation(evidence)
        self.assertNotIn("target", prose)
        self.assertIn("exact interpretation depends", prose)
        self.assertNotIn("proves low charge", prose)

    def test_good_presenter_does_not_mutate_assessments_or_decision(self):
        final = self.final("good")
        before = [a.model_dump() for a in final.technical_assessments], final.decision.model_dump()
        rs.compose_refrigerant_summary(final, source("good"))
        self.assertEqual(before, ([a.model_dump() for a in final.technical_assessments], final.decision.model_dump()))

    def test_keywords_are_not_results(self):
        for text in ("Low charge. Superheat and subcooling should be tested. Recharge proposed.",
                     "Suspected refrigerant leak. Oil residue. Recharge proposed.",
                     "Replace TXV because suction is low.", "Replace coil because refrigerant is low."):
            self.assertEqual(rs.source_facts(text)["scope_support"], "PARTIALLY_DEFINED")

    def test_planned_measurements_not_completed(self):
        text = "Will measure subcooling: 2 F.\nWill measure superheat: 29 F.\nRecharge proposed; suspect low refrigerant."
        self.assertEqual(rs.source_facts(text)["scope_support"], "PARTIALLY_DEFINED")

    def test_repeated_measurement_is_not_multiple_independent_results(self):
        text = "Suction pressure: 102 PSIG.\nSuction pressure: 102 PSIG.\nReadings consistent with undercharged system. Recharge proposed."
        self.assertEqual(rs.source_facts(text)["scope_support"], "PARTIALLY_DEFINED")

    def test_targets_are_not_actual_measurements(self):
        text = "Manufacturer target subcooling: 10 F.\nManufacturer target superheat: 12 F.\nFindings consistent with undercharged system. Recharge proposed."
        self.assertEqual(rs.source_facts(text)["scope_support"], "PARTIALLY_DEFINED")

    def test_negated_leak_result_is_not_confirmation(self):
        text = "Electronic leak detector did not confirm a leak at evaporator coil. Replace evaporator coil."
        self.assertEqual(rs.source_facts(text)["scope_support"], "PARTIALLY_DEFINED")

    def test_negative_charge_interpretation_preserved(self):
        text = "Subcooling: 2 F.\nSuperheat: 29 F.\nReadings are not consistent with an undercharged system. Recharge proposed."
        self.assertEqual(rs.source_facts(text)["scope_support"], "UNSUPPORTED")

    def test_located_leak_does_not_itself_prove_low_charge(self):
        text = "Low refrigerant diagnosis. Electronic leak detector confirmed a leak at service valve. Replace valve and recharge."
        self.assertEqual(rs.source_facts(text)["scope_support"], "PARTIALLY_DEFINED")

    def test_supported_test_names_are_not_results(self):
        original = analysis_with([assessment(subject=rs.CONDITION, evidence=["Superheat", "Subcooling"])])
        final = main.finalize_customer_analysis(original, "Low refrigerant diagnosis. Recharge proposed. Superheat and subcooling tests planned.", 1)
        self.assertEqual(rs.refrigerant_items(final)[0].scope_support, "PARTIALLY_DEFINED")

    def test_no_cross_quote_source_recovery(self):
        facts = rs.source_facts("QUOTE 1\n" + source("good") + "\nQUOTE 2\n" + source("partial"))
        self.assertNotEqual(facts["scope_support"], "APPROPRIATE")

    def test_independent_duct_gap_does_not_create_refrigerant_flag(self):
        original = raw("good")
        original.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED",
            subject="Duct and airflow support for proposed equipment", gaps=["Duct review not documented."]))
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(rs.refrigerant_items(final)[0].scope_support, "APPROPRIATE")
        self.assertFalse(any("refrigerant" in f for f in final.red_flags))

    def test_legacy_low_charge_good_source_remains_supported(self):
        facts = rs.source_facts(Path("refrigerant_low_charge_good_test.txt").read_text())
        self.assertEqual(facts["scope_support"], "APPROPRIATE")

    def test_classifier_minor_mention_and_omitted_route(self):
        for text, selected, expected in (("R-454B refrigerant. Charge per manufacturer.", [main.AnalysisModule.REFRIGERANT_SYSTEM], False),
                                         (source("good"), [], True)):
            classification = main.QuoteClassification(quote_type="repair", system_type="AC", primary_scope="Review work",
                modules_required=selected)
            parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=classification))])
            with patch.object(main.client.beta.chat.completions, "parse", return_value=parsed):
                result = main.classify_quotes(text)
            self.assertEqual(main.AnalysisModule.REFRIGERANT_SYSTEM in result.modules_required, expected)

    def test_minor_detail_does_not_downgrade_refrigerant(self):
        original = raw("good")
        original.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED", materiality="MINOR",
            subject=rs.VERIFICATION, gaps=["Optional refrigerant documentation detail."]))
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.contractor_questions, [])

    def test_supported_leak_location_not_invented_from_oil_residue(self):
        result = rs.source_facts("Low refrigerant. Oil residue present, no location documented. Replace evaporator coil.")
        self.assertEqual(result["scope_support"], "PARTIALLY_DEFINED")
        self.assertEqual(result["documented_evidence"], [])

    def test_low_charge_does_not_confirm_coil(self):
        text = source("good").replace("Replace the leaking service valve core and seal", "Replace evaporator coil")
        result = rs.source_facts(text)
        self.assertEqual(result["subject"], rs.COIL)
        self.assertEqual(result["scope_support"], "UNSUPPORTED")

    def test_confirmed_coil_preserved(self):
        text = Path("refrigerant_coil_good_diagnosis_test.txt").read_text()
        facts = rs.source_facts(text)
        self.assertEqual(facts["scope_support"], "APPROPRIATE")
        self.assertTrue(any("Soap bubbles" in s for s in facts["documented_evidence"]))

    def test_suspected_leak_not_promoted(self):
        text = "Suspected refrigerant leak at evaporator coil; no leak location confirmed. Replace evaporator coil."
        facts = rs.source_facts(text)
        self.assertEqual(facts["scope_support"], "PARTIALLY_DEFINED")
        self.assertEqual(facts["documented_evidence"], [])

    def test_metering_strong_legacy_evidence(self):
        facts = rs.source_facts(Path("refrigerant_metering_device_good_test.txt").read_text())
        self.assertEqual(facts["subject"], rs.METERING)
        self.assertEqual(facts["scope_support"], "APPROPRIATE")

    def test_metering_symptoms_not_proof(self):
        facts = rs.source_facts("Restricted TXV. Not cooling. Suction pressure is low. Replace TXV.")
        self.assertEqual(facts["scope_support"], "PARTIALLY_DEFINED")

    def test_structured_contradictions_preserved(self):
        original = analysis_with([assessment("CONTRADICTORY", "UNSUPPORTED", subject=rs.CONDITION,
            contradictions=["Submitted charge measurements contradict the diagnosis."])])
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.assertEqual(rs.refrigerant_items(final)[0].scope_support, "UNSUPPORTED")

    def test_multiple_subconditions_preserved(self):
        original = analysis_with([assessment(subject=rs.CONDITION, evidence=["Measured charge condition is supported."]),
            assessment("INCOMPLETE", "PARTIALLY_DEFINED", materiality="MATERIAL_SECONDARY", subject=rs.CAUSE,
                       gaps=["Leak location remains uncertain."])])
        final = main.finalize_customer_analysis(original, source("good"), 1)
        self.assertEqual(len(rs.refrigerant_items(final)), 2)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.red_flags, [])

    def test_question_purposes(self):
        cases = {"What refrigerant readings support this?": "refrigerant_evidence",
                 "Why was the refrigerant low?": "refrigerant_cause", "Where was the leak located?": "leak_location",
                 "What refrigerant repair scope is included?": "refrigerant_repair_scope",
                 "How will refrigerant charge be verified after repair?": "refrigerant_verification"}
        for question, purpose in cases.items():
            self.assertEqual(main.contractor_question_category(question), purpose)

    def test_customer_prose_no_location_upgrade_or_universal_leak_search(self):
        original = raw("partial")
        original.equipment_analysis = "The evaporator coil is leaking refrigerant."
        original.red_flags = ["Leak search is essential before any recharge."]
        original.good_signs = ["The low-charge diagnosis is confirmed."]
        original.contractor_questions = ["Why was a leak search not included?"]
        final = main.finalize_customer_analysis(original, source("partial"), 1)
        html = main.build_report_html(final, 1)
        self.assertNotIn("coil is leaking", html)
        self.assertNotIn("Leak search is essential", html)
        self.assertNotIn("diagnosis is confirmed", html)

    def test_visible_section(self):
        for case in ("good", "partial", "bad", "circuit_work", "cross_module"):
            self.assertIn("What Does the Refrigerant Evidence Show?", main.build_report_html(self.final(case), 1))

    def test_production_upload_five_cases(self):
        classification = main.QuoteClassification(quote_type="repair", system_type="split AC", primary_scope="Refrigerant work",
            modules_required=[main.AnalysisModule.REFRIGERANT_SYSTEM])
        for case, verdict in (("good", "PROCEED"), ("partial", "REVIEW_BEFORE_APPROVING"),
                              ("bad", "GET_A_SECOND_OPINION"), ("circuit_work", "PROCEED"),
                              ("cross_module", "REVIEW_BEFORE_APPROVING")):
            with self.subTest(case=case):
                original = raw(case)
                parsed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=original))])
                with patch.object(main, "classify_quotes", return_value=classification), patch.object(main.client.beta.chat.completions,
                        "parse", return_value=parsed), patch.object(main, "send_review_email") as email:
                    response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                        files={"files": (f"refrigerant_system_{case}_test.txt", source(case).encode(), "text/plain")})
                self.assertEqual(response.status_code, 200)
                final = email.call_args.kwargs["analysis"]
                self.assertEqual(final.decision.verdict, verdict)
                self.assertIn("What Does the Refrigerant Evidence Show?", response.text)


if __name__ == "__main__":
    unittest.main()
