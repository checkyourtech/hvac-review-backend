"""Fixed mini-split evidence matrix through the existing production finalizer."""
import contextlib
import html
import io
import unittest
import main
from test_technical_support_derivation import analysis_with, assessment
from test_mini_split_scope_routing import classification
from evidence_integrity import assessment_owner
from commissioning import commissioning_items
from test_repair_diagnostics import upload
CASES = {'compressor': ('compressor',
                'Claimed compressor failure',
                [['The inverter compressor does not start and a fault code is reported.'],
                 ['Inverter compressor input voltage was measured within the supplied equipment '
                  'range.',
                  'The inverter compressor drive output under command was not tested.'],
                 ['With the compressor leads disconnected from the inverter drive, an isolated '
                  'compressor winding test confirmed an open winding.']]),
 'drive': ('inverter drive board',
           'Inverter drive board diagnosis',
           [['The inverter drive board has a reported fault code; the unit does not start.'],
            ['Required input power at the inverter drive board was confirmed present.',
             'The inverter drive board output under the operating command was not measured.'],
            ['The inverter drive board received the required input power and operating command.',
             'Under the attached manufacturer isolation procedure, the inverter drive board failed '
             'its output test with the compressor and wiring excluded as the cause.']]),
 'thermistor': ('indoor thermistor',
                'Indoor thermistor diagnosis',
                [['The indoor thermistor has a reported temperature sensor fault code.'],
                 ['The indoor thermistor reading was intermittent during operation; the sensor and '
                  'its wiring have not been isolated.'],
                 ['The disconnected indoor thermistor measured open circuit; the supplied sensor '
                  'test procedure specifies finite resistance at the documented temperature.',
                  'Separate thermistor wiring and connector tests were normal.']]),
 'valve': ('EEV',
           'Metering-device / refrigerant-flow condition',
           [['The EEV is blamed for poor cooling; no component testing is documented.'],
            ['The EEV response to an operating command was intermittent during testing.',
             'EEV control signal and sensor inputs were not verified.'],
            ['The EEV failed to respond to the commanded movement during the documented '
             'manufacturer isolation test.',
             'The EEV control signal and sensor inputs tested normally; charge, airflow and other '
             'restrictions were excluded as causes by the documented checks.']]),
 'communication': ('communicating control module',
                   'Communicating control module diagnosis',
                   [['The communicating control module has a reported communication fault code.'],
                    ['The communication control wiring continuity test passed.',
                     'Communication control module dropouts were observed; the module and peer '
                     'controller have not been isolated.'],
                    ['The communicating control module received the required power and incoming '
                     'message.',
                     'The documented manufacturer communication-path isolation test showed that '
                     'the communicating control module did not transmit its required reply; its '
                     'wiring and peer controller tested normally.']])}

def source(key, index):
    (part, subject, levels) = CASES[key]
    return 'Existing ductless mini-split.\nReplace ' + part + ' only.\n' + '\n'.join(levels[index]) + '\nVerify operation after repair.\nTotal repair price: $850 lump sum, parts and labor included.'

def raw(key, index, optimistic=False):
    (part, subject, levels) = CASES[key]
    (status, scope) = [('ABSENT', 'UNSUPPORTED'), ('INCOMPLETE', 'PARTIALLY_DEFINED'), ('CONFIRMED', 'APPROPRIATE')][index]
    if optimistic:
        (status, scope) = ('CONFIRMED', 'APPROPRIATE')
    return analysis_with([assessment(status, scope, subject=subject, evidence=levels[index], gaps=['Component isolation remains undocumented.'] if index == 1 else [])], pricing='LIMITED')
SUPPORT = ['UNSUPPORTED', 'PARTIALLY_SUPPORTED', 'SUPPORTED']
OWNERS = {'compressor': 'compressor', 'drive': 'control board', 'thermistor': 'temperature sensor', 'valve': 'metering device', 'communication': 'communication control'}

