"""Offline readiness, source-fact and verdict-neutrality regressions."""
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from pricing import (
    MarketPriceContext, MarketPriceStatus, extract_quote_price_facts,
    evaluate_market_price, customer_pricing_text,
)
from test_system_sizing import analysis, domain_items


class PricingArchitectureTests(unittest.TestCase):
    def test_old_payload_and_safe_defaults(self):
        payload = analysis().model_dump()
        payload.pop('price_facts')
        payload.pop('market_price_context')
        parsed = main.HVACAnalysis.model_validate(payload)
        self.assertEqual(parsed.price_facts, [])
        self.assertEqual(parsed.market_price_context.status, MarketPriceStatus.NOT_EVALUATED)
        self.assertIsNone(parsed.market_price_context.expected_low)
        self.assertEqual(len(MarketPriceStatus), 5)
        self.assertIsNone(main.AnalyzeRequest(files=[]).project_zip)
        self.assertEqual(main.AnalyzeRequest(files=[], project_zip='01234').project_zip, '01234')

    def test_market_payload_is_untrusted_and_verdict_neutral(self):
        for transparency in ('ADEQUATE', 'LIMITED', 'ABSENT', 'NOT_APPLICABLE'):
            with self.subTest(transparency=transparency):
                raw = analysis(pricing=transparency)
                baseline = main.finalize_customer_analysis(raw)
                raw.market_price_context = MarketPriceContext(
                    status='above_expected', expected_low=100, expected_high=500,
                    data_source='Model memory', confidence='high',
                )
                final = main.finalize_customer_analysis(raw)
                self.assertEqual(final.decision, baseline.decision)
                self.assertEqual(final.market_price_context, MarketPriceContext())
                self.assertEqual(final.contractor_questions, baseline.contractor_questions)

    def test_explicit_prices_stay_separate_and_keep_provenance(self):
        text = '''Total replacement price: $18,000
Repair option: $6,900
Equipment: $11,000
Labor and installation materials: $7,000
Warranty: $300
Optional air cleaner: $400
Refrigerant: $75 per pound
Permit fees: $120
Rebate: $500 subject to approval
Labor: $600
Installation materials: $200'''
        facts = extract_quote_price_facts(text)
        amounts = {item.category: item.amount for item in facts[0].items}
        self.assertEqual(amounts['replacement_total'], 18000)
        self.assertEqual(amounts['repair_option_total'], 6900)
        self.assertEqual(amounts['equipment'], 11000)
        self.assertEqual(amounts['labor_installation_materials'], 7000)
        self.assertEqual(amounts['accessory_addon'], 400)
        self.assertEqual(amounts['labor'], 600)
        self.assertEqual(amounts['installation_materials'], 200)
        self.assertEqual(len(amounts), 11)
        self.assertIn('per pound', next(i.source_text for i in facts[0].items if i.category == 'refrigerant'))
        self.assertEqual(extract_quote_price_facts('No price given'), [])

    def test_multiple_quotes_are_not_merged(self):
        facts = extract_quote_price_facts('QUOTE 1\nTotal: $500\nQUOTE 2\nTotal: $800')
        self.assertEqual([x.quote_index for x in facts], [1, 2])
        self.assertEqual([x.items[0].amount for x in facts], [500, 800])

    def test_adequate_split_voice_and_no_finer_itemization(self):
        quote = Path('replacement_basis_elective_test.txt').read_text()
        quote = quote.replace('18,200', '18,000').replace('7,200', '7,000')
        final = main.finalize_customer_analysis(analysis(*domain_items(), pricing='LIMITED'), quote)
        self.assertEqual(final.decision.pricing_transparency, 'ADEQUATE')
        self.assertIn('$18,000', final.pricing_review)
        self.assertIn('$11,000', final.pricing_review)
        self.assertIn('$7,000', final.pricing_review)
        self.assertNotIn('high-level', main.build_report_html(final))
        self.assertFalse(any(main.contractor_question_category(q) == 'pricing' for q in final.contractor_questions))
        self.assertEqual(final.market_price_context.status, MarketPriceStatus.NOT_EVALUATED)

    def test_no_benchmarks_or_location_judgments_rendered(self):
        raw = analysis()
        raw.pricing_review = 'This is a fair market price. The expected range is $500 to $800. This price is above average in Reno. The quote lists $950 for the repair.'
        final = main.finalize_customer_analysis(raw, 'Repair total: $950')
        report = main.build_report_html(final)
        for forbidden in ('fair market', '$500', '$800', 'average in Reno'):
            self.assertNotIn(forbidden, report)
        self.assertIn('$950', final.pricing_review)
        self.assertEqual(final.price_facts[0].items[0].amount, 950)
        self.assertEqual(evaluate_market_price(final.price_facts, project_zip='01234').project_zip, '01234')
        self.assertIsNone(evaluate_market_price(final.price_facts).expected_high)

    def test_lump_sum_policy_and_voice(self):
        final = main.finalize_customer_analysis(analysis(pricing='LIMITED'), 'Repair total: $950')
        self.assertEqual(final.decision.pricing_transparency, 'LIMITED')
        self.assertEqual(len(final.contractor_questions), 1)
        text = customer_pricing_text('The pricing components show an allocation of costs. Pricing transparency is adequate.')
        self.assertNotIn('pricing components', text.lower())
        self.assertNotIn('allocation of costs', text.lower())
        self.assertNotIn('transparency is adequate', text.lower())

    def test_request_location_and_source_facts_reach_api_and_email_object(self):
        raw = analysis(pricing='LIMITED')
        raw.price_facts = extract_quote_price_facts('Total: $99999')
        before = raw.model_dump()
        classification = main.QuoteClassification(
            quote_type='repair', system_type='air conditioner', primary_scope='Capacitor repair',
            modules_required=[main.AnalysisModule.ELECTRICAL_CONTROLS],
        )
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=raw))])
        request = main.AnalyzeRequest(
            files=[main.UploadedQuote(fileName='repair.txt', extractedText='Repair total: $725')],
            project_zip='01234', city='Example City', state='MA',
        )
        with patch.object(main, 'classify_quotes', return_value=classification), \
             patch.object(main.client.beta.chat.completions, 'parse', return_value=response), \
             patch.object(main, 'send_review_email') as email:
            final = asyncio.run(main.analyze_hvac_quote(request))
        self.assertIs(email.call_args.kwargs['analysis'], final)
        self.assertEqual(final.market_price_context.project_zip, '01234')
        self.assertEqual(final.market_price_context.market_area, 'Example City, MA')
        self.assertEqual(final.market_price_context.status, MarketPriceStatus.NOT_EVALUATED)
        self.assertEqual(final.price_facts[0].items[0].amount, 725)
        self.assertEqual(raw.model_dump(), before)
        self.assertNotIn('Market comparison unavailable', main.build_report_html(final))

    def test_quote_fairness_claim_without_the_word_price_is_removed(self):
        self.assertEqual(customer_pricing_text('This quote is expensive for this area.'), '')


if __name__ == '__main__':
    unittest.main()
