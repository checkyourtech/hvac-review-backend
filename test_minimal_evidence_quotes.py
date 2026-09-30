"""Phase 2I: source presence, canonical owners, and unbypassed upload reports."""
import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import main
from compressor import compressor_items
from evidence_sufficiency import proposed_work_facts, ensure_primary_evidence_assessment
from test_technical_support_derivation import analysis_with, assessment

BARE = ("compressor", "blower_motor", "evaporator_coil", "full_replacement")
CASES = (*BARE, "sufficient")
INVENTED = ("97 amperes", "2.7 ohms", "0.3 megohms", "18 microfarads", "104 PSIG",
            "27 F superheat", "3 F subcooling", "detector confirmed valve leak", "R-410A",
            "E77", "0.92 in. w.c.", "1100 CFM", "5 tons", "AHRI 987654321",
            "MODEL-INVENTED", "10-year warranty", "manufacturer criterion ABC", "Manual J 34000")


def source(case):
    name = "minimal_but_sufficient_compressor" if case == "sufficient" else f"bare_bones_{case}"
    return Path(f"{name}_test.txt").read_text()


def classification(case):
    return main.QuoteClassification(quote_type="replacement" if case == "full_replacement" else "repair",
        system_type="unknown", primary_scope="Named replacement", modules_required=[])


def raw(case, malicious=False):
    a = analysis_with([], ai_support="SUPPORTED")
    if malicious:
        invented = ". ".join(INVENTED) + ". Isolated terminals short to ground. Manufacturer startup performed."
        a.technical_assessments = [assessment("CONFIRMED", subject=proposed_work_facts(source(case)).subject,
                                             evidence=[invented]),
                                  assessment("CONFIRMED", subject="Equipment matching", evidence=[invented])]
        a.replacement_context = main.ReplacementContext.ELECTIVE
        for name in ("project_overview", "equipment_analysis", "installation_concerns", "missing_information",
                     "pricing_review", "quote_comparison", "best_quote_recommendation", "contractor_vetting"):
            setattr(a, name, invented)
        a.good_signs = [invented]
        a.red_flags = [invented]
        a.contractor_questions = [f"Can you confirm {value}?" for value in INVENTED]
        a.decision.required_actions = [invented]
        a.decision.optional_suggestions = [invented]
        a.decision.verdict_reasons = [invented]
    return a


