"""Offline Phase 2D integration, evidence, presentation and routing regressions."""
import ast
import inspect
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from system_sizing import (
    SIZING_SUBJECT, SYSTEM_SIZING_RULES, sizing_required, sizing_text,
    normalize_capacity, exclusive_area_rule, explicit_capacity_values,
)

LOAD_EVIDENCE = [
    "Sample Home D cooling load: 34,000 Btu/h; heating load: 58,000 Btu/h.",
    "Proposed cooling capacity: 35,000 Btu/h at the submitted design conditions; "
    "furnace output: 60,000 Btu/h. The selection supports these building loads.",
]


def sizing(status="ADEQUATE", scope="APPROPRIATE", *, subject=SIZING_SUBJECT,
           evidence=None, contradictions=None, gaps=None):
    return main.TechnicalEvidenceAssessment(
        subject=subject, materiality="PRIMARY", diagnostic_evidence_status=status,
        scope_support=scope, documented_evidence=LOAD_EVIDENCE if evidence is None else evidence,
        contradictions=contradictions or [], material_gaps=gaps or [],
    )


def analysis(*items, pricing="ADEQUATE"):
    return main.HVACAnalysis(
        project_overview="The homeowner requests replacement.",
        equipment_analysis="The listed components are documented together.",
        missing_information="No important information is missing.",
        installation_concerns="The proposal includes startup verification.",
        pricing_review="Equipment and installation are separately priced.",
        quote_comparison="", best_quote_recommendation="", contractor_vetting="",
        red_flags=[], good_signs=[], contractor_questions=[], recommendation="Raw response",
        decision=main.HVACDecision(verdict="PROCEED", technical_support="SUPPORTED",
                                   pricing_transparency=pricing),
        technical_assessments=list(items),
    )


def domain_items():
    return [
        sizing(subject="Basis for full-system replacement",
               evidence=["The homeowner requests a planned replacement of working equipment."]),
        sizing(subject="Quoted indoor/outdoor equipment compatibility",
               evidence=["Submitted manufacturer documentation identifies the exact models together."]),
    ]


def fixture(name):
    return Path(f"system_sizing_{name}_test.txt").read_text()


class SizingOnlyTests(unittest.TestCase):
    """Keep accepted Phase 2D scenarios independent of new duct completeness.

    These fixtures only assert generic duct review, not Phase 2E evidence. Actual
    unmocked duct completeness and upload integration live in test_duct_airflow.
    No production bypass is introduced; supplied duct assessments still normalize.
    """
    def setUp(self):
        super().setUp()
        # Preserve Phase 2D isolation; Phase 2F tests the unchanged fixture's
        # real whole-report commissioning gap without this scope mock.
        startup_patcher = patch("commissioning.commissioning_required", return_value=False)
        startup_patcher.start()
        self.addCleanup(startup_patcher.stop)
        patcher = patch("duct_airflow.duct_required", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)


class SizingRoutingTests(SizingOnlyTests):
    def test_registry_and_gap(self):
        self.assertEqual(main.AnalysisModule.SYSTEM_SIZING.value, "system_sizing")
        self.assertEqual(set(main.ANALYSIS_MODULES), set(main.AnalysisModule))
        self.assertEqual(main.ANALYSIS_MODULES[main.AnalysisModule.SYSTEM_SIZING], SYSTEM_SIZING_RULES)
        self.assertNotIn("sizing", main.PHASE_2_MODULE_GAPS)

    def test_capacity_scopes_route(self):
        for text in (
            "Complete HVAC system replacement", "Furnace replacement",
            "AC replacement", "Heat-pump replacement", "Packaged-system replacement",
            "Mini-split installation", "Install a new HVAC system",
            "Replace the complete system during an addition/remodel",
            "The HVAC system is oversized", "The HVAC system is undersized",
            "Manual J load calculation attached", "The contractor states a sizing method",
            "Install a new air handler", "Full system replacement",
        ):
            with self.subTest(text=text):
                self.assertTrue(sizing_required(text))
                c = main.QuoteClassification(quote_type="unknown", system_type="unknown",
                                             primary_scope="Unknown", modules_required=[])
                self.assertIn("SYSTEM SIZING / CAPACITY JUSTIFICATION", main.get_analysis_knowledge(c, text))
                self.assertEqual(c.modules_required, [])

    def test_minor_repairs_do_not_route(self):
        for text in (
            "Replace furnace control board", "Replace furnace igniter",
            "Replace the capacitor in the existing 3-ton AC system",
            "Replace compressor in existing system", "Replace flame sensor",
            "Add refrigerant to the 4-ton system", "Repair the condensate drain",
            "Routine maintenance on the heat pump", "Replace condenser fan motor",
            "Replace return duct; the return is undersized",
            "Furnace replacement is not recommended", "Homeowner declined HVAC system replacement",
        ):
            with self.subTest(text=text):
                self.assertFalse(sizing_required(text))

    def test_classification_completeness(self):
        c = main.QuoteClassification(quote_type="replacement", system_type="heat pump",
            primary_scope="Replace equipment", replacement_components=["heat pump"], modules_required=[])
        self.assertTrue(sizing_required("", c))
        final = main.finalize_customer_analysis(analysis(), classification=c)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_classifier_restores_module_without_live_call(self):
        c = main.QuoteClassification(quote_type="replacement", system_type="furnace",
                                    primary_scope="Furnace replacement", modules_required=[])
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=c))])
        with patch.object(main.client.beta.chat.completions, "parse", return_value=response):
            result = main.classify_quotes("Furnace replacement")
        self.assertIn(main.AnalysisModule.SYSTEM_SIZING, result.modules_required)

    def test_existing_repair_fixtures_stay_out(self):
        for name in ("electrical_capacitor_good_test.txt", "electrical_igniter_good_test.txt",
                     "electrical_control_board_bad_test.txt", "heat_pump_defrost_good_test.txt",
                     "airflow_static_good_test.txt", "ductwork_distribution_good_test.txt"):
            with self.subTest(name=name):
                self.assertFalse(sizing_required(Path(name).read_text()))


