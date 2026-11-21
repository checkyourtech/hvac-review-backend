"""Offline durable HTTP queue, process ownership and recovery regressions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import main
from durable_uploads import PrivateUploadStore, UploadSource
from purchase_security import AuthorizedPurchase, EntitlementService, InvalidPurchaseProof, SQLitePurchaseLedger, VerifiedPurchase
from queue_http import process_saved_submission
from queue_runtime import QueueRuntime, RevalidatedPurchase
from queue_store import ClaimedReportStore
from report_jobs import JobPurchase, SQLiteReportStore
from sqlite_migrations import migrate_stage2
from test_blower_support_normalization import BAD
from test_blower_live_response import saved_response
from test_purchase_security import classified


class OfflineVerifier:
    def __init__(self, purchases):
        self.purchases = purchases
        self.unavailable = False
        self.revalidations = []

    async def verify(self, purchase_id, bearer_token):
        if bearer_token != 'proof-' + purchase_id or purchase_id not in self.purchases:
            raise InvalidPurchaseProof()
        return self.purchases[purchase_id]

    def revalidate(self, purchase_id):
        self.revalidations.append(purchase_id)
        if self.unavailable:
            raise RuntimeError('Synthetic unavailable provider')
        return RevalidatedPurchase(**self.purchases[purchase_id].model_dump(exclude={'authenticated_customer_id'}))


class QueueStage2Tests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='cyt-stage2-test-'))).resolve()
        self.ledger = SQLitePurchaseLedger.initialize(self.base / 'ledger.sqlite3')
        migrate_stage2(self.ledger)
        self.storage = PrivateUploadStore.provision(self.base / 'inputs', self.ledger, max_bytes=128 * 1024 * 1024, reserve_bytes=0)
        self.purchases = {}
        self.verifier = OfflineVerifier(self.purchases)
        self.app = FastAPI()
        self.app.include_router(main.app.router)
        self.app.state.entitlements = EntitlementService(self.verifier, self.ledger)
        self.app.state.queue_enabled = True
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.gate = threading.Event()
        self.gate.set()
        self.gates = [self.gate]
        self.counter = threading.Lock()
        self.active, self.peak = 0, 0
        self.started = []
        self.runtimes = []
        self.runtime = self.new_runtime()
        self.app.state.durable_queue = self.runtime
        self.addCleanup(self.shutdown)
        self.stack.enter_context(patch.object(main, 'send_review_email', return_value=True))
        self.stack.enter_context(patch.object(main, 'classify_quotes', return_value=classified()))
        self.stack.enter_context(patch.object(main, 'request_structured_analysis', side_effect=lambda **kwargs: saved_response()))
        # Reused synthetic purchase IDs must not reuse another test's cached OCR.
        cache = main.TranscriptionCache()
        self.stack.enter_context(patch.object(main, 'image_text_cache', cache))
        self.stack.callback(cache.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def new_runtime(self, **kwargs):
        runtime = QueueRuntime(self.storage, kwargs.pop('verifier', self.verifier), kwargs.pop('processor', self.process),
            deployment_confirmed=kwargs.pop('deployment_confirmed', True), poll_seconds=0.01,
            processing_seconds=kwargs.pop('processing_seconds', 30), wait_seconds=kwargs.pop('wait_seconds', 60),
            retry_seconds=kwargs.pop('retry_seconds', 0.05), **kwargs)
        self.runtimes.append(runtime)
        return runtime

    def shutdown(self):
        for gate in self.gates:
            gate.set()
        for runtime in reversed(self.runtimes):
            self.assertTrue(runtime.stop(timeout=5), 'Synthetic worker did not stop')

    async def process(self, manifest, files, grant):
        with self.counter:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started.append(grant.purchase.purchase_id)
        try:
            if not self.gate.wait(8):
                raise RuntimeError('Synthetic test gate timed out')
            grant.consume()
            grant.store.complete(grant.job_id, grant.purchase, grant.attempt,
                '{"synthetic":true}', '<p>Synthetic report</p>', manifest['customer_name'], len(manifest['quotes']))
        finally:
            with self.counter:
                self.active -= 1

    def customer(self, index=0, **changes):
        purchase_id = f'synthetic-order-{index}'
        if purchase_id not in self.purchases:
            self.purchases[purchase_id] = VerifiedPurchase(purchase_id=purchase_id,
                owner_customer_id=f'synthetic-owner-{index}', authenticated_customer_id=f'synthetic-owner-{index}',
                tier='tier1', payment_eligible=True, expires_at=int(time.time()) + 3600, submission_limit=1)
        if changes:
            self.purchases[purchase_id] = self.purchases[purchase_id].model_copy(update=changes)
        return self.purchases[purchase_id]

    def headers(self, index=0, **changes):
        purchase = self.customer(index)
        return {'Authorization': 'Bearer proof-' + purchase.purchase_id, 'X-Purchase-ID': purchase.purchase_id,
                'Idempotency-Key': f'synthetic-queue-key-{index}', **changes}

    def payload(self, **changes):
        return {'customerName': 'Synthetic', 'customerEmail': 'synthetic@example.invalid',
                'files': [{'fileName': 'original-proposal.pdf', 'extractedText': BAD}], **changes}

    def post(self, index=0, **changes):
        return self.client.post('/analyze', headers=self.headers(index), json=self.payload(**changes))

    def sql(self, statement, args=()):
        with self.ledger._connect() as db:
            return db.execute(statement, args).fetchall()

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail('Bounded wait expired')

    def status(self, job_id, index=0):
        return self.client.get('/jobs/' + job_id, headers=self.headers(index))

    def internal_save(self, index=0):
        purchase = self.customer(index)
        self.ledger.register(purchase)
        return self.storage.save(AuthorizedPurchase(purchase, self.ledger), f'internal-queue-key-{index}',
            [UploadSource('synthetic.txt', 'text/plain', io.BytesIO(BAD.encode()))],
            [{'pages': [{'file': 0, 'rotation': 0}]}], customer_name='Synthetic', customer_email='synthetic@example.invalid')

    def test_twenty_customers_all_accepted_two_workers_and_automatic_drain(self):
        self.gate.clear()
        for index in range(20):
            self.customer(index)
        self.runtime.start()
        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(self.post, range(20)))
        self.assertEqual([response.status_code for response in responses], [202] * 20)
        ids = [response.json()['job_id'] for response in responses]
        self.assertEqual(len(set(ids)), 20)
        self.wait(lambda: self.active == 2)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM review_queue WHERE phase='ready'")[0][0], 18)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_inputs')[0][0], 20)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM review_holds WHERE state='active'")[0][0], 20)
        self.assertEqual(self.sql('SELECT SUM(used) FROM purchases')[0][0], 0)
        self.gate.set()
        self.wait(lambda: self.sql("SELECT COUNT(*) FROM review_jobs WHERE state='completed'")[0][0] == 20)
        self.assertEqual(self.peak, 2)
        self.assertEqual(len(self.started), 20)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 20)
        self.assertEqual(self.sql('SELECT SUM(used) FROM purchases')[0][0], 20)
        for index, job_id in enumerate(ids):
            self.assertEqual(self.status(job_id, index).json()['state'], 'completed')

    def test_lifespan_starts_and_stops_exactly_three_managed_threads(self):
        with TestClient(self.app) as client:
            self.assertTrue(self.runtime.ready)
            self.assertEqual(len(self.runtime._threads), 3)
            response = client.post('/analyze', headers=self.headers(), json=self.payload())
            self.assertEqual(response.status_code, 202)
            self.wait(lambda: self.sql("SELECT COUNT(*) FROM review_jobs WHERE state='completed'")[0][0] == 1)
        self.assertTrue(all(not thread.is_alive() for thread in self.runtime._threads))
        self.assertIsNone(self.runtime._lock_fd)

    def test_sqlite_discovery_after_restart_without_wake_signal(self):
        first = self.internal_save(0)
        second = self.internal_save(1)
        reopened = PrivateUploadStore(self.storage.root, self.ledger, max_bytes=2**27, reserve_bytes=0)
        runtime = QueueRuntime(reopened, self.verifier, self.process, deployment_confirmed=True, poll_seconds=0.01)
        self.runtimes.append(runtime)
        runtime.start()
        self.wait(lambda: self.sql("SELECT COUNT(*) FROM review_jobs WHERE state='completed'")[0][0] == 2)
        self.assertEqual(set(self.started), {'synthetic-order-0', 'synthetic-order-1'})
        self.assertNotEqual(first['job_id'], second['job_id'])

    def test_oldest_ready_jobs_are_claimed_first(self):
        ids = [self.internal_save(i)['job_id'] for i in range(3)]
        self.runtime.records.recover('offline-owner')
        first = self.runtime.records.claim_next('offline-owner')
        second = self.runtime.records.claim_next('offline-owner')
        self.assertEqual([first['job_id'], second['job_id']], ids[:2])
        self.assertIsNone(self.runtime.records.claim_next('offline-owner'))

    def test_competing_runtime_cannot_start_or_exceed_two_workers(self):
        self.runtime.start()
        contender = self.new_runtime()
        with self.assertRaises(HTTPException):
            contender.start()
        self.assertTrue(self.runtime.ready)
        self.assertFalse(contender.ready)
        self.assertEqual(len([t for t in self.runtime._threads if t.is_alive()]), 3)

    def test_expired_worker_still_occupies_slot_and_late_publication_is_rejected(self):
        self.gate.clear()
        self.runtime.records.processing_seconds = 0.1
        self.runtime.start()
        ids = [self.post(i).json()['job_id'] for i in range(3)]
        self.wait(lambda: self.active == 2)
        self.wait(lambda: self.sql("SELECT COUNT(*) FROM review_jobs WHERE state='failed'")[0][0] == 2)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_queue WHERE worker_active=1')[0][0], 2)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.status(ids[2], 2).json()['state'], 'queued')
        self.assertTrue(self.status(ids[0], 0).json()['external_outcome_uncertain'])
        self.gate.set()
        self.wait(lambda: len(self.started) == 3)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM review_jobs WHERE job_id IN (?,?) AND report_html IS NOT NULL", ids[:2])[0][0], 0)
        self.assertLessEqual(self.peak, 2)

    def test_stuck_shutdown_retains_process_lock_until_workers_exit(self):
        self.gate.clear()
        self.runtime.start()
        self.post()
        self.wait(lambda: self.active == 1)
        self.assertFalse(self.runtime.stop(timeout=0.02))
        self.app.state.queue_enabled = False
        self.assertEqual(self.post(1).status_code, 503)
        self.assertEqual(self.client.get('/health/ready').status_code, 503)
        contender = self.new_runtime()
        with self.assertRaises(HTTPException):
            contender.start()
        self.assertIsNotNone(self.runtime._lock_fd)
        self.gate.set()
        self.assertTrue(self.runtime.stop(timeout=3))

    def test_pre_external_interruption_is_recovered_with_new_claim(self):
        row = self.internal_save()
        self.runtime.records.recover('old-offline-owner')
        old = self.runtime.records.claim_next('old-offline-owner')
        self.runtime.start()
        self.wait(lambda: self.sql("SELECT state FROM review_jobs WHERE job_id=?", (row['job_id'],))[0][0] == 'completed')
        self.assertEqual(self.sql('SELECT attempts FROM review_jobs')[0][0], 2)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)
        stale = ClaimedReportStore(self.runtime.records, old)
        with self.assertRaises(HTTPException):
            stale.complete(row['job_id'], self.customer(), old['attempts'], '{}', 'stale', 'Synthetic', 1)

    def test_external_interruption_is_uncertain_and_never_replayed(self):
        row = self.internal_save()
        self.runtime.records.recover('old-offline-owner')
        claim = self.runtime.records.claim_next('old-offline-owner')
        self.runtime.records.mark_external(claim)
        self.runtime.start()
        self.assertEqual(self.status(row['job_id']).json()['state'], 'failed')
        self.assertTrue(self.status(row['job_id']).json()['external_outcome_uncertain'])
        self.assertFalse(self.started)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM review_holds WHERE state='active'")[0][0], 1)

    def test_completed_report_survives_restart_without_second_execution(self):
        self.runtime.start()
        job_id = self.post().json()['job_id']
        self.wait(lambda: self.status(job_id).json()['state'] == 'completed')
        self.runtime.stop()
        runtime = self.new_runtime()
        self.app.state.durable_queue = runtime
        runtime.start()
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['job_id'], job_id)
        self.assertEqual(self.client.get('/jobs/' + job_id + '/result', headers=self.headers()).json(), {'synthetic': True})
        self.assertEqual(len(self.started), 1)

    def test_twenty_duplicate_submissions_share_one_hold_and_job(self):
        self.gate.clear()
        self.runtime.start()
        with ThreadPoolExecutor(max_workers=10) as executor:
            responses = list(executor.map(lambda _: self.post(), range(20)))
        self.assertEqual([r.status_code for r in responses], [202] * 20)
        self.assertEqual(len({r.json()['job_id'] for r in responses}), 1)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_holds')[0][0], 1)
        self.gate.set()
        self.wait(lambda: self.sql('SELECT SUM(used) FROM purchases')[0][0] == 1)
        self.assertEqual(len(self.started), 1)

    def test_different_content_cannot_reuse_key_or_held_entitlement(self):
        self.gate.clear()
        self.runtime.start()
        self.assertEqual(self.post().status_code, 202)
        changed = self.payload(files=[{'fileName': 'other.txt', 'extractedText': 'Different synthetic proposal'}])
        self.assertEqual(self.client.post('/analyze', headers=self.headers(), json=changed).status_code, 409)
        self.assertEqual(self.client.post('/analyze', headers=self.headers(**{'Idempotency-Key': 'different-queue-key'}), json=changed).status_code, 409)

    def test_lost_acknowledgment_recovers_using_original_key(self):
        self.gate.clear()
        self.runtime.start()
        with patch('queue_http._ack', side_effect=RuntimeError('Synthetic lost response')):
            response = self.post()
        self.assertEqual(response.status_code, 500)
        existing = self.sql('SELECT job_id FROM review_jobs')[0][0]
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['job_id'], existing)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_holds')[0][0], 1)

    def test_missing_or_corrupt_saved_inputs_fail_without_external_work(self):
        for index in range(2):
            row = self.internal_save(index)
            bundle = self.sql('SELECT bundle_id FROM review_inputs WHERE job_id=?', (row['job_id'],))[0][0]
            path = self.storage.root / bundle / '0000.bin'
            if index == 0:
                path.unlink()
            else:
                path.chmod(0o600)
                path.write_bytes(b'Synthetic corrupted content')
        self.runtime.start()
        self.wait(lambda: self.sql("SELECT COUNT(*) FROM review_jobs WHERE state='failed'")[0][0] == 2)
        self.assertFalse(self.started)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM review_holds WHERE state='active'")[0][0], 0)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 0)

    def test_stale_owner_and_unclaimed_store_cannot_consume_or_publish(self):
        row = self.internal_save()
        self.runtime.records.recover('owner-one')
        claim = self.runtime.records.claim_next('owner-one')
        self.runtime.records.mark_external(claim)
        old = ClaimedReportStore(self.runtime.records, claim)
        self.runtime.records.recover('owner-two')
        for store in [old, SQLiteReportStore(self.ledger)]:
            with self.subTest(store=type(store).__name__):
                with self.assertRaises(HTTPException):
                    store.consume(row['job_id'], self.customer(), claim['attempts'])
                with self.assertRaises(HTTPException):
                    store.complete(row['job_id'], self.customer(), claim['attempts'], '{}', 'bad', 'Synthetic', 1)

    def test_authentication_cross_customer_and_tier_restrictions(self):
        self.runtime.start()
        self.assertEqual(self.client.post('/analyze', json=self.payload()).status_code, 401)
        self.assertEqual(self.client.post('/analyze', headers=self.headers(Authorization='Bearer invalid'), json=self.payload()).status_code, 403)
        row = self.post().json()
        self.customer(1)
        for suffix in ['', '/result', '/report']:
            self.assertEqual(self.client.get('/jobs/' + row['job_id'] + suffix, headers=self.headers(1)).status_code, 404)
        self.customer(2, tier='tier3')
        self.assertEqual(self.post(2).status_code, 503)
        self.assertEqual(self.post(3, package='tier2').status_code, 403)
        self.assertEqual(self.post(4, files=self.payload()['files'] * 2).status_code, 400)
        self.customer(5, tier='tier2')
        self.assertEqual(self.post(5, files=self.payload()['files'] * 3).status_code, 202)

    def test_missing_idempotency_key_and_unsafe_explicit_retry_rejected(self):
        self.runtime.start()
        headers = self.headers()
        del headers['Idempotency-Key']
        self.assertEqual(self.client.post('/analyze', headers=headers, json=self.payload()).status_code, 400)
        self.assertEqual(self.client.post('/analyze', headers=self.headers(**{'X-Review-Retry': 'true'}), json=self.payload()).status_code, 409)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_jobs')[0][0], 0)

    def test_unconfigured_verifier_and_unconfirmed_deployment_fail_closed(self):
        for options in [{'verifier': None}, {'deployment_confirmed': False}]:
            runtime = self.new_runtime(**options)
            with self.assertRaises(HTTPException):
                runtime.start()
            self.app.state.durable_queue = runtime
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_jobs')[0][0], 0)

    def test_temporary_server_verification_outage_pauses_then_recovers(self):
        self.verifier.unavailable = True
        self.runtime.records.retry_seconds = 0.3
        self.runtime.start()
        job_id = self.post().json()['job_id']
        self.wait(lambda: self.status(job_id).json().get('processing_paused'))
        self.assertFalse(self.started)
        self.verifier.unavailable = False
        self.wait(lambda: self.status(job_id).json()['state'] == 'completed')
        self.assertEqual(self.sql('SELECT attempts FROM review_jobs')[0][0], 2)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)

    def test_safe_retries_are_bounded(self):
        self.verifier.unavailable = True
        self.runtime.start()
        job_id = self.post().json()['job_id']
        self.wait(lambda: self.status(job_id).json()['state'] == 'failed')
        self.assertEqual(self.sql('SELECT attempts FROM review_jobs')[0][0], 2)
        self.assertEqual(len(self.verifier.revalidations), 2)
        self.assertFalse(self.started)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 0)

    def test_changed_server_owner_refund_and_product_are_not_bypassed(self):
        for index, changes in enumerate([{'owner_customer_id': 'different-owner'}, {'payment_eligible': False}, {'tier': 'tier3'}]):
            self.internal_save(index)
            self.customer(index, **changes)
        self.runtime.start()
        self.wait(lambda: self.sql("SELECT COUNT(*) FROM review_jobs WHERE state='failed'")[0][0] == 3)
        self.assertFalse(self.started)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 0)

    def test_external_timeout_is_not_automatically_replayed(self):
        calls = []
        async def timeout(manifest, files, grant):
            calls.append(grant.job_id)
            grant.consume()
            raise TimeoutError('Synthetic provider outcome unknown')
        self.runtime.processor = timeout
        self.runtime.start()
        job_id = self.post().json()['job_id']
        self.wait(lambda: self.status(job_id).json()['state'] == 'failed')
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()['external_outcome_uncertain'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)

    def test_wait_timeout_is_distinct_from_processing_timeout(self):
        row = self.internal_save()
        self.sql('UPDATE review_queue SET wait_deadline=?', (time.time() - 1,))
        self.runtime.start()
        response = self.status(row['job_id']).json()
        self.assertEqual(response['error']['code'], 'queue_wait_expired')
        self.assertFalse(self.started)
        self.assertEqual(self.sql('SELECT attempts FROM review_jobs')[0][0], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM review_holds WHERE state='active'")[0][0], 0)

    def test_storage_or_registration_failure_never_acknowledges_submission(self):
        self.runtime.start()
        with patch.object(self.storage, '_write', side_effect=OSError('Synthetic full disk')):
            self.assertEqual(self.post().status_code, 503)
        self.sql("""CREATE TRIGGER reject_queue BEFORE INSERT ON review_queue
            BEGIN SELECT RAISE(ABORT,'synthetic SQLite failure'); END""")
        self.assertEqual(self.post().status_code, 503)
        for table in ['review_jobs', 'review_holds', 'review_inputs', 'review_queue']:
            self.assertEqual(self.sql('SELECT COUNT(*) FROM ' + table)[0][0], 0)

    def test_queue_health_and_missing_dependency_do_not_fall_back_to_legacy(self):
        self.app.state.durable_queue = None
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.client.get('/health/ready').status_code, 503)
        self.app.state.durable_queue = self.runtime
        self.runtime.start()
        self.assertEqual(self.client.get('/health/ready').status_code, 200)
        self.app.state.queue_enabled = False
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.client.get('/health/ready').status_code, 503)

    def test_input_and_status_do_not_store_or_disclose_bearer_or_claim_tokens(self):
        self.runtime.start()
        response = self.post()
        job_id = response.json()['job_id']
        self.wait(lambda: self.status(job_id).json()['state'] == 'completed')
        dumped = '\n'.join(self.ledger_dump())
        self.assertNotIn('proof-synthetic-order-0', dumped)
        for path in self.storage.root.glob('*/manifest.json'):
            self.assertNotIn('proof-synthetic-order-0', path.read_text())
        status = json.dumps(self.status(job_id).json())
        for key in ['claim_token', 'owner_token', 'customer_email', 'extractedText', 'bundle_id']:
            self.assertNotIn(key, status)

    def ledger_dump(self):
        with self.ledger._connect() as db:
            return list(db.iterdump())

    def test_real_pipeline_preserves_html_json_and_notification_failure_recovery(self):
        self.runtime.processor = process_saved_submission
        self.runtime.start()
        with patch.object(main, 'send_review_email', side_effect=RuntimeError('Synthetic mail failure')):
            response = self.post()
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()['job_id']
            self.wait(lambda: self.status(job_id).json().get('notification', {}).get('state') == 'failed')
        html = self.client.get('/jobs/' + job_id + '/report', headers=self.headers())
        result = self.client.get('/jobs/' + job_id + '/result', headers=self.headers())
        self.assertEqual(html.status_code, 200)
        self.assertEqual(html.text, main.build_report_html(main.HVACAnalysis.model_validate(result.json()), 1))
        self.assertIn('window.print()', html.text)
        retry = self.client.post('/jobs/' + job_id + '/delivery-retry', headers=self.headers())
        self.assertEqual(retry.json()['notification']['state'], 'sent')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)

    def test_original_heic_pages_remain_one_quote_through_existing_pipeline(self):
        from test_image_intake import photo
        self.runtime.processor = process_saved_submission
        self.runtime.start()
        with patch.object(main, 'transcribe_page', return_value=BAD) as vision:
            response = self.client.post('/upload', headers=self.headers(),
                data={'customer_name': 'Synthetic', 'customer_email': 'synthetic@example.invalid',
                      'quote_groups': json.dumps([{'pages': [{'file': 1, 'rotation': 90}, {'file': 0, 'rotation': 0}]}])},
                files=[('files', ('first.heic', photo(format='HEIF'), 'image/heic')),
                       ('files', ('second.HEIC', photo(format='HEIF', marker='Second page'), 'image/heic'))])
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()['job_id']
            self.wait(lambda: self.status(job_id).json()['state'] == 'completed', timeout=20)
            self.assertEqual(vision.call_count, 2)
        self.assertEqual(self.sql('SELECT quote_count FROM review_jobs')[0][0], 1)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)

    def test_unreadable_photo_does_not_run_hvac_or_consume_review(self):
        from document_intake import BETTER_PHOTO
        from test_image_intake import photo
        self.runtime.processor = process_saved_submission
        self.runtime.start()
        with patch.object(main, 'transcribe_page', side_effect=HTTPException(422, BETTER_PHOTO)), patch.object(main, 'classify_quotes') as classifier:
            response = self.client.post('/upload', headers=self.headers(),
                data={'customer_name': 'Synthetic', 'customer_email': 'synthetic@example.invalid'},
                files=[('files', ('synthetic.jpg', photo(), 'image/jpeg'))])
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()['job_id']
            self.wait(lambda: self.status(job_id).json()['state'] == 'failed')
            self.assertEqual(self.status(job_id).json()['error']['code'], 'unreadable_photo')
            classifier.assert_not_called()
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM review_holds WHERE state='active'")[0][0], 0)

    def test_stage2_migration_is_explicit_idempotent_and_preserves_stage1_data(self):
        row = self.internal_save()
        before = self.sql('SELECT * FROM review_inputs')
        migrate_stage2(self.ledger)
        self.assertEqual(self.sql('SELECT * FROM review_inputs'), before)
        self.assertEqual(self.sql('PRAGMA user_version')[0][0], 2)
        self.assertEqual(self.sql('PRAGMA integrity_check')[0][0], 'ok')
        self.assertEqual(self.sql('SELECT job_id FROM review_queue')[0][0], row['job_id'])

    def child_runtime(self, mode):
        script = '''
import os,sys,time
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
def offline(event,args):
    if event in ('socket.connect','socket.getaddrinfo','socket.sendto'):
        raise RuntimeError('Network forbidden')
    if event=='open' and isinstance(args[0],str) and os.path.basename(args[0])=='.env':
        raise RuntimeError('Environment access forbidden')
sys.addaudithook(offline)
from pathlib import Path
from fastapi import HTTPException
from purchase_security import SQLitePurchaseLedger
from durable_uploads import PrivateUploadStore
from queue_runtime import QueueRuntime,RevalidatedPurchase
ledger=SQLitePurchaseLedger(Path(sys.argv[2]))
storage=PrivateUploadStore(Path(sys.argv[3]),ledger,max_bytes=2**27,reserve_bytes=0)
mode=sys.argv[4]
class Offline:
    def revalidate(self,purchase_id):
        if mode=='preparing': os._exit(42)
        with ledger._connect() as db:
            row=db.execute('SELECT customer_id,tier,submission_limit,expires_at,eligible FROM purchases WHERE purchase_id=?',(purchase_id,)).fetchone()
        return RevalidatedPurchase(purchase_id=purchase_id,owner_customer_id=row[0],tier=row[1],submission_limit=row[2],expires_at=row[3],payment_eligible=bool(row[4]))
async def process(manifest,files,grant):
    grant.consume()
    os._exit(43)
runtime=QueueRuntime(storage,Offline(),process,deployment_confirmed=True,poll_seconds=0.01)
try: runtime.start()
except HTTPException: sys.exit(77)
time.sleep(5)
runtime.stop()
sys.exit(88)
'''
        return subprocess.run([sys.executable, '-B', '-c', script, str(Path(__file__).parent),
            str(self.ledger.path), str(self.storage.root), mode], timeout=8,
            capture_output=True, text=True).returncode

    def test_process_crash_before_external_recovers_automatically(self):
        row = self.internal_save()
        self.assertEqual(self.child_runtime('preparing'), 42)
        self.runtime.start()
        self.wait(lambda: self.status(row['job_id']).json()['state'] == 'completed')
        self.assertEqual(self.sql('SELECT attempts FROM review_jobs')[0][0], 2)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)

    def test_process_crash_after_external_retains_usage_without_replay(self):
        row = self.internal_save()
        self.assertEqual(self.child_runtime('external'), 43)
        self.runtime.start()
        self.assertTrue(self.status(row['job_id']).json()['external_outcome_uncertain'])
        self.assertFalse(self.started)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)

    def test_second_process_cannot_acquire_live_scheduler_lock(self):
        self.runtime.start()
        self.assertEqual(self.child_runtime('contender'), 77)
        self.assertTrue(self.runtime.ready)

    def test_original_document_formats_pass_durable_upload_and_existing_processor(self):
        from test_document_intake import pdf, docx
        from test_image_intake import scan, photo
        self.runtime.processor = process_saved_submission
        self.runtime.start()
        cases = [('proposal.txt', BAD.encode()), ('proposal.pdf', pdf()), ('proposal.docx', docx()),
                 ('scan.pdf', scan()), ('proposal.png', photo(format='PNG'))]
        with patch.object(main, 'transcribe_page', return_value=BAD):
            for index, (filename, data) in enumerate(cases):
                with self.subTest(filename=filename):
                    response = self.client.post('/upload', headers=self.headers(index),
                        data={'customer_name': 'Synthetic', 'customer_email': 'synthetic@example.invalid'},
                        files=[('files', (filename, data, 'application/octet-stream'))])
                    self.assertEqual(response.status_code, 202, response.text)
                    job_id = response.json()['job_id']
                    self.wait(lambda: self.status(job_id, index).json()['state'] == 'completed', timeout=20)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], len(cases))

    def test_json_provenance_and_pricing_context_reach_unchanged_analysis(self):
        captured = []
        async def analyze(request, grant):
            captured.append(request)
            grant.consume()
            grant.store.complete(grant.job_id, grant.purchase, grant.attempt, '{}', '<p>Synthetic</p>', 'Synthetic', 1)
        self.runtime.processor = process_saved_submission
        self.runtime.start()
        with patch.object(main, 'analyze_hvac_quote', side_effect=analyze):
            response = self.post(city='Synthetic City', state='XX', project_zip='00000', contractor_1_name='Synthetic Contractor')
            self.assertEqual(response.status_code, 202)
            self.wait(lambda: self.status(response.json()['job_id']).json()['state'] == 'completed')
        request = captured[0]
        self.assertEqual(request.files[0].fileName, 'original-proposal.pdf')
        self.assertEqual(request.files[0].extractedText, BAD)
        self.assertEqual((request.city, request.state, request.project_zip), ('Synthetic City', 'XX', '00000'))
        self.assertEqual(request.contractor_1_name, 'Synthetic Contractor')

    def test_queue_migration_rolls_back_and_does_not_rebuild_legacy_reports(self):
        path = self.base / 'stage-one.sqlite3'
        ledger = SQLitePurchaseLedger.initialize(path)
        purchase = self.customer()
        ledger.register(purchase)
        store = SQLiteReportStore(ledger)
        row = store.create(purchase, 'synthetic-key', 'synthetic-content')
        attempt = store.claim(row['job_id'], purchase, False)
        store.consume(row['job_id'], purchase, attempt)
        store.complete(row['job_id'], purchase, attempt, '{"saved":true}', '<p>Saved</p>', 'Synthetic', 1)
        before = store.get(row['job_id'], purchase)
        import sqlite_migrations
        with patch.object(sqlite_migrations, 'STAGE2_SCHEMA', (*sqlite_migrations.STAGE2_SCHEMA, 'INVALID SQL')):
            with self.assertRaises(sqlite3.Error):
                migrate_stage2(ledger)
        with ledger._connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='review_queue'").fetchone())
        migrate_stage2(ledger)
        self.assertEqual(store.get(row['job_id'], purchase), before)

    def test_scheduler_sqlite_fault_stops_dispatch_and_fails_readiness(self):
        with patch.object(self.runtime.records, 'tick', side_effect=HTTPException(503, 'Synthetic storage failure')):
            self.runtime.start()
            self.wait(lambda: self.runtime._faulted.is_set())
            self.assertEqual(self.post().status_code, 503)
        self.assertFalse(self.started)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_jobs')[0][0], 0)

    def test_paused_retry_waits_until_prior_worker_finishes(self):
        self.internal_save()
        queue = self.runtime.records
        queue.recover('offline-owner')
        claim = queue.claim_next('offline-owner')
        queue.fail_claim(claim, 'verification_unavailable', 503, retry=True)
        self.sql('UPDATE review_queue SET retry_at=0')
        self.assertIsNone(queue.claim_next('offline-owner'))
        queue.finish_worker(claim)
        retry = queue.claim_next('offline-owner')
        self.assertEqual(retry['job_id'], claim['job_id'])
        self.assertEqual(retry['attempts'], 2)
        self.assertNotEqual(retry['claim_token'], claim['claim_token'])

    def test_restart_after_recorded_retry_before_worker_cleanup(self):
        row = self.internal_save()
        queue = self.runtime.records
        queue.recover('old-offline-owner')
        claim = queue.claim_next('old-offline-owner')
        queue.fail_claim(claim, 'verification_unavailable', 503, retry=True)
        self.runtime.start()
        self.wait(lambda: self.status(row['job_id']).json()['state'] == 'completed')
        self.assertEqual(self.sql('SELECT attempts FROM review_jobs')[0][0], 2)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)

    def test_report_publication_sqlite_failure_rolls_back_complete_report(self):
        self.sql("""CREATE TRIGGER reject_queue_completion BEFORE UPDATE OF phase ON review_queue
            WHEN NEW.phase='finished' BEGIN SELECT RAISE(ABORT,'Synthetic storage failure'); END""")
        self.runtime.processor = process_saved_submission
        self.runtime.start()
        with patch.object(main, 'send_review_email') as sender:
            job_id = self.post().json()['job_id']
            self.wait(lambda: self.status(job_id).json()['state'] == 'failed')
            self.assertTrue(self.status(job_id).json()['external_outcome_uncertain'])
            sender.assert_not_called()
        self.assertEqual(self.sql('SELECT result_json,report_html FROM review_jobs')[0], (None, None))
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)
        self.assertEqual(self.client.get('/jobs/' + job_id + '/report', headers=self.headers()).status_code, 409)

    def test_corrupt_replay_cannot_release_uncertain_or_live_worker_hold(self):
        row = self.internal_save()
        queue = self.runtime.records
        queue.recover('offline-owner')
        claim = queue.claim_next('offline-owner')
        queue.mark_external(claim)
        queue.fail_claim(claim, 'analysis_failed', 503)
        authorization = AuthorizedPurchase(self.customer(), self.ledger)
        for finished in (False, True):
            if finished:
                queue.finish_worker(claim)
            with self.assertRaises(HTTPException):
                queue.release(authorization, row['job_id'])
            self.assertEqual(self.sql('SELECT phase FROM review_queue')[0][0], 'uncertain')
            self.assertEqual(self.sql('SELECT state FROM review_holds')[0][0], 'active')


if __name__ == '__main__':
    unittest.main()
