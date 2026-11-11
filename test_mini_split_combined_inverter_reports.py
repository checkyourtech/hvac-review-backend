"""Exact saved analyses with bounded source proposals for offline report replay."""
import contextlib
import html
import io
from pathlib import Path
import unittest

import main
import commissioning as cs
import electrical_controls as ec
from test_mini_split_inverter_live_regression import (
    saved_response as compressor_response, SOURCE as COMPRESSOR_SOURCE,
)
from test_mini_split_scope_routing import classification
from test_mini_split_component_evidence import raw as evidence_raw, source as evidence_source
from test_repair_diagnostics import upload
from test_technical_support_derivation import assessment

RESPONSE = Path(__file__).with_name('test_fixtures') / 'mini_split_pcb_bad_20261109_205230.json'
# Reconstruction from the saved analysis: the original submitted PCB quote was
# not included in the saved HTML/JSON. No diagnostic readings are reconstructed.
PCB_SOURCE = '''Existing ductless inverter mini-split.
Inverter fault reported.
Contractor states that the inverter drive PCB has failed.
Replace the outdoor inverter drive board only.
No specific drive input/output measurements, manufacturer diagnostic results, or component isolation findings documented.
Verify system operation after repair.
Total price: $1,250 lump sum,
parts and labor included.'''
VERIFICATION = 'Verify system operation after repair.\n'


def pcb_response():
    return main.HVACAnalysis.model_validate_json(RESPONSE.read_text())


def finalize(raw=None, source=PCB_SOURCE):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(raw or pcb_response(), source, 1,
                                              classification(), fresh_analysis=True)