class SizingCalibrationTests(SizingOnlyTests):
    def finalize(self, item, quote=""):
        return main.finalize_customer_analysis(analysis(*domain_items(), item), quote_text=quote)

    def test_supported_load_summary(self):
        final = self.finalize(sizing(), fixture("good"))
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])

    def test_missing_basis_is_partial_even_if_raw_absent(self):
        for status, scope in (("ABSENT", "UNSUPPORTED"), ("INCOMPLETE", "UNSUPPORTED"),
                              ("ADEQUATE", "APPROPRIATE"), ("CONTRADICTORY", "UNSUPPORTED")):
            with self.subTest(status=status):
                raw = sizing(status, scope, evidence=["Proposed system: 3 tons."],
                             contradictions=["No Manual J is supplied."])
                before = raw.model_dump()
                final = self.finalize(raw, fixture("partial"))
                item = main.sizing_assessments(final)[0]
                self.assertEqual((item.diagnostic_evidence_status, item.scope_support), ("INCOMPLETE", "PARTIALLY_DEFINED"))
                self.assertEqual(raw.model_dump(), before)
                self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
                self.assertEqual(final.red_flags, [])

    def test_manual_j_reference_future_and_no_capacity(self):
        for evidence in ("Manual J included.", "Manual J will be completed before final equipment selection.",
                         "Exact capacity and models are pending."):
            with self.subTest(evidence=evidence):
                final = self.finalize(sizing(evidence=[evidence]), evidence)
                self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
                self.assertEqual(len(final.contractor_questions), 1)
                self.assertEqual(final.red_flags, [])

    def test_like_for_like_history_not_proof(self):
        for history in ("good comfort and no humidity or runtime complaints", "longstanding poor cooling"):
            text = f"Same size, 3 tons; home unchanged; {history}."
            final = self.finalize(sizing(evidence=[text]), text)
            self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
            self.assertIn("keeping the same size", final.contractor_questions[0])

    def test_remodel_is_partial_and_question_specific(self):
        final = self.finalize(sizing("ABSENT", "UNSUPPORTED", evidence=[]), fixture("remodel"))
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("added or changed space", final.contractor_questions[0])
        self.assertNotIn("must increase", final.model_dump_json())

    def test_submitted_conflict_remains_unsupported(self):
        conflict = "Same-project sizing document selects C36 at 3 tons, but the quote proposes C60 at 5 tons without explanation."
        raw = sizing("CONTRADICTORY", "UNSUPPORTED", contradictions=[conflict])
        final = self.finalize(raw, fixture("bad"))
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(main.sizing_assessments(final)[0].contradictions, [conflict])
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertIn("load-based 3-ton selection", final.contractor_questions[0])
        self.assertIn("quoted 5-ton system", final.contractor_questions[0])
        self.assertIn("updated sizing results", final.contractor_questions[0])

    def test_wrong_project_conflict_is_not_missing_paperwork(self):
        final = self.finalize(sizing("CONTRADICTORY", "UNSUPPORTED", contradictions=[
            "The submitted sizing document belongs to another home, not this project."]))
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_exclusive_final_rule_only(self):
        text = "Final sizing was based ONLY on 1 ton per 500 square feet, with no building-specific load review."
        final = self.finalize(sizing(evidence=[text]), text)
        item = main.sizing_assessments(final)[0]
        self.assertEqual((item.diagnostic_evidence_status, item.scope_support), ("INCOMPLETE", "UNSUPPORTED"))
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(item.contradictions, [])
        for preliminary in ("Preliminary estimate: 1 ton per 500 square feet.",
                            "We sized this house at 1 ton per 500 square feet.",
                            "Final sizing is not based only on 1 ton per 500 square feet."):
            self.assertFalse(exclusive_area_rule(preliminary))
            self.assertEqual(self.finalize(sizing(evidence=[preliminary]), preliminary).decision.technical_support,
                             "PARTIALLY_SUPPORTED")

    def test_equivalent_subjects(self):
        for subject in ("System size selection", "Proposed tonnage", "HVAC capacity selection",
                        "Heat pump sizing", "Furnace capacity", "Equipment capacity for the home",
                        "Load-based equipment selection"):
            with self.subTest(subject=subject):
                final = self.finalize(sizing("ABSENT", "UNSUPPORTED", subject=subject, evidence=[]))
                self.assertEqual(len(main.sizing_assessments(final)), 1)
                self.assertEqual(main.sizing_assessments(final)[0].subject, SIZING_SUBJECT)
                self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_missing_assessment_restored_only_on_copy(self):
        raw = analysis(*domain_items())
        before = raw.model_dump()
        final = main.finalize_customer_analysis(raw, quote_text="Complete HVAC system replacement. Exact system capacity is pending.")
        self.assertEqual(raw.model_dump(), before)
        item = main.sizing_assessments(final)[0]
        self.assertEqual(item.documented_evidence, [])
        self.assertEqual(item.diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_firewalls_cannot_supply_sizing_support(self):
        for evidence in ("AHRI matched 3-ton system", "Grounded compressor justifies replacement",
                         "Duct airflow supports 3 tons", "High SEER2 and HSPF2 efficiency",
                         "Variable speed means oversizing is not a concern", "Home is 1500 square feet",
                         "Model HP036 proves 3 tons; AH048 proves 4 tons"):
            with self.subTest(evidence=evidence):
                self.assertEqual(self.finalize(sizing(evidence=[evidence])).decision.technical_support,
                                 "PARTIALLY_SUPPORTED")

    def test_domain_assessments_preserved(self):
        raw = analysis(*domain_items(), sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]))
        before = [i.model_dump() for i in raw.technical_assessments[:2]]
        final = main.finalize_customer_analysis(raw)
        self.assertEqual([i.model_dump() for i in final.technical_assessments[:2]], before)

    def test_aggregation_function_unchanged(self):
        original = subprocess.check_output(["git", "show", "HEAD:main.py"], text=True)
        def node(text):
            return ast.dump(next(n for n in ast.parse(text).body
                                 if isinstance(n, ast.FunctionDef) and n.name == "derive_technical_support"))
        self.assertEqual(node(original), node(Path("main.py").read_text()))


class CapacityNormalizationTests(SizingOnlyTests):
    def test_tons_and_rate_units(self):
        for value, unit, context, expected in ((3.5, "tons", False, 42000),
                                              (36, "kBtu/h", False, 36000),
                                              (60, "MBH", True, 60000),
                                              (34000, "BTUH", False, 34000)):
            self.assertEqual(normalize_capacity(value, unit, hvac_heat_rate=context)["btu_per_hour"], expected)

    def test_ambiguous_units_not_interpreted(self):
        self.assertIsNone(normalize_capacity(60000, "BTU")["btu_per_hour"])
        self.assertIsNone(normalize_capacity(60, "MBH")["btu_per_hour"])

    def test_labels_preserved(self):
        labels = dict(mode="heating", rating="input", basis="nominal", project="D",
                      system="furnace", zone="upstairs", design_conditions="20 F")
        result = normalize_capacity(60, "kBtu/h", **labels)
        for key, value in labels.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["rating"], "input")

    def test_invalid_numbers_rejected(self):
        for value in ("NaN", "Infinity", -3):
            with self.assertRaises(ValueError):
                normalize_capacity(value, "tons")

    def test_furnace_input_and_afue_not_output(self):
        raw = analysis(sizing(evidence=["Furnace input: 60,000 Btu/h; AFUE 96%. Same size as old unit."]))
        final = main.finalize_customer_analysis(raw)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertNotIn("57,600", final.equipment_analysis)

    def test_nominal_tonnage_not_cold_weather_output(self):
        final = main.finalize_customer_analysis(analysis(sizing(evidence=[
            "Proposed heat pump has nominal cooling capacity 3 tons; cold weather output is not supplied."])))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_backup_strategy_can_support_heat_pump(self):
        evidence = ["Sample Home D cooling load: 34,000 Btu/h; heating load: 48,000 Btu/h.",
                    "Proposed heat pump heating output: 28,000 Btu/h at 5 F; simultaneous supplemental "
                    "output 34,120 Btu/h covers the 20,000 Btu/h difference. Selection supports both loads."]
        final = main.finalize_customer_analysis(analysis(*domain_items(), sizing(evidence=evidence)),
                                               quote_text=fixture("heat_pump"))
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.red_flags, [])


