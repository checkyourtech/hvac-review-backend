"""Replay the exact saved 2026-10-21 live response without model calls."""
from pathlib import Path
import unittest
from unittest.mock import patch

import main
import electrical_controls as ec
from repair_diagnostics import normalize_blower_report_details
from test_blower_support_normalization import BAD, classified, finalize
from test_repair_diagnostics import upload
from test_technical_support_derivation import assessment


RESPONSE = Path(__file__).with_name('test_fixtures') / 'blower_live_response_20261021_044722.json'


def saved_response():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


class BlowerLiveResponseTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('Live network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def assert_independent_positives(self, result):
        self.assertEqual(len(result.good_signs), 2)
        warranty = [s for s in result.good_signs if 'warranty' in s.lower()]
        scope = [s for s in result.good_signs if 'scope' in s.lower()]
        self.assertEqual(len(warranty), 1)
        self.assertEqual(len(scope), 1)
        self.assertNotIn('scope', warranty[0].lower())
        self.assertIn('1-year parts and labor warranty', warranty[0])
        for fact in ('motor removal', 'motor installation', 'blower reassembly', 'operation testing'):
            self.assertIn(fact, scope[0])

    def test_exact_saved_response_finalizes_to_independent_facts_and_two_questions(self):
        incoming = saved_response()
        before = incoming.model_dump()
        result, report = upload(incoming, BAD, classified())
        self.assert_independent_positives(result)
        self.assertEqual(result.decision.technical_support, 'UNSUPPORTED')
        self.assertEqual(result.decision.verdict, 'GET_A_SECOND_OPINION')
        self.assertEqual(result.decision.pricing_transparency, 'LIMITED')
        self.assertEqual(len(result.red_flags), 1)
        self.assertEqual(result.contractor_questions,
                         [incoming.contractor_questions[0], incoming.contractor_questions[2]])
        self.assertEqual(ec.electrical_items(result), [])
        self.assertEqual(result.installation_concerns, result.good_signs[1])
        self.assertNotIn('does not specify any verification steps', report)
        self.assertNotIn(incoming.contractor_questions[1], report)
        self.assertNotIn('Quote Comparison', report)
        self.assertNotIn('Best Quote Recommendation', report)
        self.assertEqual(report, main.build_report_html(result, quote_count=1))
        self.assertEqual(incoming.model_dump(), before)

    def test_saved_response_finalization_is_idempotent(self):
        first = finalize(saved_response())
        for _ in range(3):
            again = finalize(first)
            self.assertEqual(again.model_dump(), first.model_dump())
            self.assertEqual(main.build_report_html(again, 1), main.build_report_html(first, 1))
            first = again
        self.assert_independent_positives(first)
        self.assertEqual(len(first.contractor_questions), 2)

    def test_equivalent_mixed_positive_wording_is_split_without_losing_facts(self):
        for mixed in (
            saved_response().good_signs[0],
            'The proposal includes blower replacement scope along with a one-year parts and labor warranty.',
            'A 1-year parts and labor warranty accompanies the documented blower replacement scope.',
        ):
            with self.subTest(mixed=mixed):
                incoming = saved_response()
                incoming.good_signs[0] = mixed
                result = finalize(incoming)
                self.assert_independent_positives(result)

    def test_combined_positive_alone_retains_both_source_supported_facts(self):
        incoming = saved_response()
        incoming.good_signs = incoming.good_signs[:1]
        self.assert_independent_positives(finalize(incoming))

    def test_independent_facts_and_special_warranty_terms_are_preserved(self):
        extras = ['The quote includes a permit.', 'New mounting brackets are included.',
                  'The 1-year parts and labor warranty also includes an annual inspection.']
        incoming = saved_response()
        incoming.good_signs.extend(extras)
        result = finalize(incoming, BAD + '\n' + '\n'.join(extras))
        for extra in extras:
            self.assertIn(extra, result.good_signs)
        self.assertEqual(len(result.good_signs), 5)

    def test_combined_positive_with_an_independent_detail_is_not_discarded(self):
        incoming = saved_response()
        mixed = incoming.good_signs[0][:-1] + ', plus an annual inspection.'
        incoming.good_signs[0] = mixed
        result = finalize(incoming, BAD + '\nAnnual inspection included.')
        self.assertIn(mixed, result.good_signs)

    def test_absent_or_different_warranty_does_not_manufacture_claimed_terms(self):
        for replacement in ('', '90-day labor warranty.'):
            with self.subTest(replacement=replacement):
                incoming = saved_response()
                incoming.good_signs = []
                result = finalize(incoming, BAD.replace('1-year parts and labor warranty.', replacement))
                self.assertFalse(any('1-year' in s for s in result.good_signs))
                if replacement:
                    self.assertTrue(any('90-day labor warranty' in s for s in result.good_signs))

    def test_equivalent_generic_verification_questions_are_not_material(self):
        for question in (
            saved_response().contractor_questions[1],
            'How will proper operation be verified once the replacement motor is installed?',
            'What checks will confirm normal operation following motor installation?',
        ):
            with self.subTest(question=question):
                incoming = saved_response()
                incoming.contractor_questions[1] = question
                result = finalize(incoming)
                self.assertEqual(len(result.contractor_questions), 2)
                self.assertNotIn(question, result.contractor_questions)

    def test_detail_omission_without_a_material_basis_is_not_an_installation_gap(self):
        for concern in (
            saved_response().installation_concerns,
            'The quote does not describe specific airflow or control verification procedures after repair.',
        ):
            with self.subTest(concern=concern):
                incoming = saved_response()
                incoming.installation_concerns = concern
                result = finalize(incoming)
                self.assertIn('planned system operation testing', result.installation_concerns)
                self.assertNotIn('does not', result.installation_concerns)
                self.assertEqual(len(result.contractor_questions), 2)

    def test_actual_exclusion_or_absent_operation_testing_keeps_question(self):
        for scope in ('', 'System operation testing is not included.',
                      '- Test system operation\nDo not test system operation.'):
            with self.subTest(scope=scope):
                incoming = saved_response()
                result = finalize(incoming, BAD.replace('- Test system operation', scope))
                self.assertIn(incoming.contractor_questions[1], result.contractor_questions)

    def test_specific_procedural_omission_keeps_concern_and_question(self):
        concern = 'The required blower airflow setting is not specified.'
        question = 'How will the required blower airflow setting be verified after the repair?'
        incoming = saved_response()
        incoming.installation_concerns = concern
        incoming.contractor_questions[1] = question
        incoming.decision.required_actions.append(concern)
        result = finalize(incoming, BAD + '\n' + concern)
        self.assertIn(concern, result.installation_concerns)
        self.assertIn(question, result.contractor_questions)

    def test_source_backed_requirement_preserves_generic_followup(self):
        requirement = 'The required post-repair airflow verification is explicitly excluded.'
        incoming = saved_response()
        incoming.installation_concerns = requirement
        result = finalize(incoming, BAD + '\n' + requirement)
        self.assertIn(requirement, result.installation_concerns)
        self.assertIn(incoming.contractor_questions[1], result.contractor_questions)

    def test_independent_structured_procedural_gap_preserves_followup(self):
        incoming = saved_response()
        incoming.technical_assessments.append(assessment(
            'INCOMPLETE', 'PARTIALLY_DEFINED', materiality='MATERIAL_SECONDARY',
            subject='Startup and commissioning plan',
            gaps=['The required startup checks are not described.']))
        original = incoming.contractor_questions[1]
        normalize_blower_report_details(incoming, BAD)
        self.assertIn(original, incoming.contractor_questions)

    def test_documented_conflict_is_not_removed_as_a_detail_omission(self):
        conflict = 'The specified blower control wiring conflicts with the required wiring configuration.'
        incoming = saved_response()
        incoming.installation_concerns = conflict
        incoming.contractor_questions[1] = 'How will the incorrect blower control wiring be corrected and verified?'
        result = finalize(incoming, BAD + '\n' + conflict)
        self.assertIn(conflict, result.installation_concerns)
        self.assertIn(incoming.contractor_questions[1], result.contractor_questions)


if __name__ == '__main__':
    unittest.main()
