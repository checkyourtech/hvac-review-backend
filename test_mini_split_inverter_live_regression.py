"""Saved inverter-compressor response: one diagnosis, independent repair closeout."""
import contextlib
import html
import io
from pathlib import Path
import unittest

import main
import commissioning as cs
from test_mini_split_scope_routing import classification
from test_repair_diagnostics import upload
from test_technical_support_derivation import assessment

RESPONSE = Path(__file__).with_name('test_fixtures') / 'mini_split_inverter_bad_20261109_195525.json'
SOURCE = '''Existing ductless inverter mini-split.
Outdoor unit will not start.
Inverter fault has been reported.
Contractor states that the compressor is bad.
Contractor recommends compressor replacement.
No compressor isolation tests.
No inverter drive input/output tests.
No manufacturer results confirming failure.
Repair includes verification of system operation afterward.
$2,400 lump sum, parts and labor included.'''
VERIFICATION = 'Repair includes verification of system operation afterward.\n'
QUESTION = 'What testing establishes compressor failure rather than an inverter-drive or control problem?'


def saved_response():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


def finalize(raw=None, source=SOURCE):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw or saved_response(), source, 1,
                                              classification(), fresh_analysis=True)


class InverterCompressorLiveTests(unittest.TestCase):
    def assert_accepted(self, result):
        raw = saved_response()
        self.assertEqual(result.decision.technical_support, 'UNSUPPORTED')
        self.assertEqual(result.decision.verdict, 'GET_A_SECOND_OPINION')
        self.assertEqual(result.decision.pricing_transparency, 'LIMITED')
        self.assertEqual(result.price_facts, raw.price_facts)
        self.assertEqual(result.red_flags, raw.red_flags[1:])
        self.assertEqual(result.contractor_questions, [QUESTION, raw.contractor_questions[-1]])
        self.assertEqual(len(result.good_signs), 1)
        self.assertTrue(cs.generic_component_verification(result.good_signs[0]))
        self.assertEqual(result.technical_assessments, raw.technical_assessments[:1])
        self.assertFalse(cs.commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))
        self.assertNotIn('startup checks', result.model_dump_json().lower())
        self.assertNotIn('How Will Startup Be Verified?', main.build_report_html(result, 1))

    def test_exact_saved_response_through_production_upload(self):
        raw = saved_response()
        before = raw.model_dump()
        self.assertEqual(len(raw.red_flags), 2)
        self.assertEqual(len(raw.contractor_questions), 5)
        self.assertFalse(raw.good_signs)
        self.assertIn('How Will Startup Be Verified?', main.build_report_html(raw, 1))
        result, report = upload(raw, SOURCE, classification())
        self.assert_accepted(result)
        self.assertEqual(report, main.build_report_html(result, 1))
        for question in result.contractor_questions:
            self.assertEqual(html.unescape(report).count(question), 1)
        self.assertEqual(raw.model_dump(), before)

    def test_equivalent_drive_testing_gaps_are_one_diagnosis(self):
        for question, flag in [
            ('What inverter drive input/output testing was performed?', 'No inverter drive test results are documented.'),
            ('Can you provide documented tests for the inverter drive?', 'Missing inverter drive input/output tests.'),
        ]:
            with self.subTest(question=question):
                raw = saved_response()
                raw.contractor_questions[2] = question
                raw.red_flags[0] = flag
                self.assert_accepted(finalize(raw))

    def test_missing_verification_keeps_a_material_question(self):
        result = finalize(source=SOURCE.replace(VERIFICATION, ''))
        self.assertIn('How will operation be verified after the repair?', result.contractor_questions)
        self.assertFalse(result.good_signs)
        self.assertFalse(cs.commissioning_items(result))
        self.assertIn(QUESTION, result.contractor_questions)

    def test_existing_verification_positive_is_not_duplicated(self):
        for positive in ('The proposal includes verification of operation after the repair.',
                         'The proposal includes verification of operation after compressor replacement.'):
            with self.subTest(positive=positive):
                raw = saved_response()
                raw.good_signs = [positive]
                result = finalize(raw)
                self.assertEqual(result.good_signs, [positive])
                self.assertEqual(result.contractor_questions, [QUESTION, raw.contractor_questions[-1]])

    def test_actual_failed_verification_is_preserved(self):
        raw = saved_response()
        conflict = 'Required safety verification failed after compressor repair.'
        raw.technical_assessments.append(assessment('CONTRADICTORY', 'UNSUPPORTED',
            subject=cs.COMMISSIONING_SUBJECT, evidence=[conflict], contradictions=[conflict]))
        raw.red_flags.append(conflict)
        result = finalize(raw, SOURCE + '\n' + conflict)
        self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))
        self.assertTrue(cs.commissioning_items(result))
        self.assertTrue(any('verification' in q.lower() or 'check' in q.lower() for q in result.contractor_questions))

    def test_independent_drive_repair_keeps_its_own_evidence_question(self):
        raw = saved_response()
        evidence = 'The inverter drive board output was intermittent during testing.'
        raw.technical_assessments.append(assessment('INCOMPLETE', 'PARTIALLY_DEFINED',
            subject='Inverter drive board diagnosis', evidence=[evidence],
            gaps=['The inverter drive board fault has not been isolated.']))
        raw.contractor_questions.insert(0, 'What testing isolates the inverter drive board failure?')
        result = finalize(raw, SOURCE + '\nReplace the inverter drive board.\n' + evidence)
        self.assertTrue(any('drive board' in q.lower() for q in result.contractor_questions))
        self.assertTrue(any('compressor' in q.lower() for q in result.contractor_questions))
        self.assertTrue(any(evidence in a.documented_evidence for a in result.technical_assessments))

    def test_independent_specific_drive_concern_is_not_discarded(self):
        raw = saved_response()
        question = 'What explains the arcing observed at the inverter drive connector?'
        concern = 'Arcing was observed at the inverter drive connector.'
        raw.red_flags.append(concern)
        raw.contractor_questions.insert(0, question)
        result = finalize(raw, SOURCE + '\n' + concern)
        self.assertIn(concern, result.red_flags)
        self.assertIn(question, result.contractor_questions)

    def test_independent_refrigerant_concern_remains_material(self):
        raw = saved_response()
        from refrigerant_system import CAUSE
        conflict = 'The documented service-valve leak remains unrepaired.'
        raw.technical_assessments.append(assessment('CONTRADICTORY', 'UNSUPPORTED',
            subject=CAUSE, materiality='MATERIAL_SECONDARY', evidence=[conflict], contradictions=[conflict]))
        result = finalize(raw, SOURCE + '\n' + conflict)
        self.assertIn(conflict, ' '.join(result.red_flags))
        self.assertTrue(any('leak' in q.lower() for q in result.contractor_questions))

    def test_new_system_and_mixed_work_keep_system_review(self):
        for work in ('Install a new single-zone mini-split.',
                     'Install a new multi-zone mini-split.',
                     'Replace the complete mini-split system.'):
            with self.subTest(work=work):
                result = finalize(source=SOURCE + '\n' + work)
                self.assertTrue(cs.commissioning_items(result))
                self.assertTrue(main.sizing_assessments(result))

    def test_no_fixed_speed_or_invented_manufacturer_requirements(self):
        result = finalize()
        guidance = ' '.join(p for s in result.repair_evidence_sections for p in s.paragraphs)
        self.assertIn('applicable manufacturer', guidance)
        self.assertIn('inverter drive', guidance)
        for invented in ('24 volts', 'capacitor replacement first', 'superheat/subcooling', 'fault code E6'):
            self.assertNotIn(invented, guidance.lower())
        self.assertFalse(any(main.replacement_basis_question(q) for q in result.contractor_questions))

    def test_pricing_order_deduplication_and_question_limit(self):
        raw = saved_response()
        raw.contractor_questions *= 3
        result = finalize(raw)
        self.assert_accepted(result)
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]), 'pricing')
        self.assertLessEqual(len(result.contractor_questions), 6)

    def test_repeated_finalization_and_json_html_consistency(self):
        result = finalize()
        self.assert_accepted(result)
        for _ in range(3):
            again = finalize(main.HVACAnalysis.model_validate_json(result.model_dump_json()))
            self.assertEqual(result.model_dump(), again.model_dump())
            self.assertEqual(main.build_report_html(result, 1), main.build_report_html(again, 1))
            result = again


if __name__ == '__main__':
    unittest.main()