class SizingPresentationTests(SizingOnlyTests):
    def partial_raw(self):
        raw = analysis(*domain_items(), sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]),
                       pricing="LIMITED")
        raw.pricing_review = "The breakdown is reasonably clear, but more parts/labor detail would enhance transparency."
        raw.contractor_questions = [
            "How did you determine the size of the new system?",
            "Can you provide the building-specific load calculations that justify the selected cooling and heating capacities?",
            "Can you detail the startup process and verification steps included in the proposal?",
            "Can you provide an itemized breakdown of the parts, labor, and other charges included in the $18,000 total?",
        ]
        return raw

    def test_partial_question_and_pricing_cleanup(self):
        final = main.finalize_customer_analysis(self.partial_raw(), quote_text=fixture("partial"))
        item = main.sizing_assessments(final)[0]
        self.assertEqual((item.diagnostic_evidence_status, item.scope_support),
                         ("INCOMPLETE", "PARTIALLY_DEFINED"))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [
            "Can you show me the load calculation or explain how you determined the 3-ton cooling system and 60,000 Btu/h furnace output for this home?"
        ])
        self.assertEqual(main.contractor_question_category(final.contractor_questions[0]), "system_sizing")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        for amount in ("$18,000", "$11,000", "$7,000"):
            self.assertIn(amount, final.pricing_review)
        self.assertNotIn("enhance transparency", final.pricing_review)
        report = main.build_report_html(final)
        self.assertIn("Is the New System the Right Size?", report)
        for value in ("3-ton", "36,000 Btu/h", "60,000 Btu/h"):
            self.assertIn(value, report)
        paragraphs = " ".join(main.system_sizing_report_paragraphs(final))
        self.assertEqual(paragraphs.count("the home's load is not shown in the submitted information"), 2)
        self.assertNotRegex(paragraphs, r"oversized|undersized")

    def test_partial_capacity_question_uses_submitted_values_and_fallback(self):
        text = fixture("partial").replace("3 refrigeration tons", "4 refrigeration tons").replace("60,000", "80,000")
        final = main.finalize_customer_analysis(self.partial_raw(), quote_text=text)
        self.assertIn("4-ton cooling system and 80,000 Btu/h furnace output", final.contractor_questions[0])
        text = text.replace("Cooling capacity: 4 refrigeration tons. Furnace output: 80,000 Btu/h.", "")
        final = main.finalize_customer_analysis(self.partial_raw(), quote_text=text)
        self.assertEqual(final.contractor_questions[0], "How did you determine the size of the new system?")

    def test_partial_limited_pricing_keeps_existing_question(self):
        text = fixture("partial").replace("Equipment: $11,000.", "").replace("Labor and installation materials: $7,000.", "")
        final = main.finalize_customer_analysis(self.partial_raw(), quote_text=text)
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions],
                         ["system_sizing", "pricing"])
        self.assertEqual(final.contractor_questions[-1], main.deterministic_pricing_question(final, None, text))

    def test_partial_independent_commissioning_gap_preserves_question(self):
        raw = self.partial_raw()
        raw.technical_assessments.append(sizing("INCOMPLETE", "PARTIALLY_DEFINED",
            subject="Startup commissioning", evidence=[], gaps=["Startup verification steps are missing."]))
        final = main.finalize_customer_analysis(raw, quote_text=fixture("partial"))
        self.assertIn("commissioning", [main.contractor_question_category(q) for q in final.contractor_questions])

    def test_partial_prose_flags_and_questions_are_calibrated(self):
        raw = analysis(*domain_items(), sizing("ABSENT", "UNSUPPORTED", evidence=[]))
        raw.equipment_analysis = "The system is oversized. The selected tonnage guarantees comfort and savings."
        raw.red_flags = ["No sizing evidence", "Manual J missing", "The duct is visibly damaged."]
        raw.good_signs = ["The same size guarantees savings.", "The proposal includes startup verification."]
        raw.contractor_questions = ["What load calculation supports this?", "How was the system size verified?",
                                    "Can you provide Manual J?", "What evidence supports the proposed tonnage?"]
        final = main.finalize_customer_analysis(raw, quote_text=fixture("partial"))
        self.assertEqual(final.red_flags, ["The duct is visibly damaged."])
        self.assertEqual(len(final.contractor_questions), 1)
        for text in (final.equipment_analysis, final.bottom_line, final.missing_information):
            for prohibited in ("oversized", "wrong size", "guarantee", "insufficient to substantiate"):
                self.assertNotIn(prohibited, text)
        self.assertIn("startup verification", " ".join(final.good_signs))

    def test_contradiction_flags_deduplicate_without_losing_other_issue(self):
        raw = analysis(sizing("CONTRADICTORY", "UNSUPPORTED", contradictions=["Sizing document selects different capacity for this project."]))
        raw.red_flags = ["Sizing document conflicts", "Proposed capacity is wrong", "The duct is visibly damaged."]
        final = main.finalize_customer_analysis(raw)
        self.assertEqual(len(final.red_flags), 2)
        self.assertIn("The duct is visibly damaged.", final.red_flags)

    def test_sizing_question_category_precedes_generic_categories(self):
        for question in ("What evidence supports sizing?", "Can you verify the proposed system capacity?",
                         "Why keep the same size?", "How did you determine the size of the new system?"):
            self.assertEqual(main.contractor_question_category(question), "system_sizing")

    def test_pricing_separate(self):
        raw = analysis(sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]), pricing="LIMITED")
        final = main.finalize_customer_analysis(raw)
        self.assertEqual(len(final.contractor_questions), 2)
        self.assertEqual(main.contractor_question_category(final.contractor_questions[0]), "system_sizing")
        self.assertEqual(main.contractor_question_category(final.contractor_questions[1]), "pricing")

    def test_rendered_support_and_verdict_consistent(self):
        for item, expected, verdict in ((sizing(), "SUPPORTED", "PROCEED"),
                (sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]), "PARTIALLY_SUPPORTED", "REVIEW BEFORE APPROVING"),
                (sizing("CONTRADICTORY", "UNSUPPORTED", contradictions=["Document selects different capacity."]), "UNSUPPORTED", "GET A SECOND OPINION")):
            with self.subTest(expected=expected):
                raw = analysis(*domain_items(), item)
                final = main.finalize_customer_analysis(raw)
                report = main.build_report_html(final, quote_count=1)
                self.assertEqual(final.decision.technical_support, expected)
                self.assertIn(expected.replace("_", " ").title(), report)
                self.assertTrue(final.recommendation.startswith(verdict))
                if expected != "SUPPORTED":
                    self.assertIn("size" if expected == "PARTIALLY_SUPPORTED" else "capacity", final.bottom_line)

    def test_elective_matching_override_cannot_hide_bad_sizing(self):
        raw = analysis(*domain_items(), sizing("CONTRADICTORY", "UNSUPPORTED",
                                              contradictions=["The sizing document specifies a different capacity."]))
        raw.replacement_context = main.ReplacementContext.ELECTIVE
        raw.technical_assessments[1].diagnostic_evidence_status = "INCOMPLETE"
        raw.technical_assessments[1].scope_support = "PARTIALLY_DEFINED"
        final = main.finalize_customer_analysis(raw)
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertTrue(final.recommendation.startswith("GET A SECOND OPINION"))
        self.assertIn("cooling-capacity change", final.bottom_line)
        self.assertIn("updated sizing results", final.bottom_line)

    def test_whole_legacy_good_matching_now_requires_sizing(self):
        raw = analysis(*domain_items())
        final = main.finalize_customer_analysis(raw, quote_text=Path("equipment_matching_good_test.txt").read_text())
        self.assertEqual(main.primary_equipment_matching_assessment(final).diagnostic_evidence_status, "ADEQUATE")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(len(main.sizing_assessments(final)), 1)

    def test_whole_legacy_replacement_fixtures_preserve_basis(self):
        for name, status, scope in (("good", "ADEQUATE", "APPROPRIATE"),
                                   ("partial", "INCOMPLETE", "PARTIALLY_DEFINED"),
                                   ("bad", "ABSENT", "UNSUPPORTED"),
                                   ("elective", "ADEQUATE", "APPROPRIATE")):
            with self.subTest(name=name):
                basis = domain_items()[0].model_copy(update=dict(diagnostic_evidence_status=status, scope_support=scope))
                raw = analysis(basis)
                if name == "elective":
                    raw.replacement_context = main.ReplacementContext.ELECTIVE
                final = main.finalize_customer_analysis(raw, quote_text=Path(f"replacement_basis_{name}_test.txt").read_text())
                self.assertEqual(main.replacement_basis_assessment(final).diagnostic_evidence_status, status)
                self.assertTrue(main.sizing_assessments(final))
                self.assertNotEqual(final.decision.verdict, "PROCEED")

    def test_finalization_is_idempotent(self):
        once = main.finalize_customer_analysis(analysis(*domain_items(), sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[])))
        twice = main.finalize_customer_analysis(once)
        self.assertEqual(once.model_dump(), twice.model_dump())

    def test_prompt_boundaries(self):
        for phrase in ("AFUE is not exact", "100%", "400 CFM/ton", "024/030/036/042/048/060",
                       "same served space", "zone loads", "never ABSENT", "preliminary", "humidity"):
            self.assertIn(phrase.lower(), SYSTEM_SIZING_RULES.lower())


