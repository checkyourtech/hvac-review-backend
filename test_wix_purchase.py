"""Offline Wix contract/security tests. Synthetic orders, temporary storage only."""
import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, PropertyMock, patch

import httpx
from fastapi.testclient import TestClient

from deployment_app import create_app
from deployment_ops import provision
from deployment_storage import DeploymentConfig, DeploymentError, open_storage
from purchase_security import EntitlementService, InvalidPurchaseProof, SQLitePurchaseLedger
from wix_purchase import (SESSION_SECONDS, ORDER_URL, WixConfig, WixOrdersClient,
                          WixPurchaseVerifier, WixUnavailable)


def guid(number):
    return '00000000-0000-4000-8000-' + str(number).zfill(12)


def config():
    return WixConfig(guid(1), guid(2), guid(3), 'https://www.checkyourtech.info',
                     'synthetic-wix-api-key', 'a1' * 32, 'b2' * 32)


def date(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def order(**changes):
    return dict(id=guid(10), planId=guid(2), buyer={'memberId': guid(20)},
        type='ONLINE', status='ACTIVE', lastPaymentStatus='PAID',
        startDate=date(time.time() - 60), endDate=date(time.time() + 86400),
        pricing={'singlePaymentForDuration': {'count': 1, 'unit': 'MONTH'}}, **changes)


class WixVerificationTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.order = order()
        self.client = Mock()
        self.client.get_order.side_effect = lambda _: copy.deepcopy(self.order)
        self.verifier = WixPurchaseVerifier(self.config, client=self.client)

    def token(self):
        return self.verifier.issue(guid(10), guid(20))['authorization'].split(' ', 1)[1]

    def verify(self, token=None, purchase=None):
        return asyncio.run(self.verifier.verify(purchase or guid(10), token or self.token()))

    def test_tier1_authority_not_names_email_or_price(self):
        self.order.update(planName='Full Protection Review', customerEmail='wrong@example.invalid', planPrice='9999')
        purchase = self.verify()
        self.assertEqual((purchase.tier, purchase.submission_limit, purchase.payment_eligible), ('tier1', 1, True))
        self.assertEqual(purchase.authenticated_customer_id, guid(20))

    def test_tier2_grants_one_review_not_three_consumptions(self):
        self.order['planId'] = guid(3)
        self.assertEqual((self.verify().tier, self.verify().submission_limit), ('tier2', 1))

    def test_unknown_or_tier3_plan_always_rejected(self):
        self.order.update(planId=guid(4), planName='In-Depth Quote Review')
        with self.assertRaises(InvalidPurchaseProof): self.token()

    def test_wrong_member_cannot_receive_proof(self):
        with self.assertRaises(InvalidPurchaseProof): self.verifier.issue(guid(10), guid(21))

    def test_expired_or_future_order_cannot_receive_proof(self):
        for changes in ({'endDate': date(time.time() - 1)}, {'startDate': date(time.time() + 60)}):
            self.order = order(); self.order.update(changes)
            with self.subTest(changes=changes), self.assertRaises(InvalidPurchaseProof): self.token()

    def test_only_active_online_paid_orders_eligible(self):
        for field, values in [('status', ['DRAFT', 'PENDING', 'PAUSED', 'ENDED', 'CANCELED', 'UNKNOWN']),
                              ('type', ['OFFLINE', 'EXTERNAL', 'UNKNOWN']),
                              ('lastPaymentStatus', ['UNPAID', 'FAILED', 'PENDING', 'REFUNDED', 'NOT_APPLICABLE', 'UNKNOWN'])]:
            for value in values:
                self.order = order(); self.order[field] = value
                with self.subTest(field=field, value=value):
                    self.assertFalse(self.verifier.revalidate(guid(10)).payment_eligible)
                    with self.assertRaises(InvalidPurchaseProof): self.token()

    def test_cancel_request_blocks_even_if_status_active(self):
        for field, value in [('cancellation', {'effectiveAt': 'NEXT_PAYMENT_DATE'}), ('autoRenewCanceled', True)]:
            self.order = order(); self.order[field] = value
            self.assertFalse(self.verifier.revalidate(guid(10)).payment_eligible)

    def test_wrong_duration_recurring_unlimited_not_eligible(self):
        for pricing in ({}, {'singlePaymentUnlimited': True}, {'subscription': {}},
                        {'singlePaymentForDuration': {'count': 12, 'unit': 'MONTH'}},
                        {'singlePaymentForDuration': {'count': 1, 'unit': 'MONTH'}, 'subscription': {}}):
            self.order['pricing'] = pricing
            self.assertFalse(self.verifier.revalidate(guid(10)).payment_eligible)

    def test_missing_authoritative_fields_fail_closed(self):
        for field in ['buyer', 'planId', 'id', 'startDate', 'endDate', 'pricing', 'lastPaymentStatus', 'status', 'type']:
            self.order = order(); self.order.pop(field)
            with self.subTest(field=field), self.assertRaises(WixUnavailable): self.token()

    def test_malformed_dates_or_missing_timezone_fail_closed(self):
        for value in ['tomorrow', '2026-11-01', '2026-11-01T12:00:00', None, 123]:
            self.order['endDate'] = value
            with self.subTest(value=value), self.assertRaises(WixUnavailable): self.token()

    def test_order_id_response_must_match_request(self):
        self.order['id'] = guid(11)
        with self.assertRaises(InvalidPurchaseProof): self.token()

    def test_signed_proof_tampering_and_wrong_order_rejected_before_lookup(self):
        token = self.token(); self.client.reset_mock()
        for value in [token[:-1] + ('0' if token[-1] != '0' else '1'), 'invented', 'x' * 9000, 'e30.none']:
            with self.subTest(value=value[:8]), self.assertRaises(InvalidPurchaseProof): self.verify(value)
        with self.assertRaises(InvalidPurchaseProof): self.verify(token, guid(11))
        self.client.get_order.assert_not_called()

    def test_expired_proof_cannot_be_refreshed_by_client(self):
        token = self.token()
        with patch('wix_purchase.time.time', return_value=time.time() + SESSION_SECONDS + 1):
            with self.assertRaises(InvalidPurchaseProof): self.verify(token)

    def test_signing_key_or_site_rotation_invalidates_proofs(self):
        token = self.token()
        for changes in ({'session_key': 'c3' * 32}, {'site_id': guid(99)}):
            self.verifier = WixPurchaseVerifier(replace(self.config, **changes), client=self.client)
            with self.assertRaises(InvalidPurchaseProof): self.verify(token)

    def test_owner_change_rejected_on_every_access(self):
        token = self.token()
        self.order['buyer']['memberId'] = guid(21)
        with self.assertRaises(InvalidPurchaseProof): self.verify(token)

    def test_refund_rechecked_with_existing_token_and_by_worker(self):
        token = self.token()
        self.order['lastPaymentStatus'] = 'REFUNDED'
        self.assertFalse(self.verify(token).payment_eligible)
        self.assertFalse(self.verifier.revalidate(guid(10)).payment_eligible)
        self.assertEqual(self.client.get_order.call_count, 3)

    def test_refund_revokes_ledger_without_replenishing_usage(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = SQLitePurchaseLedger.initialize(Path(root).resolve() / 'ledger.sqlite3')
            service = EntitlementService(self.verifier, ledger)
            token = self.token()
            authorized = asyncio.run(service.authorize(guid(10), token))
            authorized.consume()
            self.order['lastPaymentStatus'] = 'REFUNDED'
            from fastapi import HTTPException
            with self.assertRaises(HTTPException): asyncio.run(service.authorize(guid(10), token, require_unused=False))
            with ledger._connect() as db:
                self.assertEqual(db.execute('SELECT used,eligible FROM purchases').fetchone(), (1, 0))

    def test_bad_configuration_does_not_expose_secrets(self):
        for changes in ({'tier1_plan_id': ''}, {'tier2_plan_id': guid(2)}, {'site_id': 'unknown'},
                        {'parent_origin': 'http://www.checkyourtech.info'}, {'parent_origin': 'https://*.example.com'},
                        {'parent_origin': 'https://example.com/path'}, {'parent_origin': 'https://user@example.com'},
                        {'bridge_key': 'short'}, {'session_key': self.config.bridge_key}):
            with self.subTest(fields=list(changes)), self.assertRaises(WixUnavailable) as caught: replace(self.config, **changes)
            self.assertNotIn(self.config.api_key, str(caught.exception))
        self.assertNotIn(self.config.bridge_key, repr(self.config))
        self.assertNotIn(self.config.session_key, repr(self.config))


class WixTransportTests(unittest.TestCase):
    def lookup(self, response):
        self.requests = []
        def handler(request):
            self.requests.append(request)
            if isinstance(response, Exception): raise response
            return response
        self.client = WixOrdersClient(config(), transport=httpx.MockTransport(handler))
        return self.client.get_order(guid(10))

    def test_documented_endpoint_site_headers_and_basic_fieldset(self):
        self.assertEqual(self.lookup(httpx.Response(200, json={'order': order()}))['id'], guid(10))
        request = self.requests[0]
        self.assertEqual(str(request.url), ORDER_URL + guid(10) + '?fieldSet=BASIC')
        self.assertEqual(request.headers['authorization'], config().api_key)
        self.assertEqual(request.headers['wix-site-id'], guid(1))
        self.assertNotIn('wix-account-id', request.headers)
        self.assertLessEqual(request.extensions['timeout']['connect'], 3)

    def test_auth_billing_ratelimit_and_server_errors_sanitized_no_retries(self):
        for status in [400, 401, 402, 403, 429, 500, 502]:
            with self.subTest(status=status), self.assertRaises(WixUnavailable) as caught:
                self.lookup(httpx.Response(status, text='private provider body'))
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(len(self.requests), 1)

    def test_no_redirect_following_or_credential_forwarding(self):
        with self.assertRaises(WixUnavailable): self.lookup(httpx.Response(302, headers={'Location': 'https://untrusted.invalid'}))
        self.assertEqual(len(self.requests), 1)

    def test_unknown_order_invalid_proof(self):
        with self.assertRaises(InvalidPurchaseProof): self.lookup(httpx.Response(404))

    def test_timeout_sanitized(self):
        with self.assertRaises(WixUnavailable) as caught: self.lookup(httpx.ReadTimeout('secret request'))
        self.assertNotIn('secret', str(caught.exception))

    def test_malformed_oversized_response_closed(self):
        for response in [httpx.Response(200, text='not-json'), httpx.Response(200, json={'order': []}),
                         httpx.Response(200, text=' ' * 65537)]:
            with self.assertRaises(WixUnavailable): self.lookup(response)

    def test_path_traversal_never_requests(self):
        client = WixOrdersClient(config(), transport=httpx.MockTransport(lambda _: self.fail('Unexpected request')))
        for value in ['../orders', 'https://other.invalid', guid(10) + '?x=y']:
            with self.assertRaises(InvalidPurchaseProof): client.get_order(value)

    def test_transport_concurrency_is_bounded(self):
        client = WixOrdersClient(config(), transport=httpx.MockTransport(lambda _: self.fail('Unexpected request')))
        for _ in range(8): self.assertTrue(client.slots.acquire(blocking=False))
        with self.assertRaises(WixUnavailable): client.get_order(guid(10))


class WixIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='cyt-wix-test-')
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.mount = self.base / 'disk'; self.mount.mkdir(mode=0o700)
        self.deployment = DeploymentConfig(disk_mount=self.mount, data_dir=self.mount / 'cyt',
            max_bytes=16_000_000, reserve_bytes=1, max_bundles=20, require_mount=False)
        provision(self.deployment)
        self.ledger, _, _ = open_storage(self.deployment)
        self.orders = {guid(10): order()}
        self.requests = []
        def handler(request):
            self.requests.append(request)
            found = self.orders.get(request.url.path.rsplit('/', 1)[-1])
            return httpx.Response(200, json={'order': copy.deepcopy(found)}) if found else httpx.Response(404)
        self.verifier = WixPurchaseVerifier(config(), client=WixOrdersClient(config(), transport=httpx.MockTransport(handler)))
        self.calls = 0
        async def process(manifest, files, grant):
            self.calls += 1
            grant.consume()
            grant.store.complete(grant.job_id, grant.purchase, grant.attempt,
                '{"synthetic":true}', '<p>Private synthetic report</p>', manifest['customer_name'], len(manifest['quotes']))
        self.app = create_app(replace(self.deployment, queue_enabled=True), verifier=self.verifier, processor=process)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def handoff(self, purchase=guid(10), member=guid(20), **kwargs):
        return self.client.post('/wix/handoff', headers={'X-CYT-Wix-Key': config().bridge_key, **kwargs.pop('headers', {})},
            json={'purchaseId': purchase, 'memberId': member, **kwargs})

    def headers(self, purchase=guid(10), member=guid(20)):
        result = self.handoff(purchase, member)
        self.assertEqual(result.status_code, 200, result.text)
        return {'Authorization': result.json()['authorization'], 'X-Purchase-ID': purchase,
                'Idempotency-Key': 'synthetic-wix-submission'}

    def upload(self, headers, count=1):
        return self.client.post('/upload', headers=headers, data={'customer_name': 'Synthetic',
            'customer_email': 'synthetic@example.invalid', 'quote_groups': json.dumps([
                {'pages': [{'file': i, 'rotation': 0} for i in range(count)]}])},
            files=[('files', (f'page-{i}.txt', b'Synthetic proposal: replace part and verify operation, $300.', 'text/plain')) for i in range(count)])

    def wait(self, job, headers):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.client.get('/jobs/' + job, headers=headers)
            if result.json().get('state') == 'completed': return result
            time.sleep(0.02)
        self.fail('Synthetic job did not complete within its bounded wait')

    def test_server_only_handoff_rejects_guessed_keys_browser_origin_and_extra_claims(self):
        for headers in ({'X-CYT-Wix-Key': ''}, {'X-CYT-Wix-Key': 'f0' * 32}, {'Origin': config().parent_origin}):
            self.assertEqual(self.handoff(headers=headers).status_code, 403)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.handoff(package='tier2').status_code, 400)
        self.assertEqual(self.handoff(member=guid(21)).status_code, 403)

    def test_no_session_when_runtime_not_ready(self):
        with patch.object(type(self.app.state.durable_queue), 'ready', new_callable=PropertyMock) as ready:
            ready.return_value = False
            self.assertEqual(self.handoff().status_code, 503)
        self.assertEqual(self.requests, [])

    def test_handoff_bounded_sanitized_and_not_cached(self):
        response = self.handoff()
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn(config().bridge_key, response.text)
        self.assertNotIn(config().api_key, response.text)
        self.assertNotIn('set-cookie', response.headers)
        response = self.client.post('/wix/handoff', headers={'X-CYT-Wix-Key': config().bridge_key}, content=b'x' * 2049)
        self.assertEqual(response.status_code, 413)
        self.assertNotIn('xxx', response.text)

    def test_bootstrap_has_no_credentials_and_upload_page_remains_protected(self):
        response = self.client.get('/wix/connect')
        self.assertEqual(response.status_code, 200)
        self.assertIn('frame-ancestors https://www.checkyourtech.info', response.headers['content-security-policy'])
        self.assertNotIn(config().bridge_key, response.text)
        self.assertEqual(self.client.get('/upload-page?package=basic').status_code, 401)
        headers = self.headers()
        self.assertEqual(self.client.get('/upload-page', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/upload-page?package=standard', headers=headers).status_code, 403)

    def test_multipage_direct_upload_202_dedup_report_and_reconnection(self):
        headers = self.headers()
        response = self.upload(headers, count=2)
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()['job_id']
        self.assertEqual(response.headers['location'], '/jobs/' + job)
        self.wait(job, headers)
        second = self.upload(headers, count=2)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()['job_id'], job)
        # Fresh member handoff after browser close preserves consumed-purchase reports.
        new_headers = self.headers()
        self.assertEqual(self.client.get('/jobs', headers=new_headers).json()['jobs'][0]['job_id'], job)
        self.assertEqual(self.client.get('/jobs/' + job + '/report', headers=new_headers).text, '<p>Private synthetic report</p>')
        self.assertEqual(self.client.get('/jobs/' + job + '/result', headers=new_headers).json(), {'synthetic': True})
        self.assertEqual(self.calls, 1)
        with self.ledger._connect() as db:
            self.assertEqual(db.execute('SELECT used,submission_limit FROM purchases').fetchone(), (1, 1))
            # No browser or server credentials in SQLite or durable source manifests.
        for path in self.deployment.data_dir.rglob('*'):
            if path.is_file() and path.suffix not in ('.lock',):
                content = path.read_bytes()
                for forbidden in (headers['Authorization'].encode(), config().bridge_key.encode(), config().session_key.encode()):
                    self.assertNotIn(forbidden, content)

    def test_other_customer_cannot_access_report(self):
        headers = self.headers(); response = self.upload(headers); job = response.json()['job_id']; self.wait(job, headers)
        self.orders[guid(11)] = {**order(), 'id': guid(11), 'buyer': {'memberId': guid(21)}}
        other = self.headers(guid(11), guid(21))
        for suffix in ['', '/report', '/result']:
            self.assertEqual(self.client.get('/jobs/' + job + suffix, headers=other).status_code, 404)

    def test_refund_blocks_report_access_and_background_revalidation(self):
        headers = self.headers(); job = self.upload(headers).json()['job_id']; self.wait(job, headers)
        self.orders[guid(10)]['lastPaymentStatus'] = 'REFUNDED'
        self.assertFalse(self.verifier.revalidate(guid(10)).payment_eligible)
        self.assertEqual(self.client.get('/jobs/' + job + '/report', headers=headers).status_code, 403)

    def test_refund_after_submission_before_worker_blocks_paid_work(self):
        headers = self.headers()
        runtime = self.app.state.durable_queue
        original = runtime._revalidate
        def refund_then_revalidate(claim):
            self.orders[guid(10)]['lastPaymentStatus'] = 'REFUNDED'
            return original(claim)
        with patch.object(runtime, '_revalidate', side_effect=refund_then_revalidate):
            response = self.upload(headers)
            self.assertEqual(response.status_code, 202)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self.ledger._connect() as db:
                    row = db.execute('SELECT state FROM review_jobs WHERE job_id=?', (response.json()['job_id'],)).fetchone()
                if row == ('failed',): break
                time.sleep(0.02)
            self.assertEqual(row, ('failed',))
        self.assertEqual(self.calls, 0)
        with self.ledger._connect() as db:
            self.assertEqual(db.execute('SELECT used,eligible FROM purchases').fetchone(), (0, 0))

    def test_tier_limits_enforced_after_wix_handoff(self):
        def submit(headers, count):
            return self.client.post('/analyze', headers=headers, json={'customerName': 'Synthetic',
                'customerEmail': 'synthetic@example.invalid', 'files': [
                    {'fileName': f'quote-{i}.txt', 'extractedText': 'Synthetic proposal, $300. Verify operation.'}
                    for i in range(count)]})
        headers = self.headers()
        self.assertEqual(submit(headers, 2).status_code, 400)
        # A separate Tier 2 purchase, not a mutable upgrade of an existing ledger row.
        self.orders[guid(11)] = {**order(), 'id': guid(11), 'planId': guid(3)}
        tier2 = self.headers(guid(11))
        self.assertEqual(submit(tier2, 4).status_code, 400)
        response = submit(tier2, 3)
        self.assertEqual(response.status_code, 202, response.text)
        self.wait(response.json()['job_id'], tier2)
        self.assertEqual(self.calls, 1)

    def test_tier3_active_wix_order_cannot_receive_entitlement(self):
        self.orders[guid(10)].update(planId=guid(4), planName='Full Protection Review')
        self.assertEqual(self.handoff().status_code, 403)
        with self.ledger._connect() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM purchases').fetchone()[0], 0)

    def test_old_wix_file_urls_rejected_without_processing(self):
        response = self.client.post('/analyze', headers=self.headers(), json={'customerName': 'Synthetic',
            'customerEmail': 'synthetic@example.invalid', 'files': [{'fileName': 'quote.pdf', 'downloadUrl': 'https://static.wixstatic.com/synthetic.pdf'}]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, 0)

    def test_closed_factory_does_not_load_wix_credentials_or_expose_handoff(self):
        with patch('deployment_app.WixConfig.from_env', side_effect=AssertionError('Must not load credentials')):
            app = create_app(self.deployment)
        with TestClient(app) as client:
            self.assertEqual(client.get('/wix/connect').status_code, 404)
            self.assertEqual(client.post('/wix/handoff', json={}).status_code, 404)
        self.assertFalse(hasattr(app.state, 'entitlements'))

    def test_missing_production_wix_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(DeploymentError):
            create_app(replace(self.deployment, queue_enabled=True))

    def test_factory_wires_concrete_verifier_only_with_complete_configuration(self):
        with patch('deployment_app.WixConfig.from_env', return_value=config()):
            app = create_app(replace(self.deployment, queue_enabled=True))
        self.assertIsInstance(app.state.entitlements.verifier, WixPurchaseVerifier)
        self.assertIn('/wix/handoff', [route.path for route in app.routes])


class WixJavascriptTests(unittest.TestCase):
    def test_offline_velo_and_browser_contracts(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for Wix/browser regressions')
        result = subprocess.run([node, '--test', 'test_wix_ui.js'], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