def final(key, index, incoming=None, text=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return main.finalize_customer_analysis(incoming or raw(key, index), text or source(key, index), 1, classification(), fresh_analysis=True)

class MiniSplitComponentEvidenceTests(unittest.TestCase):

    def check_case(self, key, index):
        incoming = raw(key, index)
        before = incoming.model_dump()
        (result, report) = upload(incoming, source(key, index), classification())
        self.assertEqual(result.decision.technical_support, SUPPORT[index])
        self.assertEqual(result.decision.pricing_transparency, 'LIMITED')
        self.assertEqual({assessment_owner(a.subject) for a in result.technical_assessments}, {OWNERS[key]})
        self.assertFalse(commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))
        self.assertEqual(len(result.red_flags), 1 if index == 0 else 0)
        self.assertEqual(len(result.red_flags), len(set(result.red_flags)))
        self.assertEqual(len(result.good_signs), len(set(result.good_signs)))
        self.assertEqual(len(result.contractor_questions), 1 if index == 2 else 2)
        self.assertEqual(main.contractor_question_category(result.contractor_questions[-1]), 'pricing')
        self.assertLessEqual(len(result.contractor_questions), 6)
        self.assertTrue(result.repair_evidence_sections)
        for q in result.contractor_questions:
            self.assertIn(q, html.unescape(report))
        self.assertNotIn('thermostat call', report.lower())
        self.assertNotIn('24 volt', report.lower())
        self.assertNotIn('How Will Startup Be Verified?', report)
        self.assertEqual(incoming.model_dump(), before)
        self.assertEqual(result.model_dump(), final(key, index, result).model_dump())
        self.assertEqual(report, main.build_report_html(result, 1))

    def test_compressor_bad(self):
        self.check_case('compressor', 0)

    def test_compressor_partial(self):
        self.check_case('compressor', 1)

    def test_compressor_good(self):
        self.check_case('compressor', 2)

    def test_drive_bad(self):
        self.check_case('drive', 0)

    def test_drive_partial(self):
        self.check_case('drive', 1)

    def test_drive_good(self):
        self.check_case('drive', 2)

    def test_thermistor_bad(self):
        self.check_case('thermistor', 0)

    def test_thermistor_partial(self):
        self.check_case('thermistor', 1)

    def test_thermistor_good(self):
        self.check_case('thermistor', 2)

    def test_valve_bad(self):
        self.check_case('valve', 0)

    def test_valve_partial(self):
        self.check_case('valve', 1)

    def test_valve_good(self):
        self.check_case('valve', 2)

    def test_communication_bad(self):
        self.check_case('communication', 0)

    def test_communication_partial(self):
        self.check_case('communication', 1)

    def test_communication_good(self):
        self.check_case('communication', 2)

    def test_faults_and_symptoms_cannot_validate_optimistic_model_claims(self):
        for key in CASES:
            with self.subTest(key=key):
                result = final(key, 0, raw(key, 0, True))
                self.assertEqual(result.decision.technical_support, 'UNSUPPORTED')
                self.assertEqual(result.technical_assessments[0].diagnostic_evidence_status, 'ABSENT')

    def test_promised_tests_and_test_names_do_not_establish_failure(self):
        for (key, (part, subject, _)) in CASES.items():
            for evidence in [f'{part} diagnostic test.', f'The contractor will test the {part} using the manufacturer procedure.']:
                with self.subTest(key=key, evidence=evidence):
                    incoming = raw(key, 0, True)
                    incoming.technical_assessments[0].documented_evidence = [evidence]
                    text = f'Existing ductless mini-split.\nReplace {part} only.\n{evidence}\nVerify operation after repair.'
                    self.assertEqual(final(key, 0, incoming, text).decision.technical_support, 'UNSUPPORTED')

    def test_model_only_numeric_results_are_rejected(self):
        incoming = raw('thermistor', 2)
        incoming.technical_assessments[0].documented_evidence = ['Indoor thermistor resistance measured 1234 ohms against a 900 ohm limit.']
        result = final('thermistor', 2, incoming, source('thermistor', 0))
        self.assertNotEqual(result.decision.technical_support, 'SUPPORTED')
        self.assertNotIn('1234', result.model_dump_json())

    def test_supported_repair_does_not_clear_an_independent_material_gap(self):
        incoming = raw('valve', 2)
        incoming.technical_assessments.append(assessment('INCOMPLETE', 'PARTIALLY_DEFINED', subject='Thermistor diagnosis', materiality='MATERIAL_SECONDARY', evidence=['The thermistor reading was intermittent.'], gaps=['The thermistor and wiring are not isolated.']))
        result = final('valve', 2, incoming, source('valve', 2) + '\nReplace indoor thermistor.\nThe thermistor reading was intermittent.')
        self.assertEqual(result.decision.technical_support, 'PARTIALLY_SUPPORTED')
        self.assertTrue(any((a.scope_support == 'APPROPRIATE' and assessment_owner(a.subject) == 'metering device' for a in result.technical_assessments)))

    def test_lev_uses_the_same_evidence_boundary(self):
        for i in range(3):
            with self.subTest(level=i):
                incoming = main.HVACAnalysis.model_validate_json(raw('valve', i).model_dump_json().replace('EEV', 'LEV'))
                result = final('valve', i, incoming, source('valve', i).replace('EEV', 'LEV'))
                self.assertEqual(result.decision.technical_support, SUPPORT[i])
                self.assertFalse(commissioning_items(result))

    def test_generic_board_label_uses_inverter_guidance(self):
        incoming = raw('drive', 1)
        incoming.technical_assessments[0].subject = 'Electrical control evidence: control board'
        result = final('drive', 1, incoming)
        report = main.build_report_html(result, 1).lower()
        self.assertNotIn('thermostat call', report)
        self.assertIn('inverter', report)
        self.assertIn('manufacturer', report)

    def test_asserted_component_failure_without_findings_is_not_confirmation(self):
        for (key, (part, _, _)) in CASES.items():
            with self.subTest(component=key):
                evidence = f'The {part} is faulty.'
                incoming = raw(key, 0, True)
                incoming.technical_assessments[0].documented_evidence = [evidence]
                text = source(key, 0) + '\n' + evidence
                self.assertEqual(final(key, 0, incoming, text).decision.technical_support, 'UNSUPPORTED')

    def test_unfamiliar_source_backed_inspection_is_not_absent_evidence(self):
        evidence = 'The indoor thermistor ceramic body is visibly fractured under magnification.'
        for (status, scope, support) in [('INCOMPLETE', 'PARTIALLY_DEFINED', 'PARTIALLY_SUPPORTED'), ('CONFIRMED', 'APPROPRIATE', 'SUPPORTED')]:
            with self.subTest(status=status):
                incoming = raw('thermistor', 2)
                item = incoming.technical_assessments[0]
                item.diagnostic_evidence_status = status
                item.scope_support = scope
                item.documented_evidence = [evidence]
                result = final('thermistor', 2, incoming, source('thermistor', 0) + '\n' + evidence)
                self.assertEqual(result.decision.technical_support, support)
                self.assertEqual(result.technical_assessments[0].documented_evidence, [evidence])

    def test_documented_conflict_remains_unsupported(self):
        for (key, (part, _, _)) in CASES.items():
            with self.subTest(component=key):
                evidence = f'The {part} passed the supplied component isolation test.'
                incoming = raw(key, 2)
                item = incoming.technical_assessments[0]
                item.diagnostic_evidence_status = 'CONTRADICTORY'
                item.scope_support = 'UNSUPPORTED'
                item.documented_evidence = [evidence]
                item.contradictions = [evidence]
                result = final(key, 2, incoming, source(key, 0) + '\n' + evidence)
                self.assertEqual(result.decision.technical_support, 'UNSUPPORTED')
                self.assertTrue(any((evidence in a.contradictions for a in result.technical_assessments)))

    def test_drive_findings_do_not_establish_independent_compressor_failure(self):
        incoming = raw('compressor', 0, True)
        incoming.technical_assessments[0].documented_evidence += CASES['drive'][2][2]
        incoming.technical_assessments += raw('drive', 2).technical_assessments
        text = source('compressor', 0) + '\n' + source('drive', 2)
        result = final('compressor', 0, incoming, text)
        compressor = next((a for a in result.technical_assessments if assessment_owner(a.subject) == 'compressor'))
        drive = next((a for a in result.technical_assessments if assessment_owner(a.subject) == 'control board'))
        self.assertNotEqual(compressor.scope_support, 'APPROPRIATE')
        self.assertEqual(drive.scope_support, 'APPROPRIATE')
        self.assertNotEqual(result.decision.technical_support, 'SUPPORTED')
        self.assertFalse(commissioning_items(result))
        self.assertFalse(main.sizing_assessments(result))

    def test_no_universal_conventional_inverter_compressor_checklist(self):
        result = final('compressor', 0)
        guidance = ' '.join((p for s in result.repair_evidence_sections for p in s.paragraphs)).lower()
        self.assertIn('manufacturer', guidance)
        self.assertIn('inverter', guidance)
        self.assertNotIn('capacitor', guidance)
if __name__ == '__main__':
    unittest.main()