class SizingBoundaryRegressions(SizingOnlyTests):
    def test_capacity_changes_and_investigation_route(self):
        for text in ("Capacity change: 3 tons to 4 tons", "Increase system capacity to 4 tons",
                     "Investigate HVAC capacity for the addition", "HVAC capacity selection for the remodel"):
            self.assertTrue(sizing_required(text), text)

    def test_thermostat_repair_does_not_trigger_completeness(self):
        self.assertFalse(sizing_required("Install new thermostat for existing system"))

    def test_explicit_values_are_normalized_without_model_decoding(self):
        text = "Zone A heating output: 60 MBH. Zone B cooling capacity: 3 tons. Model HP036."
        values = explicit_capacity_values(text)
        self.assertEqual([v["btu_per_hour"] for v in values], [60000, 36000])
        self.assertTrue(all(v["source_text"] == text for v in values))
        self.assertEqual(explicit_capacity_values("Model HP036 or HP048"), [])

    def test_furnace_load_plus_input_is_still_not_output(self):
        final = main.finalize_customer_analysis(analysis(sizing(evidence=[
            "Home A heating load: 58,000 Btu/h; proposed furnace capacity input 60,000 Btu/h.",
            "The contractor says this selection supports the load; AFUE 96%."
        ])))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_nominal_heat_pump_with_load_needs_heating_strategy(self):
        final = main.finalize_customer_analysis(analysis(sizing(evidence=[
            "Home A heating load: 48,000 Btu/h; cooling load: 34,000 Btu/h.",
            "Proposed heat pump nominal cooling capacity: 3 tons. Selection supports these loads."
        ])))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_rule_method_in_contradictions_does_not_become_document_conflict(self):
        text = "Final sizing based ONLY on 1 ton per 500 square feet with no building-specific review."
        final = main.finalize_customer_analysis(analysis(sizing("CONTRADICTORY", "UNSUPPORTED",
                                                               evidence=[text], contradictions=[text])))
        item = main.sizing_assessments(final)[0]
        self.assertEqual(item.diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(item.scope_support, "UNSUPPORTED")
        self.assertEqual(item.contradictions, [])

    def test_explicit_no_contradiction_is_not_a_conflict(self):
        final = main.finalize_customer_analysis(analysis(sizing("CONTRADICTORY", "UNSUPPORTED",
            evidence=[], contradictions=["No contradictory sizing information is supplied."])))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.red_flags, [])

    def test_independent_missing_information_survives_boilerplate_cleanup(self):
        raw = analysis(sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]))
        raw.missing_information = "No important information is missing. The required disconnect is not identified."
        final = main.finalize_customer_analysis(raw)
        self.assertIn("disconnect", final.missing_information)
        self.assertNotIn("No important", final.missing_information)

    def test_overview_does_not_claim_wrong_size(self):
        raw = analysis(sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]))
        raw.project_overview = "The contractor selected the wrong size."
        final = main.finalize_customer_analysis(raw)
        self.assertNotIn("wrong size", final.project_overview)
        self.assertTrue(final.project_overview)

    def test_independent_backup_gap_can_have_second_question(self):
        raw = analysis(sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[],
                              gaps=["Supplemental heat capacity covering the heating load is not documented."]))
        raw.contractor_questions = ["How much backup capacity covers the heating load?",
                                    "How will supplemental heat cover heating needs?"]
        final = main.finalize_customer_analysis(raw)
        self.assertEqual(len(final.contractor_questions), 2)
        self.assertEqual([main.contractor_question_category(q) for q in final.contractor_questions],
                         ["system_sizing", "system_sizing_backup"])

    def test_backup_checklist_suppressed_without_material_gap(self):
        raw = analysis(sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]))
        raw.contractor_questions = ["What supplemental capacity will cover the heating load?"]
        final = main.finalize_customer_analysis(raw)
        self.assertEqual(len(final.contractor_questions), 1)

    def test_zone_evidence_remains_separate(self):
        first = sizing(subject=SIZING_SUBJECT + " — Project A, Zone 1")
        second = sizing("ABSENT", "UNSUPPORTED", subject=SIZING_SUBJECT + " — Project B, Zone 2", evidence=[])
        final = main.finalize_customer_analysis(analysis(first, second))
        items = main.sizing_assessments(final)
        self.assertEqual(items[0].subject, first.subject)
        self.assertEqual(items[1].subject, second.subject)
        self.assertEqual(items[1].documented_evidence, [])
        self.assertEqual(items[1].diagnostic_evidence_status, "INCOMPLETE")
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")

    def test_genuine_refrigerant_issue_survives_sizing_guard(self):
        low_charge = sizing("ABSENT", "UNSUPPORTED", subject="Low refrigerant diagnosis", evidence=[])
        raw = analysis(low_charge, sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[]))
        final = main.finalize_customer_analysis(raw, quote_text="System low on refrigerant. Add refrigerant.")
        self.assertTrue(any("low-charge diagnosis" in flag for flag in final.red_flags))
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_major_policy_functions_and_schema_unchanged(self):
        original = ast.parse(subprocess.check_output(["git", "show", "HEAD:main.py"], text=True))
        current = ast.parse(Path("main.py").read_text())
        for name in ("TechnicalEvidenceAssessment", "determine_verdict", "normalize_replacement_pricing_transparency"):
            before = next(node for node in original.body if getattr(node, "name", None) == name)
            after = next(node for node in current.body if getattr(node, "name", None) == name)
            self.assertEqual(ast.dump(before), ast.dump(after), name)


