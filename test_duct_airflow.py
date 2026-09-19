"""Offline Phase 2E tests, including the actual upload/finalization/render path."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from duct_airflow import (DUCT_SUBJECT, DUCT_AIRFLOW_RULES, duct_required, duct_text,
                         duct_items, normalize_duct_assessments)
from test_system_sizing import analysis, sizing, domain_items


GOOD = ["Total external static pressure measured at 0.62 in. w.c., within the submitted manufacturer limit of 0.80 in. w.c. for this configuration.",
        "Delivered airflow measured at 1,100 CFM against the submitted target of 1,100 CFM."]
BAD = "Total external static pressure measured at 0.92 in. w.c. exceeds the submitted limit of 0.65 in. w.c. for this configuration, with no correction included."
CORRECTION = "The return duct is crushed. Proposed scope includes replacing the crushed return section and enlarging the restrictive connection according to the submitted correction design."


def assessment(status="ADEQUATE", scope="APPROPRIATE", evidence=None, gaps=None, conflicts=None, subject=DUCT_SUBJECT):
    return main.TechnicalEvidenceAssessment(subject=subject, materiality="PRIMARY",
        diagnostic_evidence_status=status, scope_support=scope,
        documented_evidence=GOOD if evidence is None else evidence,
        material_gaps=gaps or [], contradictions=conflicts or [])


def source(name):
    return Path(f"duct_airflow_{name}_test.txt").read_text()


def raw_case(name="good"):
    duct = {
        "good": assessment(),
        "partial": assessment("ABSENT", "UNSUPPORTED", evidence=[], gaps=["No airflow verification documented."]),
        "bad": assessment("CONTRADICTORY", "UNSUPPORTED", evidence=[BAD], conflicts=[BAD]),
        "capacity_increase": assessment("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[], gaps=["Existing ducts not checked for larger equipment."]),
        "return_restriction": assessment(evidence=[CORRECTION]),
    }[name]
    cooling_load, cooling_output = ("46,000", "47,000") if name == "capacity_increase" else ("34,000", "35,000")
    return analysis(*domain_items(), sizing(evidence=[
        f"Sample Home E cooling load: {cooling_load} Btu/h; heating load: 58,000 Btu/h.",
        f"Proposed cooling capacity: {cooling_output} Btu/h at the submitted design conditions; furnace output: 60,000 Btu/h. The selection supports these building loads.",
    ]), duct)


class DuctAirflowTests(unittest.TestCase):
    def setUp(self):
        # Isolate accepted duct calibration from newly material startup scope.
        # Real whole-report legacy-fixture behavior is covered by Phase 2F.
        startup_patcher = patch("commissioning.commissioning_required", return_value=False)
        startup_patcher.start()
        self.addCleanup(startup_patcher.stop)

    def final(self, name):
        return main.finalize_customer_analysis(raw_case(name), source(name), 1)

    def test_registry_and_prompt_ownership(self):
        self.assertEqual(main.ANALYSIS_MODULES[main.AnalysisModule.DUCT_AIRFLOW], DUCT_AIRFLOW_RULES)
        self.assertEqual(set(main.ANALYSIS_MODULES), set(main.AnalysisModule))
        for forbidden in ("LOW SUCTION PRESSURE ALONE", "motor winding", "capacitor testing", "superheat"):
            self.assertNotIn(forbidden, DUCT_AIRFLOW_RULES)

    def test_positive_routing(self):
        for text in (source("good"), "Reuse existing ducts for new air handler", "Replace all supply and return ductwork",
                     "Ducted mini-split installation", "Total external static pressure measured at 0.7",
                     "Capacity increase with existing ducts", "Zoning airflow issue"):
            with self.subTest(text=text):
                self.assertTrue(duct_required(text))

    def test_negative_routing(self):
        for text in ("Single zone ductless mini-split", "Multi-zone ductless heads", "Replace capacitor only",
                     "Replace igniter", "Replace contactor", "Condenser fan airflow repair",
                     "Refrigerant recharge", "Replace blower motor; configure blower speed"):
            with self.subTest(text=text):
                self.assertFalse(duct_required(text))

    def test_semantic_subjects(self):
        for subject in (DUCT_SUBJECT, "Existing duct support", "Airflow support for new equipment",
                        "Duct capacity for proposed system", "Existing ductwork compatibility",
                        "Static-pressure support", "Air distribution for replacement equipment", "Supply and return support"):
            self.assertTrue(duct_text(subject), subject)
        for subject in ("Equipment matching", "System sizing", "Condenser airflow", "Blower motor diagnosis"):
            self.assertFalse(duct_text(subject), subject)

    def test_good(self):
        final = self.final("good")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertIn("0.80", main.build_report_html(final, 1))

    def test_partial(self):
        final = self.final("partial")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(duct_items(final)[0].diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("does not establish", main.build_report_html(final, 1))

    def test_bad_one_flag_question(self):
        final = self.final("bad")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("0.92", final.red_flags[0])
        self.assertIn("0.65", final.red_flags[0])

    def test_capacity_increase(self):
        final = self.final("capacity_increase")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.red_flags, [])
        self.assertIn("larger system", final.contractor_questions[0])
        self.assertNotIn("400", main.build_report_html(final, 1))

    def test_return_correction(self):
        raw = raw_case("return_restriction")
        raw.red_flags = ["The return duct is restricted.", "High static pressure."]
        final = main.finalize_customer_analysis(raw, source("return_restriction"), 1)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertIn("return", " ".join(final.good_signs))

    def test_missing_assessment_raw_unchanged(self):
        raw = analysis(*domain_items(), sizing())
        before = raw.model_dump()
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(duct_items(final)[0].diagnostic_evidence_status, "ADEQUATE")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_measured_source_recovery_for_weakened_ai(self):
        raw = raw_case("partial")
        before = raw.model_dump()
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertEqual(raw.model_dump(), before)
        item = duct_items(final)[0]
        self.assertEqual((item.diagnostic_evidence_status, item.scope_support), ("ADEQUATE", "APPROPRIATE"))
        self.assertFalse(item.material_gaps)
        self.assertFalse(final.contractor_questions)

    def test_measured_source_recovery_ambiguous_stays_partial(self):
        variants = [
            source("good").replace("for proposed equipment and cooling configuration", "for old equipment"),
            source("good").replace("submitted manufacturer limit 0.80 in. w.c.", "limit not supplied"),
            source("good").replace("Delivered airflow measured at 1,100 CFM", "Airflow estimated at 1,100 CFM"),
            source("good").replace("submitted target 1,100 CFM", "target not supplied"),
            source("good").replace("for this configuration", "for a different configuration"),
            source("good").replace("Delivered airflow measured at 1,100 CFM", "Delivered airflow measured at 31 cubic meters/minute"),
            source("good") + "\nDelivered airflow measured at 900 CFM against submitted target 1,100 CFM.\n",
            source("good") + "\nUnresolved return duct restriction.\n",
            source("good").replace("Total external static pressure measured", "No total external static pressure measured"),
            "QUOTE 1\n" + source("good") + "\nQUOTE 2\n" + source("good"),
        ]
        for text in variants:
            with self.subTest(text=text[-180:]):
                raw = analysis(*domain_items(), sizing())
                final = main.finalize_customer_analysis(raw, text, 1)
                self.assertEqual(duct_items(final)[0].scope_support, "PARTIALLY_DEFINED")

    def test_omitted_bad_source_remains_unsupported(self):
        raw = analysis(*domain_items(), sizing())
        final = main.finalize_customer_analysis(raw, source("bad"), 1)
        self.assertEqual(duct_items(final)[0].scope_support, "UNSUPPORTED")
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_measured_source_does_not_override_structured_contradiction(self):
        raw = raw_case("bad")
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertEqual(duct_items(final)[0].scope_support, "UNSUPPORTED")
        self.assertTrue(duct_items(final)[0].contradictions)

    def test_measured_source_preserves_independent_material_gap(self):
        raw = raw_case("partial")
        duct_items(raw)[0].material_gaps = ["Room airflow balance is not documented."]
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertEqual(duct_items(final)[0].scope_support, "PARTIALLY_DEFINED")
        self.assertIn("Room airflow balance is not documented.", duct_items(final)[0].material_gaps)

    def test_omitted_non_good_sources_keep_phase2e_boundaries(self):
        for name, scope in (("partial", "PARTIALLY_DEFINED"), ("capacity_increase", "PARTIALLY_DEFINED"),
                            ("return_restriction", "APPROPRIATE")):
            with self.subTest(name=name):
                final = main.finalize_customer_analysis(analysis(*domain_items(), sizing()), source(name), 1)
                self.assertEqual(duct_items(final)[0].scope_support, scope)

    def test_false_positive_support_firewalls(self):
        for evidence in ("Cooling temperature split is 20 F", "Furnace temperature rise is within manufacturer range",
                         "Matched equipment", "Building load supports selection", "Startup included",
                         "Blower motor operates correctly", "Same tonnage worked before", "Blower tap selected",
                         "Total external static measured 0.7 in. w.c.", "3 tons at 400 CFM/ton"):
            raw = analysis(assessment(evidence=[evidence]))
            normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
            self.assertEqual(duct_items(raw)[0].scope_support, "PARTIALLY_DEFINED", evidence)

    def test_missing_conflict_is_not_bad(self):
        raw = analysis(assessment("ABSENT", "UNSUPPORTED", evidence=[], conflicts=["No airflow measurements documented."]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].diagnostic_evidence_status, "INCOMPLETE")

    def test_physical_defect_without_static(self):
        raw = analysis(assessment(evidence=["Disconnected supply duct documented. Scope includes reconnecting and sealing the duct."]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].scope_support, "APPROPRIATE")

    def test_question_dedup_pricing(self):
        raw = raw_case("partial")
        raw.decision.pricing_transparency = "LIMITED"
        raw.contractor_questions = ["Did you verify existing ductwork?", "What measurements support the duct capacity?", "Can you itemize pricing?"]
        final = main.finalize_customer_analysis(raw, "Existing ducts reused", 1)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["duct_airflow", "pricing"])

    def test_independent_findings_preserved(self):
        raw = raw_case("partial")
        raw.red_flags = ["The drain is disconnected."]
        final = main.finalize_customer_analysis(raw, source("partial"), 1)
        self.assertIn("The drain is disconnected.", final.red_flags)
        self.assertTrue(any(a.subject == "Quoted indoor/outdoor equipment compatibility" for a in final.technical_assessments))

    def test_section_absent_minor_and_ductless(self):
        for text in ("Replace capacitor", "Single-zone ductless mini-split"):
            raw = analysis()
            final = main.finalize_customer_analysis(raw, text, 1)
            self.assertNotIn("Can the Ductwork", main.build_report_html(final, 1))

    def test_upload_path(self):
        from fastapi.testclient import TestClient
        for name in ("good", "partial", "bad", "capacity_increase", "return_restriction"):
            with self.subTest(name=name):
                raw = raw_case(name)
                raw.project_overview = "The proposal includes existing ductwork, ensuring perfect comfort."
                classification = main.QuoteClassification(quote_type="replacement", system_type="ducted HVAC", primary_scope="replacement", modules_required=[])
                def response(value):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
                with tempfile.TemporaryDirectory() as directory, patch.object(main, "UPLOAD_DIR", Path(directory)), \
                     patch.object(main.client.beta.chat.completions, "parse", side_effect=[response(classification), response(raw)]) as api, \
                     patch.object(main, "send_review_email") as email:
                    result = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"test@example.com"},
                        files=[("files", (f"duct_airflow_{name}_test.txt", source(name).encode(), "text/plain"))])
                self.assertEqual(result.status_code, 200)
                self.assertEqual(api.call_count, 2)
                self.assertIn(main.AnalysisModule.DUCT_AIRFLOW, classification.modules_required)
                final = email.call_args.kwargs["analysis"]
                self.assertEqual(result.text, main.build_report_html(final, 1))
                self.assertIn("Can the Ductwork Support the Proposed System?", result.text)
                self.assertTrue(final.project_overview.strip())
                self.assertNotIn("perfect comfort", final.project_overview)
                self.assertIn(main.esc(final.project_overview), result.text)

    def test_numeric_static_conflict_overrides_inaccurate_within_word(self):
        raw = analysis(assessment(evidence=[GOOD[0].replace("0.62", "0.92")]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].scope_support, "UNSUPPORTED")

    def test_two_configurations_are_not_combined(self):
        from duct_airflow import static_comparison
        self.assertIsNone(static_comparison([GOOD[0], GOOD[0].replace("0.62", "0.40").replace("0.80", "0.50")]))

    def test_missing_information_with_restriction_word_is_not_contradiction(self):
        raw = analysis(assessment("ABSENT", "UNSUPPORTED", evidence=[], conflicts=["Duct restriction inspection is not documented."]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].diagnostic_evidence_status, "INCOMPLETE")

    def test_generic_duct_replacement_is_partial(self):
        raw = analysis(assessment("ABSENT", "UNSUPPORTED", evidence=["Some rooms have weak airflow; replace all ducts."]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].scope_support, "PARTIALLY_DEFINED")

    def test_unrelated_equipment_replacement_does_not_correct_return(self):
        raw = analysis(assessment(evidence=["Return duct restriction documented. Proposed scope replaces the condenser."]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertNotEqual(duct_items(raw)[0].scope_support, "APPROPRIATE")

    def test_correction_design_contradiction_survives(self):
        raw = analysis(assessment(evidence=[CORRECTION], conflicts=["The correction design conflicts with the submitted minimum airflow requirement."]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].scope_support, "UNSUPPORTED")

    def test_scoped_correction_removes_historical_restriction_flag(self):
        raw = analysis(assessment(evidence=[CORRECTION], conflicts=["The return duct is crushed."]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].scope_support, "APPROPRIATE")

    def test_delivered_airflow_against_submitted_target(self):
        raw = analysis(assessment(evidence=[GOOD[1]]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].scope_support, "APPROPRIATE")

    def test_submitted_airflow_conflict(self):
        raw = analysis(assessment("CONTRADICTORY", "UNSUPPORTED", evidence=[],
            conflicts=["Measured airflow is 800 CFM, materially below the submitted requirement of 1,200 CFM. No correction is included."]))
        final = main.finalize_customer_analysis(raw, "", 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(len(final.red_flags), 1)

    def test_material_duct_question_survives_incomplete_matching(self):
        raw = raw_case("partial")
        matching = raw.technical_assessments[1]
        matching.diagnostic_evidence_status = "INCOMPLETE"
        matching.scope_support = "PARTIALLY_DEFINED"
        matching.material_gaps = ["Exact indoor model is missing."]
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.contractor_questions = ["Can the existing ductwork support the new heat pump?"]
        final = main.finalize_customer_analysis(raw, source("partial"), 1)
        purposes = [main.contractor_question_category(q) for q in final.contractor_questions]
        self.assertEqual(purposes.count("duct_airflow"), 1)
        self.assertTrue(any(p.startswith("equipment_") for p in purposes))

    def test_sizing_conflict_not_rescued_by_good_ducts(self):
        raw = raw_case("good")
        raw.technical_assessments[2].contradictions = ["The submitted selection conflicts with the quoted cooling capacity."]
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(duct_items(final)[0].scope_support, "APPROPRIATE")

    def test_duct_good_cannot_rescue_replacement_basis(self):
        raw = raw_case("good")
        raw.technical_assessments[0].diagnostic_evidence_status = "CONTRADICTORY"
        raw.technical_assessments[0].scope_support = "UNSUPPORTED"
        raw.technical_assessments[0].contradictions = ["Replacement claim conflicts with documented repair findings."]
        final = main.finalize_customer_analysis(raw, "", 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_ductless_false_assessment_removed(self):
        raw = analysis(assessment("ABSENT", "UNSUPPORTED", evidence=[]))
        before = raw.model_dump()
        final = main.finalize_customer_analysis(raw, "True ductless single-zone heads; no ducts", 1)
        self.assertEqual(duct_items(final), [])
        self.assertEqual(raw.model_dump(), before)

    def test_legacy_fixtures_preserved_and_not_treated_as_uniform_bad(self):
        for name in ("airflow_static_good_test.txt", "airflow_static_bad_test.txt",
                     "ductwork_distribution_good_test.txt", "ductwork_distribution_bad_test.txt",
                     "repair_replace_sizing_good_test.txt", "repair_replace_sizing_bad_test.txt"):
            self.assertTrue(Path(name).read_text().strip())
        # Legacy symptoms do not establish a distribution defect.
        raw = analysis(assessment("ABSENT", "UNSUPPORTED", evidence=["Weak room airflow and uneven temperatures."]))
        final = main.finalize_customer_analysis(raw, Path("ductwork_distribution_bad_test.txt").read_text(), 1)
        self.assertEqual(duct_items(final)[0].scope_support, "PARTIALLY_DEFINED")
        self.assertFalse(any(duct_text(flag) for flag in final.red_flags))

    def test_prompt_firewalls(self):
        for phrase in ("0.50 in. w.c. is NOT universal", "No universal 400 CFM/ton", "Cooling delta-T",
                       "Furnace temperature rise", "bypass", "accessory pressure drop", "blower tap/profile"):
            self.assertIn(phrase, DUCT_AIRFLOW_RULES)

    def test_customer_promises_removed_without_losing_measurements(self):
        raw = raw_case("good")
        raw.technical_assessments[-1].documented_evidence[0] += " This ensures perfect comfort."
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        report = main.build_report_html(final, 1)
        self.assertNotIn("ensures perfect comfort", report)
        self.assertIn("0.62", report)

    def test_noncanonical_subject_normalized_on_copy(self):
        raw = analysis(assessment(subject="Existing ductwork compatibility"))
        final = main.finalize_customer_analysis(raw, "", 1)
        self.assertEqual(duct_items(final)[0].subject, DUCT_SUBJECT)
        self.assertEqual(raw.technical_assessments[0].subject, "Existing ductwork compatibility")

    def test_multiple_system_assessments_keep_identity(self):
        raw = analysis(assessment(subject="Duct support - system A"), assessment("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[], subject="Duct support - system B"))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual([a.subject for a in duct_items(raw)], ["Duct support - system A", "Duct support - system B"])

    def test_duct_pricing_stays_pricing(self):
        self.assertEqual(main.contractor_question_category("Can you itemize the ductwork pricing?"), "pricing")

    def test_minor_repair_absence_checklist_not_routing(self):
        self.assertFalse(duct_required("Replace capacitor only. No duct review is documented."))

    def test_return_correction_does_not_clear_independent_supply_defect(self):
        raw = analysis(assessment(evidence=[CORRECTION], conflicts=["The supply duct is disconnected and no repair is proposed."]))
        normalize_duct_assessments(raw, "", main.TechnicalEvidenceAssessment)
        self.assertEqual(duct_items(raw)[0].scope_support, "UNSUPPORTED")

    def test_legacy_corrected_return_and_duct_replacement(self):
        for filename in ("airflow_static_good_test.txt", "ductwork_distribution_good_test.txt"):
            quote = Path(filename).read_text()
            # Parsed evidence reflects the documented finding and actual scoped work,
            # not a synthetic universal static/CFM threshold.
            evidence = (["Return restriction identified. Proposed scope modifies return ductwork and installs larger return grille; after correcting the restriction, measured static is 0.46 within the manufacturer limit of 0.50 in. w.c."]
                        if filename.startswith("airflow") else
                        ["Crushed flex duct and documented leakage. Proposed scope replaces existing damaged ductwork, seals connections and supports flex duct; verify static and airflow after repairs."])
            raw = analysis(assessment(evidence=evidence))
            final = main.finalize_customer_analysis(raw, quote, 1)
            self.assertEqual(duct_items(final)[0].scope_support, "APPROPRIATE")

    def test_legacy_replacement_preserves_upstream_failure(self):
        raw = analysis(
            sizing("ABSENT", "UNSUPPORTED", subject="Basis for full-system replacement", evidence=[], gaps=["No meaningful failure evidence."]),
            assessment("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]))
        final = main.finalize_customer_analysis(raw, Path("repair_replace_sizing_bad_test.txt").read_text(), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(duct_items(final)[0].scope_support, "PARTIALLY_DEFINED")

    def test_good_fixture_meets_existing_sizing_and_pricing_boundaries(self):
        from system_sizing import clear_submitted_sizing_support, submitted_sizing_excerpt
        text = source("good")
        self.assertTrue(clear_submitted_sizing_support(submitted_sizing_excerpt(text)))
        self.assertEqual(main.replacement_category_price_breakdown(raw_case("good"), text),
                         ("$18,000", "$11,000", "$7,000"))
        self.assertIsNone(main.replacement_category_price_breakdown(
            raw_case("good"), text.replace("Installation scope:", "Work included:")))
        self.assertFalse(clear_submitted_sizing_support(submitted_sizing_excerpt(
            text.replace("cooling output:", "output:"))))

    def test_good_replacement_presentation_prioritizes_technical_findings(self):
        raw = raw_case("good")
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.good_signs = ["The planned upgrade is a good sign.",
                          "The warranty provides homeowner security.",
                          "A comprehensive proposal ensures comfort."]
        raw.installation_concerns = (
            "It's important to ensure that startup procedures verify effective operation. "
            "No specific concerns regarding installation practices were noted.")
        final = main.finalize_customer_analysis(raw, source("good"), 1)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertIn("load results", final.homeowner_takeaway)
        self.assertIn("airflow measurements", final.homeowner_takeaway)
        self.assertIn("duct measurements", final.bottom_line)
        self.assertIn("equipment match", final.bottom_line)
        self.assertIn("sizing or duct-support", final.missing_information)
        self.assertIn("building-specific load", final.good_signs[0])
        self.assertIn("matched combination", final.good_signs[1])
        self.assertIn("0.62", final.good_signs[2])
        self.assertIn("0.80", final.good_signs[2])
        self.assertEqual(final.good_signs[3].count("1,100"), 2)
        self.assertIn("10-year parts warranty", final.good_signs[4])
        self.assertIn("removal of the existing equipment", final.installation_concerns)
        self.assertIn("installation of the new system", final.installation_concerns)
        self.assertIn("startup verification", final.installation_concerns)
        prose = " ".join([*final.good_signs, final.installation_concerns,
                          final.homeowner_takeaway, final.bottom_line]).lower()
        for phrase in ("homeowner security", "planned upgrade", "comprehensive", "ensur", "guarantee", "no specific concerns"):
            self.assertNotIn(phrase, prose)

    def test_good_presentation_does_not_modify_decision_or_evidence(self):
        from duct_airflow import present_supported_duct_replacement
        final = self.final("good")
        before = final.model_dump()
        present_supported_duct_replacement(final, source("good"), main.sizing_assessments(final),
                                          main.primary_equipment_matching_assessment(final))
        for field in ("decision", "technical_assessments", "contractor_questions", "replacement_context", "market_price_context"):
            self.assertEqual(final.model_dump()[field], before[field])

    def test_good_presentation_not_applied_to_other_duct_cases(self):
        from duct_airflow import present_supported_duct_replacement
        for case in ("partial", "bad", "capacity_increase", "return_restriction"):
            final = self.final(case)
            before = final.model_dump()
            present_supported_duct_replacement(final, source(case), main.sizing_assessments(final),
                                              main.primary_equipment_matching_assessment(final))
            self.assertEqual(final.model_dump(), before)

    def test_good_whole_report_restores_omitted_elective_basis_and_sizing(self):
        """No sizing/duct completeness bypass: only external AI/email are mocked."""
        from fastapi.testclient import TestClient
        raw = raw_case("good")
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.technical_assessments = [raw.technical_assessments[1], raw.technical_assessments[3]]
        raw.decision.technical_support = "PARTIALLY_SUPPORTED"
        raw.decision.pricing_transparency = "LIMITED"
        raw.decision.required_actions = [main.PRICING_REQUIRED_ACTION]
        raw.contractor_questions = [
            "What is wrong with the existing system?",
            "Can you provide the building-specific load calculations?",
            "Can you provide an itemized price breakdown?",
            "Did you check the existing ducts?",
        ]
        raw.good_signs = [
            "The proposed system is supported by building-specific load data, ensuring the equipment is adequate for the calculated loads.",
            "The submitted manufacturer sheet documents the exact equipment combination.",
        ]
        before = raw.model_dump()
        classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
            primary_scope="Planned elective upgrade", modules_required=[main.AnalysisModule.REPAIR_VS_REPLACE,
            main.AnalysisModule.EQUIPMENT_MATCHING])
        def response(value):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "UPLOAD_DIR", Path(directory)), \
             patch.object(main.client.beta.chat.completions, "parse", side_effect=[response(classification), response(raw)]), \
             patch.object(main, "send_review_email") as email:
            result = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline acceptance", "customer_email":"test@example.com"},
                files=[("files", ("duct_airflow_good_test.txt", source("good").encode(), "text/plain"))])
        self.assertEqual(result.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(final.replacement_context, main.ReplacementContext.ELECTIVE)
        for item in (main.replacement_basis_assessment(final), main.primary_equipment_matching_assessment(final),
                     main.sizing_assessments(final)[0], duct_items(final)[0]):
            self.assertIsNotNone(item)
            self.assertIn(item.diagnostic_evidence_status, {"ADEQUATE", "CONFIRMED"})
            self.assertEqual(item.scope_support, "APPROPRIATE")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertEqual(final.decision.required_actions, [])
        self.assertEqual(result.text, main.build_report_html(final, 1))
        duct_section = result.text.split("Can the Ductwork Support the Proposed System?</h2>")[1].split("</div>")[0]
        for number in ("0.62", "0.80"):
            self.assertIn(number, duct_section)
        self.assertGreaterEqual(duct_section.count("1,100"), 2)
        self.assertNotIn("ensuring", " ".join(final.good_signs))
        self.assertIn("The proposed size is tied to building-specific load results.", final.good_signs)

    def test_partial_whole_report_isolates_duct_gap(self):
        from fastapi.testclient import TestClient
        raw = raw_case("partial")
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.technical_assessments = [raw.technical_assessments[1], raw.technical_assessments[3]]
        raw.decision.pricing_transparency = "LIMITED"
        raw.decision.required_actions = [main.PRICING_REQUIRED_ACTION]
        raw.pricing_review = "Finer itemization would enhance transparency."
        raw.equipment_analysis = (
            "The components are compatible with the building's specific load requirements, "
            "and the planned installation appears technically sound.")
        raw.contractor_questions = [
            "Can you provide load calculations?", "Did you check the existing ducts?",
            "Can you clarify the warranty details for the equipment and labor?",
            "Can you clarify how you will verify heating and cooling operation after installation?",
            "Can you itemize the pricing?", "Why replace the system instead of repairing it?",
        ]
        before = raw.model_dump()
        classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
            primary_scope="Elective replacement", modules_required=[main.AnalysisModule.REPAIR_VS_REPLACE,
            main.AnalysisModule.EQUIPMENT_MATCHING])
        def response(value):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "UPLOAD_DIR", Path(directory)), \
             patch.object(main.client.beta.chat.completions, "parse", side_effect=[response(classification), response(raw)]), \
             patch.object(main, "send_review_email") as email:
            result = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Partial offline", "customer_email":"test@example.com"},
                files=[("files", ("duct_airflow_partial_test.txt", source("partial").encode(), "text/plain"))])
        self.assertEqual(result.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(raw.model_dump(), before)
        for item in (main.replacement_basis_assessment(final), main.primary_equipment_matching_assessment(final), main.sizing_assessments(final)[0]):
            self.assertIn(item.diagnostic_evidence_status, {"ADEQUATE", "CONFIRMED"})
            self.assertEqual(item.scope_support, "APPROPRIATE")
        duct = duct_items(final)[0]
        self.assertEqual((duct.diagnostic_evidence_status, duct.scope_support), ("INCOMPLETE", "PARTIALLY_DEFINED"))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(final.red_flags, [])
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["duct_airflow"])
        self.assertEqual(result.text, main.build_report_html(final, 1))
        self.assertIn("This does not establish that the ducts are inadequate", result.text)
        self.assertIn("existing ducts", final.homeowner_takeaway)
        self.assertIn("airflow or static-pressure review", final.bottom_line)
        self.assertNotIn("sound", final.equipment_analysis)
        self.assertNotIn("load requirements", final.equipment_analysis)
        self.assertNotIn("finer itemization", final.pricing_review)
        self.assertNotIn("sizing", final.missing_information)
        self.assertIn("$11,000", final.pricing_review)
        self.assertIn("The one thing", final.homeowner_takeaway)
        self.assertIn("duct-design information", final.missing_information)
        self.assertNotIn("homeowner-requested", " ".join(final.good_signs))
        self.assertNotIn("planned", " ".join(final.good_signs))
        self.assertTrue(any("10-year parts warranty" in s and "2-year labor warranty" in s
                            for s in final.good_signs))

    def test_partial_duct_preserves_material_warranty_gap(self):
        raw = raw_case("partial")
        raw.technical_assessments.append(main.TechnicalEvidenceAssessment(
            subject="Warranty coverage", materiality="MATERIAL_SECONDARY",
            diagnostic_evidence_status="INCOMPLETE", scope_support="PARTIALLY_DEFINED",
            material_gaps=["Warranty coverage for the quoted replacement conflicts with the listed exclusion."]))
        raw.contractor_questions = ["Does the warranty exclusion apply to these parts?"]
        final = main.finalize_customer_analysis(raw, source("partial"), 1)
        self.assertIn("warranty", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_partial_duct_preserves_explicit_warranty_action(self):
        raw = raw_case("partial")
        raw.decision.required_actions = ["Clarify the material warranty exclusion before approval."]
        raw.contractor_questions = ["Does the warranty exclusion apply to these parts?"]
        final = main.finalize_customer_analysis(raw, source("partial"), 1)
        self.assertIn("warranty", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_partial_duct_preserves_independent_commissioning_question(self):
        raw = raw_case("partial")
        raw.technical_assessments.append(main.TechnicalEvidenceAssessment(
            subject="Commissioning and final operation", materiality="MATERIAL_SECONDARY",
            diagnostic_evidence_status="INCOMPLETE", scope_support="PARTIALLY_DEFINED",
            material_gaps=["Required final safety verification is not defined."]))
        raw.contractor_questions = ["How will you verify heating and cooling operation after installation?"]
        final = main.finalize_customer_analysis(raw, source("partial"), 1)
        self.assertIn("commissioning", [main.contractor_question_category(q) for q in final.contractor_questions])
        self.assertIn("duct_airflow", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_bad_whole_report_has_only_measured_duct_conflict(self):
        from fastapi.testclient import TestClient
        raw = raw_case("bad")
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.technical_assessments = [raw.technical_assessments[1], raw.technical_assessments[3]]
        raw.decision.pricing_transparency = "LIMITED"
        raw.decision.required_actions = [main.PRICING_REQUIRED_ACTION]
        raw.decision.verdict_reasons = ["Pricing transparency is limited because potential duct correction costs are unknown."]
        raw.pricing_review = "Pricing is limited because the duct correction cost is unknown."
        raw.equipment_analysis = "The matched equipment is appropriate for the specified loads."
        raw.good_signs = ["The matched equipment is appropriate for the specified loads."]
        raw.installation_concerns = "Without resolving this issue, performance might not meet expected standards."
        raw.contractor_questions = ["Can you show load calculations?", "What will correct the static pressure?",
            "How will you verify heating and cooling operation after installation?", "Can you itemize the price?"]
        before = raw.model_dump()
        classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
            primary_scope="Elective replacement", modules_required=[main.AnalysisModule.REPAIR_VS_REPLACE,
            main.AnalysisModule.EQUIPMENT_MATCHING])
        def response(value):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "UPLOAD_DIR", Path(directory)), \
             patch.object(main.client.beta.chat.completions, "parse", side_effect=[response(classification), response(raw)]), \
             patch.object(main, "send_review_email") as email:
            result = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Bad offline", "customer_email":"test@example.com"},
                files=[("files", ("duct_airflow_bad_test.txt", source("bad").encode(), "text/plain"))])
        self.assertEqual(result.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(raw.model_dump(), before)
        for item in (main.replacement_basis_assessment(final), main.primary_equipment_matching_assessment(final), main.sizing_assessments(final)[0]):
            self.assertIn(item.diagnostic_evidence_status, {"ADEQUATE", "CONFIRMED"})
            self.assertEqual(item.scope_support, "APPROPRIATE")
        duct = duct_items(final)[0]
        self.assertEqual((duct.diagnostic_evidence_status, duct.scope_support), ("CONTRADICTORY", "UNSUPPORTED"))
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["duct_airflow"])
        self.assertEqual(result.text, main.build_report_html(final, 1))
        for field in (final.bottom_line, final.missing_information, final.installation_concerns, final.red_flags[0]):
            self.assertIn("0.92", field)
            self.assertIn("0.65", field)
        for phrase in ("appropriate for the specified loads", "expected standards", "main diagnosis", "sizing needs clarification"):
            self.assertNotIn(phrase, result.text)
        self.assertIn("$11,000", final.pricing_review)

    def test_bad_presentation_values_are_dynamic(self):
        raw = raw_case("bad")
        item = raw.technical_assessments[-1]
        item.documented_evidence = [BAD.replace("0.92", "1.12").replace("0.65", "0.90")]
        item.contradictions = item.documented_evidence[:]
        final = main.finalize_customer_analysis(raw, source("bad").replace("0.92", "1.12").replace("0.65", "0.90"), 1)
        self.assertIn("1.12", final.bottom_line)
        self.assertIn("0.9", final.bottom_line)
        self.assertNotIn("0.92", final.bottom_line)
        self.assertEqual(len(final.red_flags), 1)

    def test_bad_voice_and_independent_positive_facts(self):
        raw = raw_case("bad")
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.good_signs = [
            "The proposal correctly uses an approved matched combination of equipment, maximizing compatibility and efficiency.",
            "The proposal clearly identifies the replacement as a planned, homeowner-requested upgrade.",
            "The proposal includes a 10-year parts warranty and a 2-year labor warranty.",
        ]
        final = main.finalize_customer_analysis(raw, source("bad"), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["duct_airflow"])
        self.assertIn("above the limit listed for this equipment", final.recommendation)
        self.assertIn("doesn't show how it will be corrected", final.recommendation)
        self.assertNotIn("air-distribution system as proposed", main.build_report_html(final, 1))
        signs = " ".join(final.good_signs).lower()
        for phrase in ("maximiz", "efficiency", "planned", "homeowner-requested", "upgrade"):
            self.assertNotIn(phrase, signs)
        self.assertIn("building-specific load results", signs)
        self.assertIn("approved matched combination", signs)
        self.assertIn("10-year parts warranty", signs)
        self.assertIn("0.92", final.bottom_line)
        self.assertIn("0.65", final.bottom_line)

    def test_bad_preserves_independent_commissioning_gap(self):
        raw = raw_case("bad")
        raw.technical_assessments.append(main.TechnicalEvidenceAssessment(
            subject="Commissioning safety verification", materiality="MATERIAL_SECONDARY",
            diagnostic_evidence_status="INCOMPLETE", scope_support="PARTIALLY_DEFINED",
            material_gaps=["Safety verification after installation is undefined."]))
        raw.contractor_questions = ["How will you verify safety after installation?"]
        final = main.finalize_customer_analysis(raw, source("bad"), 1)
        self.assertIn("commissioning", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_capacity_increase_whole_report_isolates_larger_system_duct_gap(self):
        from fastapi.testclient import TestClient
        raw = raw_case("capacity_increase")
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.technical_assessments = [raw.technical_assessments[1], raw.technical_assessments[3]]
        raw.decision.pricing_transparency = "LIMITED"
        raw.decision.required_actions = [main.PRICING_REQUIRED_ACTION]
        raw.pricing_review = "The quote lacks detailed itemization."
        raw.good_signs = ["The warranty provides assurance of quality."]
        raw.installation_concerns = "Review the documented installation scope with the contractor before approval."
        raw.contractor_questions = ["Can you provide load calculations?", "Did you check the existing ducts?",
            "What specific measures will be taken to verify proper airflow after installation?",
            "Can you itemize the price?", "Can you clarify warranty coverage?", "Why replace instead of repair?"]
        before = raw.model_dump()
        classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
            primary_scope="Elective replacement", modules_required=[main.AnalysisModule.REPAIR_VS_REPLACE,
            main.AnalysisModule.EQUIPMENT_MATCHING])
        def response(value):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "UPLOAD_DIR", Path(directory)), \
             patch.object(main.client.beta.chat.completions, "parse", side_effect=[response(classification), response(raw)]), \
             patch.object(main, "send_review_email") as email:
            result = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Capacity offline", "customer_email":"test@example.com"},
                files=[("files", ("duct_airflow_capacity_increase_test.txt", source("capacity_increase").encode(), "text/plain"))])
        self.assertEqual(result.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(raw.model_dump(), before)
        for item in (main.replacement_basis_assessment(final), main.primary_equipment_matching_assessment(final), main.sizing_assessments(final)[0]):
            self.assertIn(item.diagnostic_evidence_status, {"ADEQUATE", "CONFIRMED"})
            self.assertEqual(item.scope_support, "APPROPRIATE")
        duct = duct_items(final)[0]
        self.assertEqual((duct.diagnostic_evidence_status, duct.scope_support), ("INCOMPLETE", "PARTIALLY_DEFINED"))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(final.red_flags, [])
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions], ["duct_airflow"])
        self.assertIn("4 tons", final.contractor_questions[0])
        self.assertEqual(result.text, main.build_report_html(final, 1))
        section = result.text.split("Can the Ductwork Support the Proposed System?</h2>")[1].split("</div>")[0]
        self.assertIn("from 3 tons to 4 tons", section)
        self.assertIn("does not establish that the ducts are inadequate", section)
        self.assertNotIn("CFM", section)
        self.assertNotIn("400", section)
        self.assertIn("4-ton", final.bottom_line)
        self.assertIn("ducts", final.bottom_line)
        self.assertNotIn("assurance", " ".join(final.good_signs))
        self.assertIn("10-year parts warranty", " ".join(final.good_signs))
        self.assertNotIn("scope with the contractor", final.installation_concerns)
        self.assertNotIn("sizing", final.missing_information)

    def test_capacity_source_pair_is_dynamic_and_not_a_cfm_rule(self):
        from duct_airflow import submitted_capacity_increase, capacity_duct_paragraph
        text = "Existing system 2.5 tons; proposed system 3.5 tons, existing ducts reused."
        self.assertEqual(submitted_capacity_increase(text), (2.5, 3.5))
        self.assertIn("from 2.5 tons to 3.5 tons", capacity_duct_paragraph(submitted_capacity_increase(text)))
        self.assertIsNone(submitted_capacity_increase("Existing system 4 tons; proposed system 3 tons; existing ducts reused."))
        self.assertIsNone(submitted_capacity_increase("Existing C36 and proposed C48 with existing ducts reused."))

    def test_filtered_summary_trace_and_factual_fallback(self):
        raw = raw_case("return_restriction")
        raw.project_overview = "The quote replaces HVAC equipment and modifies the return duct, ensuring perfect comfort."
        before = raw.model_dump()
        trace = {}
        original = main.finalize_duct_fields
        def capture(analysis, text):
            trace["before_duct_filter"] = analysis.project_overview
            original(analysis, text)
            trace["after_duct_filter"] = analysis.project_overview
        with patch.object(main, "finalize_duct_fields", side_effect=capture):
            final = main.finalize_customer_analysis(raw, source("return_restriction"), 1)
        self.assertEqual(trace["before_duct_filter"], raw.project_overview)
        self.assertEqual(trace["after_duct_filter"], "")
        self.assertEqual(raw.model_dump(), before)
        self.assertIn("complete HVAC system replacement", final.project_overview)
        self.assertIn("existing ductwork", final.project_overview)
        self.assertIn("return-duct modifications", final.project_overview)
        self.assertNotIn("ensuring", final.project_overview)
        self.assertNotIn("comfort", final.project_overview)
        report = main.build_report_html(final, 1)
        self.assertIn(main.esc(final.project_overview), report)

    def test_blank_summaries_for_all_duct_fixtures(self):
        for case in ("good", "partial", "bad", "capacity_increase", "return_restriction"):
            for empty in ("", " \n\t"):
                with self.subTest(case=case, empty=empty):
                    raw = raw_case(case)
                    raw.project_overview = empty
                    final = main.finalize_customer_analysis(raw, source(case), 1)
                    self.assertTrue(final.project_overview.strip())
                    self.assertIn("replacement", final.project_overview)
                    self.assertNotIn("HVAC equipment work", final.project_overview)
                    report = main.build_report_html(final, 1)
                    body = report.split("<h2>Review Summary</h2>")[1].split("</p>")[0].split("<p>")[1]
                    self.assertTrue(body.strip())
                    for prohibited in ("PROCEED", "SECOND OPINION", "red flag", "$", "market", "fair price", "failed", "guarantee"):
                        self.assertNotIn(prohibited, final.project_overview)

    def test_safe_summary_unchanged_and_empty_source_honest(self):
        raw = raw_case("return_restriction")
        safe = "The contractor proposes replacing the indoor and outdoor units."
        raw.project_overview = safe
        final = main.finalize_customer_analysis(raw, source("return_restriction"), 1)
        self.assertEqual(final.project_overview, safe)
        self.assertEqual(main.factual_review_summary(""),
                         "The submitted information does not identify the proposed equipment or scope of work.")

    def test_summary_fallback_excludes_denied_scope(self):
        summary = main.factual_review_summary("Replace capacitor. No compressor replacement included. Return duct modification is not included.")
        self.assertIn("capacitor", summary)
        self.assertNotIn("compressor", summary)
        self.assertNotIn("return", summary)

    def test_return_correction_whole_report_recovers_source_scope(self):
        from fastapi.testclient import TestClient
        raw = raw_case("bad")
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.technical_assessments = [raw.technical_assessments[1], raw.technical_assessments[3]]
        raw.technical_assessments[-1].material_gaps = ["No corrective return scope is documented."]
        raw.project_overview = "The proposed return duct work ensures performance."
        raw.decision.pricing_transparency = "LIMITED"
        raw.decision.required_actions = [main.PRICING_REQUIRED_ACTION, "Correct the high duct static pressure before approval."]
        raw.red_flags = [BAD]
        raw.contractor_questions = ["What will correct the duct restriction?", "Can you show sizing results?",
            "How will you verify performance after installation?", "Can you itemize the price?",
            "Is the metering device compatible?"]
        before = raw.model_dump()
        classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
            primary_scope="Elective replacement with corrective return work", modules_required=[main.AnalysisModule.REPAIR_VS_REPLACE,
            main.AnalysisModule.EQUIPMENT_MATCHING])
        def response(value):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "UPLOAD_DIR", Path(directory)), \
             patch.object(main.client.beta.chat.completions, "parse", side_effect=[response(classification), response(raw)]), \
             patch.object(main, "send_review_email") as email:
            result = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Return offline", "customer_email":"test@example.com"},
                files=[("files", ("duct_airflow_return_restriction_test.txt", source("return_restriction").encode(), "text/plain"))])
        self.assertEqual(result.status_code, 200)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(raw.model_dump(), before)
        for item in (main.replacement_basis_assessment(final), main.primary_equipment_matching_assessment(final),
                     main.sizing_assessments(final)[0], duct_items(final)[0]):
            self.assertIn(item.diagnostic_evidence_status, {"ADEQUATE", "CONFIRMED"})
            self.assertEqual(item.scope_support, "APPROPRIATE")
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertEqual(result.text, main.build_report_html(final, 1))
        section = result.text.split("Can the Ductwork Support the Proposed System?</h2>")[1].split("</div>")[0]
        for number in ("-0.58", "+0.34", "0.92", "0.65"):
            self.assertIn(number, section)
        self.assertIn("return side", section)
        self.assertIn("enlarging", section)
        self.assertIn("after the work", section)
        self.assertNotIn("No correction is documented", result.text)
        self.assertNotIn("enhance performance", result.text)
        self.assertIn("return-duct modifications", final.project_overview)

    def test_source_recovery_requires_meaningful_correction(self):
        from duct_airflow import submitted_return_correction
        text = source("return_restriction")
        self.assertIsNotNone(submitted_return_correction(text))
        self.assertIsNone(submitted_return_correction(source("bad")))
        self.assertIsNone(submitted_return_correction(text.replace("Scope includes", "Scope does not include")))
        self.assertIsNone(submitted_return_correction(text.replace("to the dimensions in the submitted correction design", "as needed")))

    def test_source_recovery_preserves_independent_conflicting_scope(self):
        raw = raw_case("bad")
        raw.technical_assessments[-1].contradictions.append("The supply duct is disconnected and no repair is proposed.")
        final = main.finalize_customer_analysis(raw, source("return_restriction"), 1)
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertTrue(any("supply duct" in c for c in duct_items(final)[0].contradictions))

    def test_return_generic_gaps_and_omitted_assessment_through_upload(self):
        from fastapi.testclient import TestClient
        from duct_airflow import submitted_return_correction
        for gaps in (["The quote does not show how the ducts support the proposed airflow."],
                     ["Final static pressure and airflow results are not yet documented."], None):
            with self.subTest(gaps=gaps):
                raw = raw_case("return_restriction")
                raw.replacement_context = main.ReplacementContext.ELECTIVE
                raw.technical_assessments = [raw.technical_assessments[1]]
                if gaps is not None:
                    raw.technical_assessments.append(assessment("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[], gaps=gaps))
                raw.decision.pricing_transparency = "LIMITED"
                raw.decision.required_actions = [main.PRICING_REQUIRED_ACTION, "Verify duct airflow before approval."]
                raw.red_flags = []
                raw.contractor_questions = ["What airflow measurements show that the ducts can support the proposed equipment?",
                                            "How will you verify operation after the work?"]
                before = raw.model_dump()
                trace = {}
                original = main.normalize_duct_assessments
                def capture(a, text, assessment_type, classification):
                    trace["before"] = a.model_dump()
                    trace["source"] = submitted_return_correction(text)
                    original(a, text, assessment_type, classification)
                    trace["after"] = [i.model_dump() for i in duct_items(a)]
                classification = main.QuoteClassification(quote_type="replacement", system_type="ducted AC and furnace",
                    primary_scope="Elective replacement", modules_required=[main.AnalysisModule.REPAIR_VS_REPLACE,
                    main.AnalysisModule.EQUIPMENT_MATCHING])
                def response(value):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
                with tempfile.TemporaryDirectory() as directory, patch.object(main, "UPLOAD_DIR", Path(directory)), \
                     patch.object(main.client.beta.chat.completions, "parse", side_effect=[response(classification), response(raw)]), \
                     patch.object(main, "normalize_duct_assessments", side_effect=capture), \
                     patch.object(main, "send_review_email") as email:
                    result = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Trace offline", "customer_email":"test@example.com"},
                        files=[("files", ("duct_airflow_return_restriction_test.txt", source("return_restriction").encode(), "text/plain"))])
                self.assertEqual(result.status_code, 200)
                final = email.call_args.kwargs["analysis"]
                self.assertEqual(raw.model_dump(), before)
                self.assertEqual(trace["source"][1], (-0.58, 0.34, 0.92, 0.65))
                self.assertEqual(trace["after"][0]["diagnostic_evidence_status"], "ADEQUATE")
                self.assertEqual(trace["after"][0]["scope_support"], "APPROPRIATE")
                self.assertTrue(any("-0.58" in e for e in trace["after"][0]["documented_evidence"]))
                self.assertEqual(final.decision.technical_support, "SUPPORTED")
                self.assertEqual(final.decision.verdict, "PROCEED")
                self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
                self.assertEqual(final.red_flags, [])
                self.assertEqual(final.contractor_questions, [])
                self.assertTrue(final.project_overview.strip())
                self.assertEqual(result.text, main.build_report_html(final, 1))
                section = result.text.split("Can the Ductwork Support the Proposed System?</h2>")[1].split("</div>")[0]
                for value in ("-0.58", "+0.34", "0.92", "0.65", "return side", "after the work"):
                    self.assertIn(value, section)
                for value in ("ducts are inadequate", "ask for airflow", "No correction", "guarantee"):
                    self.assertNotIn(value, section)

    def test_return_semantic_correction_variants(self):
        from duct_airflow import submitted_return_correction
        text = source("return_restriction")
        original = "replacing the crushed return section and enlarging the restrictive return connection"
        for wording in ("enlarging the return connection", "increasing the return duct size",
                        "replacing the restrictive return", "adding a return path",
                        "modifying the return transition", "correcting the return restriction"):
            with self.subTest(wording=wording):
                changed = text.replace("Distribution review:", "Air distribution findings:").replace(original, wording)
                self.assertIsNotNone(submitted_return_correction(changed))
                raw = analysis(assessment("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[], gaps=["Duct airflow support is not documented."]))
                normalize_duct_assessments(raw, changed, main.TechnicalEvidenceAssessment)
                self.assertEqual(duct_items(raw)[0].scope_support, "APPROPRIATE")

    def test_return_correction_does_not_clear_independent_material_gap(self):
        raw = analysis(assessment("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[],
            gaps=["The zoning minimum airflow requirement is not documented."]))
        final = main.finalize_customer_analysis(raw, source("return_restriction"), 1)
        self.assertEqual(duct_items(final)[0].scope_support, "PARTIALLY_DEFINED")
        self.assertIn("zoning", " ".join(duct_items(final)[0].material_gaps))
        self.assertTrue(any("-0.58" in e for e in duct_items(final)[0].documented_evidence))
        section = main.build_report_html(final, 1).split("Can the Ductwork Support the Proposed System?</h2>")[1].split("</div>")[0]
        self.assertIn("Remaining clarification", section)
        self.assertNotIn("No correction", section)

    def test_return_correction_cannot_use_unrelated_or_optional_work(self):
        from duct_airflow import submitted_return_correction
        text = source("return_restriction")
        original = "replacing the crushed return section and enlarging the restrictive return connection"
        self.assertIsNone(submitted_return_correction(text.replace(original, "replacing supply ducts")))
        self.assertIsNone(submitted_return_correction(text.replace("Scope includes", "Optional scope includes")))

    def test_return_presentation_keeps_correction_not_measurement_as_positive(self):
        raw = raw_case("return_restriction")
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.good_signs = ["Return static pressure: -0.58 in. w.c.",
                          "The replacement is a planned homeowner-requested upgrade."]
        raw.missing_information = "All necessary components and specifications are included."
        raw.installation_concerns = "The scope is sufficient to ensure that the new system will function properly."
        final = main.finalize_customer_analysis(raw, source("return_restriction"), 1)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertIn("return-side restriction", final.homeowner_takeaway)
        self.assertIn("work to correct", final.homeowner_takeaway)
        self.assertIn("still need to be verified", final.homeowner_takeaway)
        self.assertIn("return restriction is addressed", final.bottom_line)
        self.assertIn("sizing, equipment match, or duct scope", final.bottom_line)
        self.assertIn("No material sizing or duct-support", final.missing_information)
        self.assertIn("removal of the existing equipment", final.installation_concerns)
        self.assertIn("the return-duct correction", final.installation_concerns)
        self.assertTrue(any("specific return-duct correction" in s for s in final.good_signs))
        self.assertTrue(any("10-year parts warranty" in s for s in final.good_signs))
        self.assertFalse(any("-0.58" in s or "planned" in s for s in final.good_signs))
        prose = " ".join([final.homeowner_takeaway, final.bottom_line, final.installation_concerns,
                          final.missing_information, *final.good_signs])
        for phrase in ("ensur", "guarantee", "All necessary", "already meets"):
            self.assertNotIn(phrase, prose)
        from duct_airflow import present_supported_return_correction
        before = final.model_dump()
        present_supported_return_correction(final, source("return_restriction"), main.sizing_assessments(final),
                                            main.primary_equipment_matching_assessment(final))
        for field in ("technical_assessments", "decision", "contractor_questions", "market_price_context", "project_overview"):
            self.assertEqual(final.model_dump()[field], before[field])


if __name__ == "__main__":
    unittest.main()