class MinimalEvidenceTests(unittest.TestCase):
    def final(self, case, malicious=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.finalize_customer_analysis(raw(case, malicious), source(case), 1, classification(case))

    def check_result(self, final, case):
        facts = proposed_work_facts(source(case))
        owner = next(a for a in final.technical_assessments if a.subject == facts.subject)
        self.assertEqual(final.decision.pricing_transparency, "LIMITED")
        self.assertEqual(final.market_price_context.status, main.MarketPriceStatus.NOT_EVALUATED)
        self.assertEqual(len(final.contractor_questions), 1 if case == "sufficient" else 2)
        if case == "sufficient":
            self.assertEqual(final.missing_information,
                             "No important technical information is missing from the submitted diagnosis.")
            self.assertNotIn("No important missing information was identified that appears likely to change the recommendation.",
                             main.build_report_html(final, 1))
            self.assertEqual(owner.diagnostic_evidence_status, "CONFIRMED")
            self.assertEqual(owner.scope_support, "APPROPRIATE")
            self.assertEqual(main.derive_technical_support(compressor_items(final)), "SUPPORTED")
            self.assertEqual(final.decision.technical_support, "SUPPORTED")
            self.assertEqual(final.decision.verdict, "REVIEW_BEFORE_APPROVING")
            self.assertEqual(final.red_flags, [])
            self.assertEqual(main.contractor_question_category(final.contractor_questions[0]), "pricing")
            self.assertEqual(main.commissioning_items(final), [])
            self.assertTrue(all(main.is_equivalent_itemization_action(a) for a in final.decision.required_actions))
            self.assertNotIn("How Will Startup Be Verified?", main.build_report_html(final, 1))
            self.assertIn("short to ground", main.build_report_html(final, 1))
        else:
            self.assertEqual((owner.materiality, owner.diagnostic_evidence_status, owner.scope_support),
                             ("PRIMARY", "ABSENT", "UNSUPPORTED"))
            self.assertEqual(final.decision.technical_support, "UNSUPPORTED")
            self.assertEqual(final.decision.verdict, "GET_A_SECOND_OPINION")
            self.assertEqual(len(final.red_flags), 1)
            self.assertEqual(final.good_signs, [])
            self.assertEqual(final.contractor_questions, [facts.question, f"What is included in the {facts.amount} total?"])

    def test_bare_compressor(self):
        final = self.final("compressor")
        self.check_result(final, "compressor")
        self.assertEqual(main.commissioning_items(final), [])

    def test_sizing_classifier_label_cannot_override_component_scope(self):
        for case in BARE:
            with self.subTest(case=case):
                classified = classification(case)
                classified.modules_required = [main.AnalysisModule.SYSTEM_SIZING]
                with contextlib.redirect_stdout(io.StringIO()):
                    final = main.finalize_customer_analysis(raw(case, True), source(case), 1, classified)
                self.check_result(final, case)
                html = main.build_report_html(final, 1)
                self.assertEqual(bool(main.sizing_assessments(final)), case == "full_replacement")
                self.assertEqual("Is the New System the Right Size?" in html, case == "full_replacement")
                if case != "full_replacement":
                    for phrase in ("Manual J", "home heating load", "home cooling load", "tonnage selection"):
                        self.assertNotIn(phrase, html)

    def test_component_with_explicit_capacity_issue_keeps_sizing(self):
        text = source("evaporator_coil") + "\nReview system capacity for a remodel; load calculation included."
        self.assertTrue(main.sizing_required(text, classification("evaporator_coil")))

    def test_bare_blower(self):
        self.check_result(self.final("blower_motor"), "blower_motor")

    def test_bare_coil(self):
        self.check_result(self.final("evaporator_coil"), "evaporator_coil")

    def test_bare_components_have_no_commissioning_domain(self):
        for case in BARE[:3]:
            with self.subTest(case=case):
                classified = classification(case)
                classified.modules_required = [main.AnalysisModule.COMMISSIONING]
                self.assertFalse(main.commissioning_required(source(case), classified))
                with contextlib.redirect_stdout(io.StringIO()):
                    final = main.finalize_customer_analysis(raw(case, True), source(case), 1, classified)
                self.check_result(final, case)
                self.assertEqual(main.commissioning_items(final), [])
                self.assertNotIn("How Will Startup Be Verified?", main.build_report_html(final, 1))

    def test_explicit_coil_startup_scope_still_requires_commissioning(self):
        for detail in ("Manufacturer startup plan included.",
                       "Completed startup record supplied.",
                       "Required startup safety check failed."):
            with self.subTest(detail=detail):
                self.assertTrue(main.commissioning_required(source("evaporator_coil") + "\n" + detail))

    def test_bare_system_prioritizes_basis(self):
        final = self.final("full_replacement")
        self.check_result(final, "full_replacement")
        questions = " ".join(final.contractor_questions)
        for value in ("Manual J", "AHRI", "static", "startup", "warranty", "permit"):
            self.assertNotIn(value, questions)

    def test_short_direct_failure_is_supported(self):
        self.check_result(self.final("sufficient"), "sufficient")

    def test_clean_proceed_retains_general_no_gap_wording(self):
        original = analysis_with([assessment("CONFIRMED", evidence=["Documented failure finding."])],
                                 ai_support="SUPPORTED")
        original.missing_information = ""
        original.decision.pricing_transparency = "ADEQUATE"
        original.decision.required_actions = []
        with contextlib.redirect_stdout(io.StringIO()):
            final = main.finalize_customer_analysis(original)
        self.assertEqual(final.decision.verdict, "PROCEED")
        self.assertEqual(final.missing_information,
                         "No important missing information was identified that appears likely to change the recommendation.")

    def test_sparse_missing_evidence_remains_specific(self):
        for case in BARE:
            with self.subTest(case=case):
                final = self.final(case)
                facts = proposed_work_facts(source(case))
                self.assertIn(facts.component, final.missing_information)
                self.assertNotIn("No important technical information", final.missing_information)

    def test_all_five_real_upload_paths_with_omitted_and_invented_ai_facts(self):
        for case in CASES:
            for malicious in (False, True):
                with self.subTest(case=case, malicious=malicious):
                    original = raw(case, malicious)
                    before = original.model_dump()
                    classified = classification(case)
                    if malicious and case in BARE:
                        classified.modules_required = [main.AnalysisModule.SYSTEM_SIZING, main.AnalysisModule.COMMISSIONING]
                    completions = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=a))])
                                   for a in (classified, original)]
                    captured = {}
                    def email(**kwargs):
                        captured["analysis"] = kwargs["analysis"]
                        captured["html"] = main.build_report_html(kwargs["analysis"], 1)
                    with contextlib.redirect_stdout(io.StringIO()), patch.object(
                            main.client.beta.chat.completions, "parse", side_effect=completions) as api, patch.object(main, "send_review_email", side_effect=email):
                        response = TestClient(main.app).post("/upload", data={"package":"tier1", "customer_name":"Offline", "customer_email":"offline@example.com"},
                            files={"files": ("sample.txt", source(case).encode(), "text/plain")})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(api.call_count, 2)  # Real classifier and analyzer, no finalizer mocks.
                    final = captured["analysis"]
                    self.check_result(final, case)
                    self.assertEqual(response.text, captured["html"])
                    if case in BARE:
                        self.assertEqual("Is the New System the Right Size?" in response.text,
                                         case == "full_replacement")
                        self.assertEqual("How Will Startup Be Verified?" in response.text,
                                         case == "full_replacement")
                    self.assertEqual(original.model_dump(), before)
                    for fabricated in INVENTED:
                        self.assertNotIn(fabricated, response.text)
                        self.assertNotIn(fabricated, str(final.model_dump()))
                    if case != "sufficient":
                        self.assertNotIn("short to ground", response.text)
                    if case in {"compressor", "sufficient"}:
                        self.assertIn("What Does the Compressor Evidence Show?", response.text)
                    if case == "evaporator_coil":
                        self.assertIn("What Does the Refrigerant Evidence Show?", response.text)

    def test_source_presence_not_word_count(self):
        short = "Replace compressor — $4,950."
        long = "ARTIFICIAL SAMPLE QUOTE\n" + "\n".join([short] * 200)
        self.assertTrue(proposed_work_facts(long).scope_only)
        self.assertTrue(proposed_work_facts(short).scope_only)
        self.assertFalse(proposed_work_facts(source("sufficient")).scope_only)
        with contextlib.redirect_stdout(io.StringIO()):
            result = main.finalize_customer_analysis(raw("compressor"), long, 1)
        self.assertEqual(result.decision.technical_support, "UNSUPPORTED")

    def test_unknown_evidence_or_elective_intent_abstains(self):
        for clause in ("Homeowner requested a planned voluntary upgrade; system currently operating.",
                       "Inspection documents an open winding.", "A new manufacturer diagnostic finding is attached.",
                       "1-year parts and labor warranty."):
            self.assertFalse(proposed_work_facts(source("compressor") + clause).scope_only)

    def test_multiple_quotes_and_prices_abstain(self):
        self.assertIsNone(proposed_work_facts("QUOTE 1\n" + source("compressor") + "\nQUOTE 2\n" + source("blower_motor")))
        self.assertIsNone(proposed_work_facts(source("compressor") + "\nLabor $1000"))

    def test_failed_component_name_is_not_evidence(self):
        self.assertTrue(proposed_work_facts("Replace failed compressor — $4,950.").scope_only)

    def test_shared_fallback_never_overrides_canonical_assessment(self):
        facts = proposed_work_facts(source("compressor"))
        a = analysis_with([assessment("CONFIRMED", subject=facts.subject, evidence=["Direct source evidence recovered."])])
        before = a.model_dump()
        ensure_primary_evidence_assessment(a, facts, main.TechnicalEvidenceAssessment)
        self.assertEqual(before, a.model_dump())

    def test_raw_final_and_renderer_invariants(self):
        for case in CASES:
            final = self.final(case, True)
            before = final.model_dump()
            main.build_report_html(final, 1)
            self.assertEqual(before, final.model_dump())
            with contextlib.redirect_stdout(io.StringIO()):
                twice = main.finalize_customer_analysis(final, source(case), 1)
            self.assertEqual(before, twice.model_dump())

    def test_price_does_not_create_technical_flag(self):
        final = self.final("compressor")
        self.assertEqual(len(final.red_flags), 1)
        self.assertNotIn("price", final.red_flags[0])
        for phrase in ("markup", "profit", "wholesale", "hourly"):
            self.assertNotIn(phrase, " ".join(final.contractor_questions))


if __name__ == "__main__":
    unittest.main()