class SizingGoodAcceptanceRegressionTests(SizingOnlyTests):
    # The captured live artifact contains final HTML, not the raw AI assessment.
    # This is a controlled reconstruction using the GOOD fixture's facts, not an
    # assertion that these were the exact raw strings from that live request.
    evidence = [
        "Building-specific load calculation documents 34,000 Btu/h cooling and 58,000 Btu/h heating. "
        "The selected 3-ton AC delivers 35,000 Btu/h cooling and furnace delivers 60,000 Btu/h heating "
        "at the submitted design conditions, supporting the selection."
    ]
    missing = (
        "No significant missing information regarding equipment match or size was identified. "
        "However, it's always beneficial to double-check the specific model numbers and capacities "
        "during installation to ensure accuracy."
    )
    installation = (
        "The installation scope is comprehensive, covering removal of the existing system, and "
        "installation of new equipment per manufacturer specifications, complete with required "
        "permits and startup verification. This indicates a solid plan for proper execution."
    )

    def raw(self, subject="Equipment size for the home"):
        raw = analysis(*domain_items(), sizing(subject=subject, evidence=self.evidence))
        raw.missing_information = self.missing
        raw.installation_concerns = self.installation
        raw.contractor_questions = ["How did you determine the size of the new system?"]
        return raw

    def test_valid_supported_subject_variants_do_not_get_fallback(self):
        for subject in ("System size selection", "HVAC sizing", "Proposed system capacity",
                        "Cooling capacity selection", "Heating capacity selection",
                        "Load-based equipment selection", "Manual J sizing", "Equipment size for the home",
                        "Furnace sizing", "Heat pump sizing", "Proposed tonnage"):
            with self.subTest(subject=subject):
                raw = self.raw(subject)
                before = raw.model_dump()
                final = main.finalize_customer_analysis(raw, quote_text=fixture("good"))
                items = main.sizing_assessments(final)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].diagnostic_evidence_status, "ADEQUATE")
                self.assertEqual(items[0].scope_support, "APPROPRIATE")
                self.assertEqual(items[0].documented_evidence[:len(self.evidence)], self.evidence)
                self.assertEqual(raw.model_dump(), before)
                self.assertEqual(final.decision.technical_support, "SUPPORTED")
                self.assertEqual(final.decision.verdict, "PROCEED")
                self.assertEqual(final.red_flags, [])
                self.assertEqual(final.contractor_questions, [])

    def test_good_fixture_contains_explicit_output_and_load_basis(self):
        from system_sizing import usable_load_basis
        text = fixture("good")
        for fact in ("Sample Home D", "34,000 Btu/h", "58,000 Btu/h", "3 refrigeration tons",
                     "Furnace heating output: 60,000 Btu/h", "35,000 Btu/h", "2026-09-01"):
            self.assertIn(fact, text)
        self.assertTrue(usable_load_basis([text]))
        self.assertTrue(usable_load_basis(self.evidence))

    def test_supported_case_has_sizing_positive_without_filler_or_overclaims(self):
        final = main.finalize_customer_analysis(self.raw(), quote_text=fixture("good"))
        self.assertTrue(any("load" in sign for sign in final.good_signs))
        sizing_review = " ".join(main.system_sizing_report_paragraphs(final))
        self.assertIn("sizing looks reasonable", sizing_review)
        self.assertIn("reasonably track the calculated loads", sizing_review)
        self.assertNotIn("verify how", final.missing_information)
        self.assertNotIn("double-check", final.missing_information)
        self.assertNotIn("comprehensive", final.installation_concerns)
        self.assertNotIn("proper execution", final.installation_concerns)
        self.assertIn("startup", final.installation_concerns)

    def test_real_partial_removes_contradictory_nothing_missing_sentence(self):
        raw = self.raw()
        raw.technical_assessments[-1] = sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[])
        final = main.finalize_customer_analysis(raw, quote_text=fixture("partial"))
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertNotIn("No significant missing information", final.missing_information)
        self.assertNotIn("double-check", final.missing_information)
        self.assertIn("calculated heating and cooling loads", final.missing_information)
        self.assertEqual(len(final.contractor_questions), 1)

    def test_omitted_assessment_recovers_explicit_good_source(self):
        raw = analysis(*domain_items())
        before = raw.model_dump()
        final = main.finalize_customer_analysis(raw, quote_text=fixture("good"))
        self.assertEqual(main.sizing_assessments(final)[0].diagnostic_evidence_status, "ADEQUATE")
        self.assertTrue(main.sizing_assessments(final)[0].documented_evidence[0].startswith("Submitted sizing source:"))
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(raw.model_dump(), before)

    def test_factual_installation_does_not_invent_permits(self):
        raw = self.raw()
        raw.technical_assessments = [raw.technical_assessments[-1]]
        text = "Remove existing equipment. Install new equipment. Include startup verification. No permits are included."
        final = main.finalize_customer_analysis(raw, quote_text=text)
        self.assertNotIn("permit", final.installation_concerns.lower())
        self.assertIn("startup verification", final.installation_concerns)
        self.assertEqual(final.decision.verdict, "PROCEED")

    def test_independent_question_survives_supported_sizing(self):
        raw = self.raw()
        raw.technical_assessments.append(sizing("INCOMPLETE", "PARTIALLY_DEFINED",
                                               subject="Warranty coverage", evidence=[], gaps=["Warranty coverage unclear."]))
        raw.contractor_questions.append("What warranty coverage is included?")
        final = main.finalize_customer_analysis(raw, quote_text=fixture("good"))
        self.assertTrue(any("warranty" in q.lower() for q in final.contractor_questions))
        self.assertFalse(any(main.contractor_question_category(q) == "system_sizing" for q in final.contractor_questions))

    def test_capacity_numbers_without_building_load_are_not_support(self):
        from system_sizing import usable_load_basis
        self.assertFalse(usable_load_basis(["Proposed capacity: 3 tons cooling. Furnace output: 60,000 Btu/h. Selection supported by matching."]))

    def test_duct_and_matching_subjects_do_not_count_as_sizing(self):
        for subject in ("Equipment matching", "Quoted indoor/outdoor equipment compatibility",
                        "Return duct sizing", "Blower airflow capacity", "Static pressure verification"):
            self.assertFalse(main.sizing_assessments(analysis(sizing(subject=subject))))

    def test_upload_pipeline_recovers_omitted_sizing_from_source(self):
        raw = analysis(*domain_items())
        with patch.object(self, "raw", return_value=raw):
            self.test_upload_pipeline_with_mocked_ai_preserves_supported_paraphrase()
        self.assertEqual(main.sizing_assessments(raw), [])

    def test_upload_pipeline_with_mocked_ai_preserves_supported_paraphrase(self):
        import tempfile
        from fastapi.testclient import TestClient
        raw = self.raw()
        c = main.QuoteClassification(quote_type="replacement", system_type="AC and furnace",
            primary_scope="Complete HVAC system replacement", replacement_components=["furnace", "outdoor unit"],
            modules_required=[main.AnalysisModule.REPAIR_VS_REPLACE, main.AnalysisModule.EQUIPMENT_MATCHING,
                              main.AnalysisModule.SYSTEM_SIZING, main.AnalysisModule.PRICING])
        def response(value):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=value))])
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(main, "UPLOAD_DIR", Path(directory)), \
             patch.object(main.client.beta.chat.completions, "parse", side_effect=[response(c), response(raw)]) as parse, \
             patch.object(main, "send_review_email") as email:
            result = TestClient(main.app).post("/upload", data=dict(
                package="tier1", customer_name="Sizing regression", customer_email="test@example.com"),
                files=[("files", ("system_sizing_good_test.txt", fixture("good").encode(), "text/plain"))])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(parse.call_count, 2)
        final = email.call_args.kwargs["analysis"]
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertIn("Diagnostic Support</strong>\n                Supported", result.text)
        self.assertNotIn("How did you determine the size", result.text)
        self.assertNotIn("proper execution", result.text)
        self.assertNotIn("comprehensive", result.text)
        self.assertEqual(result.text, main.build_report_html(final, quote_count=1))