class CombinedInverterReportTests(unittest.TestCase):
    def assert_boundary(self, result):
        self.assertEqual(result.decision.technical_support, 'UNSUPPORTED')
        self.assertEqual(result.decision.verdict, 'GET_A_SECOND_OPINION')
        self.assertEqual(result.decision.pricing_transparency, 'LIMITED')
        self.assertFalse(cs.commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))
        self.assertFalse(any(main.replacement_basis_question(q) for q in result.contractor_questions))
        self.assertNotIn('How Will Startup Be Verified?', main.build_report_html(result, 1))

    def assert_one_diagnosis(self, result, original):
        self.assert_boundary(result)
        self.assertEqual(len(result.red_flags), 1)
        self.assertEqual(len(result.contractor_questions), 2)
        self.assertEqual(result.contractor_questions[-1], original.contractor_questions[-1])
        self.assertEqual(result.price_facts, original.price_facts)
        self.assertEqual(len(result.good_signs), 1)
        self.assertTrue(cs.generic_component_verification(result.good_signs[0]))
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]), 'pricing')

    def test_both_exact_saved_responses_through_upload(self):
        for raw, source in ((compressor_response(), COMPRESSOR_SOURCE), (pcb_response(), PCB_SOURCE)):
            with self.subTest(subject=raw.technical_assessments[0].subject):
                original = raw.model_dump()
                self.assertEqual(len(raw.red_flags), 2)
                result, report = upload(raw, source, classification())
                self.assert_one_diagnosis(result, raw)
                self.assertEqual(report, main.build_report_html(result, 1))
                for question in result.contractor_questions:
                    self.assertEqual(html.unescape(report).count(question), 1)
                self.assertEqual(raw.model_dump(), original)

    def test_pcb_equivalent_missing_evidence_flags_and_questions(self):
        for flag, question in (
            ('No diagnostic evidence supports the failure claim.', 'What measurements support this diagnosis?'),
            ('Missing drive input/output tests leave the diagnosis unverified.', 'Can you provide the diagnostic test results?'),
        ):
            with self.subTest(question=question):
                raw = pcb_response()
                raw.red_flags.append(flag)
                raw.contractor_questions.insert(1, question)
                result = finalize(raw)
                self.assert_one_diagnosis(result, raw)
                self.assertIn('isolates', result.contractor_questions[0])

    def test_pcb_planned_verification_does_not_need_granular_procedures(self):
        result = finalize()
        self.assertFalse(any('after the repair' in q for q in result.contractor_questions))
        self.assertTrue(any(cs.generic_component_verification(s) for s in result.good_signs))

    def test_pcb_missing_verification_keeps_one_question(self):
        result = finalize(source=PCB_SOURCE.replace(VERIFICATION, ''))
        self.assertIn('How will operation be verified after the repair?', result.contractor_questions)
        self.assertFalse(result.good_signs)
        self.assert_boundary(result)

    def test_independent_component_keeps_its_diagnostic_question(self):
        raw = pcb_response()
        evidence = 'The thermistor reading was intermittent during operation.'
        raw.technical_assessments.append(assessment('INCOMPLETE', 'PARTIALLY_DEFINED',
            subject='Thermistor diagnosis', evidence=[evidence], gaps=['Thermistor wiring isolation is missing.']))
        raw.contractor_questions.insert(0, 'What testing isolates the thermistor from its wiring?')
        result = finalize(raw, PCB_SOURCE + '\nReplace the indoor thermistor.\n' + evidence)
        self.assertTrue(any('thermistor' in q.lower() for q in result.contractor_questions))
        self.assertTrue(any('drive' in q.lower() for q in result.contractor_questions))
        self.assertTrue(any(evidence in a.documented_evidence for a in result.technical_assessments))

    def test_independent_specific_concern_is_not_deduplicated(self):
        raw = pcb_response()
        concern = 'Arcing was observed at the inverter drive connector.'
        question = 'What explains the arcing at the inverter drive connector?'
        raw.red_flags.append(concern)
        raw.contractor_questions.insert(0, question)
        result = finalize(raw, PCB_SOURCE + '\n' + concern)
        self.assertIn(concern, result.red_flags)
        self.assertIn(question, result.contractor_questions)
        self.assertTrue(any('isolates' in q for q in result.contractor_questions))

    def test_actual_documented_test_conflict_survives(self):
        for status in ('CONTRADICTORY', 'ABSENT'):
            with self.subTest(status=status):
                raw = pcb_response()
                conflict = 'The inverter drive PCB passed the supplied output test.'
                raw.technical_assessments[0] = assessment(status, 'UNSUPPORTED',
                    subject='Inverter drive PCB diagnosis', evidence=[conflict], contradictions=[conflict])
                raw.red_flags.append('The drive PCB passed the test, which conflicts with the replacement recommendation.')
                result = finalize(raw, PCB_SOURCE + '\n' + conflict)
                self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))
                self.assertTrue(any('passed the test' in flag for flag in result.red_flags))

    def test_actual_repair_verification_conflict_survives(self):
        raw = pcb_response()
        conflict = 'Required safety verification failed after inverter drive board repair.'
        raw.technical_assessments.append(assessment('CONTRADICTORY', 'UNSUPPORTED',
            subject=cs.COMMISSIONING_SUBJECT, evidence=[conflict], contradictions=[conflict]))
        result = finalize(raw, PCB_SOURCE + '\n' + conflict)
        self.assertTrue(any(conflict in a.contradictions for a in result.technical_assessments))
        self.assertTrue(cs.commissioning_items(result))

    def test_unsupported_assertions_are_attributed_in_evidence_sections(self):
        for raw, source in ((pcb_response(), PCB_SOURCE), (compressor_response(), COMPRESSOR_SOURCE)):
            with self.subTest(subject=raw.technical_assessments[0].subject):
                item = raw.technical_assessments[0]
                bare_claim = 'Inverter drive PCB has failed' if 'PCB' in item.subject else 'The compressor has failed'
                item.documented_evidence = [bare_claim]
                result = finalize(raw, source + '\nContractor states: ' + bare_claim + '.')
                paragraphs = [p for section in result.repair_evidence_sections for p in section.paragraphs]
                self.assertTrue(any('Contractor-reported statement: ' + bare_claim in p for p in paragraphs))
                self.assertNotIn(bare_claim, paragraphs)
                self.assertNotIn('seems reasonable given the diagnosis', result.installation_concerns)
                self.assertEqual(result.decision.technical_support, 'UNSUPPORTED')

    def test_meaningful_partial_and_confirmed_findings_are_preserved(self):
        for index, support in ((1, 'PARTIALLY_SUPPORTED'), (2, 'SUPPORTED')):
            with self.subTest(support=support):
                raw = evidence_raw('drive', index)
                result = finalize(raw, evidence_source('drive', index))
                self.assertEqual(result.decision.technical_support, support)
                self.assertEqual(result.technical_assessments[0].documented_evidence,
                                 raw.technical_assessments[0].documented_evidence)
                self.assertNotIn('Contractor-reported statement:', ' '.join(
                    p for section in result.repair_evidence_sections for p in section.paragraphs))

    def test_system_installation_with_component_work_keeps_commissioning(self):
        result = finalize(source=PCB_SOURCE + '\nInstall a new multi-zone mini-split.')
        self.assertTrue(cs.commissioning_items(result))
        self.assertTrue(main.sizing_assessments(result))

    def test_generic_diagnostic_question_with_independent_subject_survives(self):
        raw = pcb_response()
        evidence = 'The upstairs room remains warm while the downstairs room cools normally.'
        raw.technical_assessments.append(assessment('INCOMPLETE', 'PARTIALLY_DEFINED',
            subject='Uneven room cooling', evidence=[evidence], gaps=['The separate room-cooling issue is unresolved.']))
        result = finalize(raw, PCB_SOURCE + '\n' + evidence)
        self.assertIn(raw.contractor_questions[1], result.contractor_questions)

    def test_repeated_finalization_and_question_limit_for_both_reports(self):
        for raw, source in ((pcb_response(), PCB_SOURCE), (compressor_response(), COMPRESSOR_SOURCE)):
            with self.subTest(subject=raw.technical_assessments[0].subject):
                raw.contractor_questions *= 3
                result = finalize(raw, source)
                self.assert_one_diagnosis(result, raw)
                for _ in range(3):
                    again = finalize(main.HVACAnalysis.model_validate_json(result.model_dump_json()), source)
                    self.assertEqual(result.model_dump(), again.model_dump())
                    self.assertEqual(main.build_report_html(result, 1), main.build_report_html(again, 1))
                    self.assertLessEqual(len(again.contractor_questions), 6)
                    result = again


if __name__ == '__main__':
    unittest.main()