class SizingVisibleSectionTests(SizingOnlyTests):
    def test_supported_wording_is_factual_and_sizing_specific(self):
        raw = analysis(*domain_items())
        raw.missing_information = "All necessary details regarding equipment matching, capacity, and installation scope appear to be adequately provided."
        raw.good_signs = ["Clear documentation tying the equipment match to the expected cooling and heating outputs, along with the load calculations, ensures better performance compatibility."]
        raw.installation_concerns = "There are no major concerns regarding the adequacy of the installation plan."
        final = main.finalize_customer_analysis(raw, quote_text=fixture("good"))
        report = main.build_report_html(final)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(final.contractor_questions, [])
        self.assertEqual(final.missing_information, "No important sizing information is missing from the submitted proposal.")
        self.assertIn("building-specific load results", final.good_signs[0])
        for phrase in ("ensures better performance", "performance compatibility", "all necessary details", "adequately provided", "adequacy of the installation plan"):
            self.assertNotIn(phrase, report.lower())
        self.assertIn("3-ton", final.bottom_line)
        self.assertIn("60,000 Btu/h furnace output", final.bottom_line)
        self.assertIn("startup verification", final.installation_concerns)
        self.assertIn("permits", final.installation_concerns)
        for value in ("34,000", "36,000", "35,000", "58,000", "60,000"):
            self.assertIn(value, self.section(report))

    def test_supported_bottom_line_uses_changed_source_values(self):
        text = fixture("good").replace("3 refrigeration tons", "3.5 refrigeration tons").replace("60,000", "64,000")
        final = main.finalize_customer_analysis(analysis(*domain_items()), quote_text=text)
        self.assertIn("3.5-ton", final.bottom_line)
        self.assertIn("64,000 Btu/h furnace output", final.bottom_line)
        self.assertNotIn("60,000", final.bottom_line)

    def test_installation_cleanup_does_not_add_undocumented_permits(self):
        raw = analysis(*domain_items())
        raw.installation_concerns = "No major concerns regarding the adequacy of the installation plan."
        final = main.finalize_customer_analysis(raw, quote_text=fixture("good").replace("permits, ", ""))
        self.assertNotIn("permits", final.installation_concerns)
        self.assertIn("startup verification", final.installation_concerns)

    def report(self, name):
        items = domain_items()
        if name == "bad":
            items.append(sizing("CONTRADICTORY", "UNSUPPORTED", evidence=[], contradictions=[
                "The same-project submitted selection specifies 3 tons but the quote proposes 5 tons."]))
        final = main.finalize_customer_analysis(analysis(*items), quote_text=fixture(name))
        return final, main.build_report_html(final, quote_count=1)

    def section(self, report):
        import re, html
        match = re.search(r'<h2>Is the New System the Right Size\?</h2>(.*?)</div>', report, re.S)
        self.assertIsNotNone(match)
        return html.unescape(re.sub(r'<[^>]+>', ' ', match[1]))

    def test_good_numbers_relationship_and_no_question(self):
        final, report = self.report("good")
        section = self.section(report)
        for value in ("34,000 Btu/h", "58,000 Btu/h", "3-ton", "36,000 Btu/h nominal", "35,000 Btu/h", "60,000 Btu/h"):
            self.assertIn(value, section)
        self.assertIn("Cooling:", section)
        self.assertIn("Heating:", section)
        self.assertIn("sizing looks reasonable", section)
        self.assertIn("reasonably track the calculated loads", section)
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.contractor_questions, [])
        self.assertIn("load", final.good_signs[0])
        self.assertLess(report.index("Is the New System the Right Size?"), report.index("Price &amp; Value Review"))
        self.assertNotIn("34,000", final.equipment_analysis)

    def test_partial_proposed_capacity_and_missing_basis(self):
        final, report = self.report("partial")
        section = self.section(report)
        self.assertIn("3-ton", section)
        self.assertIn("load is not shown", section)
        self.assertIn("cannot tell whether the equipment fits this home's needs", section)
        self.assertNotIn("wrong size", section)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.contractor_questions), 1)

    def test_bad_displays_both_selections_once(self):
        final, report = self.report("bad")
        section = self.section(report)
        self.assertIn("selection calls for a 3-ton system", section)
        self.assertIn("quote proposes a 5-ton system", section)
        self.assertIn("explains that change", section)
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(len(final.red_flags), 1)

    def test_bad_customer_fields_keep_one_useful_question_and_match_positive(self):
        raw = analysis(
            *domain_items(),
            sizing(
                "CONTRADICTORY",
                "UNSUPPORTED",
                evidence=[],
                contradictions=[
                    "The same-project submitted selection specifies 3 tons but the quote proposes 5 tons."
                ],
            ),
        )
        raw.contractor_questions = [
            "Why did the cooling size change from 3 tons to 5 tons?",
            "How will the proposed larger system affect energy efficiency and potential comfort issues in the home?",
        ]
        raw.missing_information = (
            "This clarity is essential for understanding the rationale behind the proposed replacement equipment size."
        )
        raw.good_signs = [
            "The matched equipment, necessary installation steps, permits, and startup verification are documented."
        ]

        final = main.finalize_customer_analysis(
            raw,
            quote_text=fixture("bad"),
            quote_count=1,
        )
        section = self.section(main.build_report_html(final, quote_count=1))

        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
        self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
        self.assertEqual(final.decision.pricing_transparency, "ADEQUATE")
        self.assertEqual(len(final.red_flags), 1)
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertEqual(
            main.contractor_question_category(final.contractor_questions[0]),
            "system_sizing",
        )
        self.assertNotIn("energy efficiency", final.contractor_questions[0].lower())
        self.assertNotIn("comfort", final.contractor_questions[0].lower())
        self.assertIn("submitted 3-ton sizing selection", final.missing_information)
        self.assertIn("5-ton system", final.missing_information)
        self.assertIn("updated sizing results", final.missing_information)
        for phrase in (
            "rationale behind",
            "essential for understanding",
            "capacity verification",
            "reconcile",
            "methodology",
        ):
            self.assertNotIn(phrase, final.missing_information.lower())
        self.assertEqual(
            final.good_signs,
            [
                "The submitted documentation shows that the indoor and outdoor equipment "
                "are an approved matched combination."
            ],
        )
        matching = main.primary_equipment_matching_assessment(final)
        self.assertEqual(matching.diagnostic_evidence_status, "ADEQUATE")
        self.assertEqual(matching.scope_support, "APPROPRIATE")
        self.assertIn("34,000 Btu/h", section)
        self.assertIn("selection calls for a 3-ton system", section)
        self.assertIn("quote proposes a 5-ton system", section)
        self.assertIn("58,000 Btu/h", section)
        self.assertIn("60,000 Btu/h", section)
        self.assertIn("cooling side", section)
        self.assertIn("does not establish a problem with the furnace size", section)
        self.assertIn("shorter cycles", section)
        self.assertIn("humidity control", section)

    def test_remodel_changed_space_without_assuming_larger(self):
        final, report = self.report("remodel")
        section = self.section(report)
        self.assertIn("changes the conditioned space", section)
        self.assertIn("updated sizing review", section)
        self.assertNotIn("must increase", section)
        self.assertEqual(final.red_flags, [])
        self.assertIn("added or changed space", final.contractor_questions[0])

    def test_remodel_live_question_and_scope_voice_cleanup(self):
        raw = analysis(*domain_items())
        raw.contractor_questions = [
            "Does the sizing review include the added or changed space?",
            "What specific parameters were considered when determining that a 3-ton system "
            "would be sufficient given the home's changes?",
        ]
        raw.good_signs = [
            "The submitted documentation shows the indoor and outdoor equipment are an approved matched combination.",
            "Warranty coverage includes 10 years for parts and 2 years for labor, which is a positive sign of reliability.",
        ]
        raw.installation_concerns = (
            "The installation scope appears adequate. Permits and startup establish quality installation and compliance."
        )
        quote = fixture("remodel")
        baseline = main.finalize_customer_analysis(analysis(*domain_items()), quote_text=quote)
        final = main.finalize_customer_analysis(raw, quote_text=quote)
        rendered = main.build_report_html(final)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
        self.assertEqual(final.red_flags, [])
        self.assertEqual(len(final.contractor_questions), 1)
        self.assertEqual(main.contractor_question_category(final.contractor_questions[0]), "system_sizing")
        self.assertIn("added or changed space", final.contractor_questions[0])
        self.assertEqual(final.good_signs, [
            "The submitted documentation shows the indoor and outdoor equipment are an approved matched combination.",
            "The proposal lists 10-year parts warranty and 2-year labor warranty.",
        ])
        self.assertNotIn("reliability", rendered)
        self.assertIn("permits", final.installation_concerns)
        self.assertIn("startup verification", final.installation_concerns)
        self.assertNotIn("appears adequate", final.installation_concerns)
        self.assertNotIn("compliance", final.installation_concerns)
        section = self.section(rendered)
        for fact in ("3-ton", "36,000", "60,000", "added or changed space", "does not mean the old size is wrong"):
            self.assertIn(fact, section)
        self.assertIn("new system has to be larger", section)
        self.assertEqual(final.price_facts, baseline.price_facts)
        self.assertEqual(final.market_price_context, baseline.market_price_context)
        self.assertEqual(final.decision.pricing_transparency, baseline.decision.pricing_transparency)
        self.assertNotIn("high-level breakdown", rendered)

    def test_remodel_scope_cleanup_does_not_invent_permits_or_erase_independent_concern(self):
        raw = analysis(*domain_items())
        raw.installation_concerns = (
            "The installation scope appears adequate. The drain connection remains unresolved."
        )
        raw.technical_assessments.append(sizing(
            "INCOMPLETE", "PARTIALLY_DEFINED", subject="Condensate drain connection",
            evidence=[], gaps=["The drain connection remains unresolved."],
        ))
        final = main.finalize_customer_analysis(raw, quote_text=fixture("remodel").replace("permits, ", ""))
        self.assertNotIn("permits", final.installation_concerns)
        self.assertIn("drain connection remains unresolved", final.installation_concerns)

    def test_heat_pump_backup_and_separate_ratings(self):
        final, report = self.report("heat_pump")
        section = self.section(report)
        self.assertIn("48,000 Btu/h", section)
        self.assertIn("28,000 Btu/h", section)
        self.assertIn("34,120 Btu/h", section)
        self.assertIn("not being asked to carry the entire heating load by itself", section)
        self.assertIn("36,000 Btu/h nominal cooling capacity", section)
        self.assertEqual(final.decision.technical_support, "SUPPORTED")

    def test_heat_pump_useful_math_and_scope_voice(self):
        raw = analysis(*domain_items())
        raw.installation_concerns = (
            "The proposal includes startup verification. The duct review is essential for ensuring "
            "that the equipment can perform adequately with the current ductwork."
        )
        final = main.finalize_customer_analysis(raw, quote_text=fixture("heat_pump"))
        section = self.section(main.build_report_html(final))
        self.assertEqual(final.decision.technical_support, "SUPPORTED")
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.red_flags, [])
        for value in ("34,000", "35,000", "36,000", "48,000", "28,000", "20,000", "34,120"):
            self.assertIn(value, section)
        self.assertIn("remaining", section)
        self.assertIn("does not by itself mean the heat pump is undersized", section)
        self.assertIn("together cover the submitted design load", final.bottom_line)
        self.assertIn("3-ton", final.bottom_line)
        self.assertIn("The proposal includes a duct review.", final.installation_concerns)
        self.assertNotIn("ensuring", final.installation_concerns)
        self.assertIn("how the quote is divided", final.pricing_review)
        self.assertNotIn("high-level breakdown", main.build_report_html(final))
        self.assertEqual(final.market_price_context.status.value, "not_evaluated")

    def test_heat_pump_remaining_load_is_dynamic_and_requires_comparable_source(self):
        from system_sizing import comparable_heat_pump_plan, sizing_review_paragraphs
        source = fixture("heat_pump")
        changed = source.replace("48,000", "50,000").replace("20,000", "22,000")
        self.assertEqual(comparable_heat_pump_plan(changed)[1], 22000)
        self.assertIn("leaving roughly 22,000", " ".join(sizing_review_paragraphs([changed], "ADEQUATE", "APPROPRIATE")))
        for invalid in (
            source.replace("at the stated heating design conditions", "at an unspecified condition"),
            source.replace("for Sample Home D", "for Another Home"),
            source.replace("permitting simultaneous operation", "with operation details unspecified"),
            source.replace("34,120 Btu/h", "an unspecified amount"),
            source + "\nProject: Another Home",
        ):
            with self.subTest(source=invalid):
                self.assertIsNone(comparable_heat_pump_plan(invalid))
                self.assertNotIn("leaving roughly", " ".join(sizing_review_paragraphs([invalid], "ADEQUATE", "APPROPRIATE")))

    def test_section_absent_for_minor_repair(self):
        raw = analysis()
        final = main.finalize_customer_analysis(raw, quote_text="Replace a capacitor in the existing 3-ton system.")
        report = main.build_report_html(final, quote_count=1)
        self.assertNotIn("Is the New System the Right Size?", report)

    def test_furnace_input_not_output_in_section(self):
        raw = analysis(sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[
            "Heating load: 58,000 Btu/h. Furnace input: 60,000 Btu/h. AFUE 96%."
        ]))
        final = main.finalize_customer_analysis(raw)
        section = self.section(main.build_report_html(final))
        self.assertIn("input, not output", section)
        self.assertIn("heating side cannot be fully verified", section)
        self.assertNotIn("57,600", section)
        self.assertNotIn("Cooling:", section)

    def test_nominal_heat_pump_rating_not_heating_output(self):
        raw = analysis(sizing("INCOMPLETE", "PARTIALLY_DEFINED", evidence=[
            "Proposed heat pump. Nominal cooling capacity: 3 tons. Heating load: 48,000 Btu/h."
        ]))
        final = main.finalize_customer_analysis(raw)
        section = self.section(main.build_report_html(final))
        self.assertIn("heating design condition is not documented", section)
        self.assertNotIn("heating output is 36,000", section)
        self.assertNotIn("backup heat to cover", section)

    def test_nothing_missing_claim_removed_and_sections_not_repetitive(self):
        raw = analysis(*domain_items())
        raw.missing_information = "No critical information appears to be missing. All necessary equipment specifications and performance data are present."
        final = main.finalize_customer_analysis(raw, quote_text=fixture("partial"))
        self.assertNotIn("No critical", final.missing_information)
        self.assertNotIn("All necessary", final.missing_information)
        self.assertIn("does not include the home's calculated heating and cooling loads", final.missing_information)
        sections = [final.homeowner_takeaway, final.missing_information, final.bottom_line, final.banner_explanation]
        self.assertEqual(len(set(sections)), len(sections))
        self.assertNotIn("verify how the new system size", final.equipment_analysis)

    def test_submitted_summary_recovers_omitted_or_terse_model_evidence(self):
        for items in (domain_items(), [*domain_items(), sizing(evidence=["The submitted load summary supports the selected equipment."])]):
            raw = analysis(*items)
            before = raw.model_dump()
            final = main.finalize_customer_analysis(raw, quote_text=fixture("good"))
            self.assertEqual(final.decision.verdict, "PROCEED")
            self.assertEqual(raw.model_dump(), before)
            self.assertEqual(len(main.sizing_assessments(final)), 1)

    def test_source_recovery_does_not_overrule_real_conflict(self):
        raw = analysis(sizing("CONTRADICTORY", "UNSUPPORTED", contradictions=[
            "A separate submitted engineer revision identifies a conflicting selection."
        ]))
        final = main.finalize_customer_analysis(raw, quote_text=fixture("good"))
        self.assertEqual(final.decision.technical_support, "UNSUPPORTED")

    def test_no_cross_quote_recovery(self):
        raw = analysis(*domain_items())
        text = "QUOTE 1\n" + fixture("good") + "\nQUOTE 2\n" + fixture("partial")
        final = main.finalize_customer_analysis(raw, quote_text=text)
        self.assertEqual(final.decision.technical_support, "PARTIALLY_SUPPORTED")
        self.assertEqual(main.sizing_assessments(final)[0].documented_evidence, [])

    def test_clear_source_recovery_requires_identity_and_output(self):
        from system_sizing import clear_submitted_sizing_support, submitted_sizing_excerpt
        text = fixture("good")
        self.assertFalse(clear_submitted_sizing_support(submitted_sizing_excerpt(text.replace("for Sample Home D", "for Different Home"))))
        self.assertFalse(clear_submitted_sizing_support(submitted_sizing_excerpt(text.replace("Furnace heating output", "Furnace input").replace("furnace output", "furnace input"))))

    def test_rendering_escapes_source_text(self):
        raw = analysis(sizing("CONTRADICTORY", "UNSUPPORTED", contradictions=["<script>alert(1)</script> sizing conflict"]))
        final = main.finalize_customer_analysis(raw)
        report = main.build_report_html(final)
        self.assertNotIn("<script>alert(1)</script>", report)


if __name__ == "__main__":
    unittest.main()
