"""Synthetic Stage 3 lifecycle, customer status and backup/restore tests."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException
import test_queue_stage2 as stage2
from durable_uploads import StoredInputError
from purchase_security import AuthorizedPurchase
from queue_maintenance import (RetentionPolicy, cleanup_inputs, cleanup_temporary,
    create_backup, restore_backup, confirm_restore_reconciled)
from queue_runtime import QueueRuntime
from report_jobs import SQLiteReportStore
from sqlite_migrations import migrate_stage3


class QueueStage3Tests(unittest.TestCase):
    # Reuse fixtures, without rediscovering the 41 accepted Stage 2 tests.
    new_runtime = stage2.QueueStage2Tests.new_runtime
    shutdown = stage2.QueueStage2Tests.shutdown
    process = stage2.QueueStage2Tests.process
    customer = stage2.QueueStage2Tests.customer
    headers = stage2.QueueStage2Tests.headers
    payload = stage2.QueueStage2Tests.payload
    post = stage2.QueueStage2Tests.post
    sql = stage2.QueueStage2Tests.sql
    wait = stage2.QueueStage2Tests.wait
    status = stage2.QueueStage2Tests.status
    internal_save = stage2.QueueStage2Tests.internal_save

    def setUp(self):
        stage2.QueueStage2Tests.setUp(self)
        migrate_stage3(self.ledger)

    def complete(self, index=0):
        self.runtime.start()
        job = self.post(index).json()['job_id']
        self.wait(lambda: self.status(job, index).json()['state'] == 'completed')
        self.wait(lambda: not self.sql('SELECT 1 FROM review_queue WHERE worker_active=1'))
        return job

    def old(self, job, days=10):
        self.sql('UPDATE review_jobs SET updated=? WHERE job_id=?', (time.time() - days * 86400, job))
        self.sql('UPDATE review_queue SET completed_at=? WHERE job_id=?', (time.time() - days * 86400, job))

    def bundle(self, job):
        return self.storage.root / self.sql('SELECT bundle_id FROM review_inputs WHERE job_id=?', (job,))[0][0]

    def backup(self):
        return create_backup(self.storage, self.base / 'snapshot', max_bytes=32 * 1024 * 1024)

    def restore(self):
        return restore_backup(self.base / 'snapshot', self.base / 'restored', max_bytes=32 * 1024 * 1024)

    def test_migration_is_additive_explicit_and_idempotent(self):
        row = self.internal_save()
        before = self.sql('SELECT * FROM review_inputs')
        migrate_stage3(self.ledger)
        self.assertEqual(self.sql('SELECT * FROM review_inputs'), before)
        self.assertEqual(self.sql('PRAGMA user_version')[0][0], 3)
        self.assertEqual(self.sql('SELECT reconciliation_required FROM restore_control'), [(0,)])
        self.assertTrue(self.bundle(row['job_id']).exists())

    def test_defaults_do_not_enable_report_expiry(self):
        policy = RetentionPolicy()
        self.assertEqual((policy.temporary_seconds, policy.completed_input_seconds, policy.failed_input_seconds), (3600, 86400, 604800))
        self.assertIsNone(policy.report_days)
        with self.assertRaises(ValueError):
            RetentionPolicy(report_days=90)
        with self.assertRaises(ValueError):
            RetentionPolicy(completed_input_seconds=0)

    def test_cleanup_preview_does_not_delete(self):
        job = self.complete()
        self.old(job)
        self.assertEqual(cleanup_inputs(self.storage), {'eligible': 1, 'deleted': 0})
        self.assertTrue(self.bundle(job).exists())
        self.assertEqual(self.sql('SELECT COUNT(*) FROM input_retention')[0][0], 0)

    def test_completed_inputs_retire_but_reports_usage_and_keys_remain(self):
        job = self.complete()
        self.old(job)
        keys = self.sql('SELECT * FROM review_request_keys')
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['deleted'], 1)
        self.assertFalse(self.bundle(job).exists())
        self.assertEqual(self.sql('SELECT used FROM purchases'), [(1,)])
        self.assertEqual(self.sql('SELECT state FROM review_holds'), [('consumed',)])
        self.assertEqual(self.sql('SELECT * FROM review_request_keys'), keys)
        self.assertEqual(self.status(job).json()['source_documents_retired'], True)
        self.assertEqual(self.client.get(f'/jobs/{job}/report', headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get(f'/jobs/{job}/result', headers=self.headers()).json(), {'synthetic': True})
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['deleted'], 0)

    def test_replay_after_retention_keeps_same_job_without_new_consumption(self):
        job = self.complete()
        self.old(job)
        cleanup_inputs(self.storage, dry_run=False)
        response = self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['job_id'], job)
        self.assertEqual(response.json()['state'], 'completed')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM submissions')[0][0], 1)
        self.assertEqual(len(self.started), 1)

    def test_retention_between_replay_registration_and_ack_keeps_stable_job(self):
        job = self.complete()
        self.old(job)
        original = self.storage.load
        def retire_then_load(*args):
            cleanup_inputs(self.storage, dry_run=False)
            return original(*args)
        with patch.object(self.storage, 'load', side_effect=retire_then_load):
            response = self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['job_id'], job)
        self.assertEqual(response.json()['state'], 'completed')

    def test_recent_inputs_preserved(self):
        job = self.complete()
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['eligible'], 0)
        self.assertTrue(self.bundle(job).exists())

    def test_notification_updates_do_not_reset_completion_retention_clock(self):
        job = self.complete()
        self.old(job)
        self.sql('UPDATE review_jobs SET updated=? WHERE job_id=?', (time.time(), job))
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['deleted'], 1)

    def test_cleanup_never_deletes_active_worker_even_after_report_saved(self):
        job = self.complete()
        self.old(job)
        self.sql('UPDATE review_queue SET worker_active=1 WHERE job_id=?', (job,))
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['eligible'], 0)
        self.assertTrue(self.bundle(job).exists())
        self.sql('UPDATE review_queue SET worker_active=0 WHERE job_id=?', (job,))

    def test_pending_paused_and_uncertain_inputs_preserved(self):
        row = self.internal_save()
        job = row['job_id']
        self.old(job)
        for phase in ['ready', 'paused', 'uncertain']:
            self.sql('UPDATE review_queue SET phase=? WHERE job_id=?', (phase, job))
            self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['eligible'], 0)
        self.assertTrue(self.bundle(job).exists())

    def test_failed_inputs_wait_seven_days_and_preserve_retryable_jobs(self):
        job = self.internal_save()['job_id']
        self.sql("UPDATE review_jobs SET state='failed',retryable=1 WHERE job_id=?", (job,))
        self.sql("UPDATE review_queue SET phase='failed' WHERE job_id=?", (job,))
        self.sql("UPDATE review_holds SET state='released' WHERE job_id=?", (job,))
        self.old(job)
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['eligible'], 0)
        self.sql('UPDATE review_jobs SET retryable=0 WHERE job_id=?', (job,))
        self.old(job, days=2)
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['eligible'], 0)
        self.old(job)
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['deleted'], 1)
        self.assertEqual(self.sql('SELECT used FROM purchases'), [(0,)])

    def test_interrupted_cleanup_resumes_from_durable_tombstone(self):
        job = self.complete()
        self.old(job)
        import queue_maintenance
        original = queue_maintenance._remove_bundle
        def interrupted(*args):
            original(*args)
            raise OSError('Synthetic failure after deletion')
        with patch.object(queue_maintenance, '_remove_bundle', side_effect=interrupted):
            with self.assertRaises(HTTPException):
                cleanup_inputs(self.storage, dry_run=False)
        self.assertEqual(self.sql('SELECT state FROM input_retention'), [('deleting',)])
        with self.assertRaises(HTTPException) as error:
            self.storage.load(AuthorizedPurchase(self.customer(), self.ledger), job)
        self.assertEqual(error.exception.status_code, 410)
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['deleted'], 1)

    def test_cleanup_database_failure_cannot_delete_files(self):
        job = self.complete()
        self.old(job)
        self.sql("CREATE TRIGGER stop_retention BEFORE INSERT ON input_retention BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(HTTPException):
            cleanup_inputs(self.storage, dry_run=False)
        self.assertTrue(self.bundle(job).exists())

    def test_cleanup_rejects_symlink_and_preserves_target(self):
        job = self.complete()
        self.old(job)
        target = self.base / 'private-synthetic.txt'
        target.write_text('synthetic')
        (self.bundle(job) / '0000.bin').unlink()
        (self.bundle(job) / '0000.bin').symlink_to(target)
        with self.assertRaises(HTTPException):
            cleanup_inputs(self.storage, dry_run=False)
        self.assertEqual(target.read_text(), 'synthetic')

    def test_incomplete_temp_grace_and_referenced_protection(self):
        job = self.internal_save()['job_id']
        temp = self.storage.root / ('.tmp-' + 'a' * 32)
        temp.mkdir(mode=0o700)
        self.assertEqual(cleanup_temporary(self.storage, dry_run=False)['deleted'], 0)
        os.utime(temp, (time.time() - 7200,) * 2)
        self.assertEqual(cleanup_temporary(self.storage)['eligible'], 1)
        self.assertEqual(cleanup_temporary(self.storage, dry_run=False)['deleted'], 1)
        self.assertTrue(self.bundle(job).exists())

    def test_authenticated_reconnection_preserves_job_id_and_status(self):
        row = self.internal_save()
        jobs = self.client.get('/jobs', headers=self.headers()).json()['jobs']
        self.assertEqual(jobs[0]['job_id'], row['job_id'])
        self.assertEqual(jobs[0]['state'], 'queued')
        self.assertEqual(jobs[0]['approximate_queue_position'], 1)
        self.assertIn('approximate', jobs[0]['queue_estimate_notice'])
        self.assertFalse(jobs[0]['retry_allowed'])
        self.runtime.start()
        self.wait(lambda: self.status(row['job_id']).json()['state'] == 'completed')
        self.assertEqual(self.client.get('/jobs', headers=self.headers()).json()['jobs'][0]['job_id'], row['job_id'])

    def test_status_privacy_isolation_and_private_cache_headers(self):
        job = self.internal_save()['job_id']
        for suffix in ['', '/result', '/report']:
            self.assertEqual(self.client.get(f'/jobs/{job}{suffix}').status_code, 401)
            self.assertEqual(self.client.get(f'/jobs/{job}{suffix}', headers=self.headers(1)).status_code, 404)
        response = self.status(job)
        self.assertIn('no-store', response.headers['cache-control'])
        for secret in ['synthetic-owner', 'proof-', 'claim_token', 'bundle_id', 'customer_email']:
            self.assertNotIn(secret, response.text)

    def test_twenty_customers_progress_reconnect_and_retention_preserves_all_reports(self):
        stage2.QueueStage2Tests.test_twenty_customers_all_accepted_two_workers_and_automatic_drain(self)
        self.wait(lambda: not self.sql('SELECT 1 FROM review_queue WHERE worker_active=1'))
        self.sql('UPDATE review_jobs SET updated=?', (time.time() - 2 * 86400,))
        self.sql('UPDATE review_queue SET completed_at=?', (time.time() - 2 * 86400,))
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False, limit=7)['deleted'], 7)
        self.assertEqual(cleanup_inputs(self.storage, dry_run=False)['deleted'], 13)
        for index in range(20):
            job = self.client.get('/jobs', headers=self.headers(index)).json()['jobs'][0]
            self.assertEqual(job['state'], 'completed')
            self.assertEqual(self.client.get(job['report_url'], headers=self.headers(index)).status_code, 200)
        self.assertEqual(self.sql('SELECT SUM(used) FROM purchases')[0][0], 20)

    def test_backup_restore_queued_and_completed_with_ownership_and_exact_reports(self):
        completed = self.complete()
        self.assertTrue(self.runtime.stop())
        queued = self.internal_save(1)['job_id']
        original_report = self.sql('SELECT report_html,result_json FROM review_jobs WHERE job_id=?', (completed,))
        self.assertTrue(self.backup()['complete'])
        restored = self.restore()
        with restored.records._connect() as db:
            self.assertEqual(db.execute('SELECT report_html,result_json FROM review_jobs WHERE job_id=?', (completed,)).fetchall(), original_report)
            self.assertEqual(db.execute('SELECT SUM(used) FROM purchases').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT reconciliation_required FROM restore_control').fetchone()[0], 1)
        grant = AuthorizedPurchase(self.customer(1), restored.records.ledger)
        manifest, files = restored.load(grant, queued)
        self.assertEqual(len(manifest['quotes']), 1)
        self.assertTrue(files)
        with self.assertRaises(HTTPException):
            restored.load(AuthorizedPurchase(self.customer(), restored.records.ledger), queued)

    def test_restored_processing_is_blocked_until_explicit_reconciliation(self):
        job = self.internal_save()['job_id']
        self.backup()
        restored = self.restore()
        runtime = QueueRuntime(restored, self.verifier, self.process, deployment_confirmed=True)
        self.runtimes.append(runtime)
        with self.assertRaises(HTTPException):
            runtime.start()
        with self.assertRaises(ValueError):
            confirm_restore_reconciled(restored)
        confirm_restore_reconciled(restored, entitlements_reconciled=True, external_outcomes_reconciled=True)
        runtime = QueueRuntime(restored, self.verifier, self.process, deployment_confirmed=True, poll_seconds=0.01)
        self.runtimes.append(runtime)
        runtime.start()
        self.wait(lambda: runtime.records.get(job, self.customer())['state'] == 'completed')
        self.assertEqual(runtime.records.get(job, self.customer())['attempts'], 1)

    def test_restore_blocks_legacy_consumption_and_cleanup(self):
        self.internal_save()
        self.backup()
        restored = self.restore()
        with self.assertRaises(Exception):
            restored.records.ledger.consume(self.customer())
        with self.assertRaises(HTTPException):
            cleanup_inputs(restored, dry_run=False)
        with self.assertRaises(HTTPException):
            restored.cleanup_orphans()

    def test_backup_rejects_live_scheduler_and_active_worker(self):
        self.runtime.start()
        with self.assertRaises(BlockingIOError):
            self.backup()
        self.assertFalse((self.base / 'snapshot').exists())
        self.runtime.stop()
        job = self.internal_save()['job_id']
        self.sql('UPDATE review_queue SET worker_active=1 WHERE job_id=?', (job,))
        with self.assertRaises(HTTPException):
            self.backup()

    def test_backup_checks_actual_source_hashes(self):
        job = self.internal_save()['job_id']
        source = self.bundle(job) / '0000.bin'
        source.chmod(0o600)
        source.write_bytes(b'corrupt')
        with self.assertRaises(StoredInputError):
            self.backup()
        self.assertFalse((self.base / 'snapshot' / 'backup.json').exists())

    def test_backup_excludes_retired_inputs_and_contains_no_credentials(self):
        job = self.complete()
        self.old(job)
        cleanup_inputs(self.storage, dry_run=False)
        self.runtime.stop()
        self.backup()
        metadata = json.loads((self.base / 'snapshot' / 'backup.json').read_text())
        self.assertEqual(set(metadata['files']), {'ledger.sqlite3', 'inputs/store.json'})
        self.assertNotIn('proof-', json.dumps(metadata))
        restored = self.restore()
        self.assertEqual(restored.records.get(job, self.customer())['state'], 'completed')
        with self.assertRaises(HTTPException):
            restored.records.claim_delivery(job, self.customer())

    def test_restore_rejects_corrupted_snapshot(self):
        job = self.internal_save()['job_id']
        self.backup()
        source = self.base / 'snapshot' / 'inputs' / self.bundle(job).name / '0000.bin'
        source.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.restore()
        # A partial restore's ledger is still fenced.
        import sqlite3
        with sqlite3.connect(self.base / 'restored' / 'ledger.sqlite3') as db:
            self.assertEqual(db.execute('SELECT reconciliation_required FROM restore_control').fetchone()[0], 1)

    def test_restore_rejects_path_traversal(self):
        self.internal_save()
        self.backup()
        path = self.base / 'snapshot' / 'backup.json'
        metadata = json.loads(path.read_text())
        metadata['files']['../escape'] = {'size': 1, 'sha256': 'x'}
        path.write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):
            self.restore()
        self.assertFalse((self.base / 'restored').exists())

    def test_backup_and_restore_never_overwrite_existing_destinations(self):
        self.internal_save()
        self.backup()
        with self.assertRaises(HTTPException):
            self.backup()
        self.restore()
        with self.assertRaises(FileExistsError):
            self.restore()

    def test_backup_and_restore_byte_limits(self):
        self.internal_save()
        with self.assertRaises(HTTPException):
            create_backup(self.storage, self.base / 'tiny', max_bytes=1)
        self.assertFalse((self.base / 'tiny' / 'backup.json').exists())
        self.backup()
        with self.assertRaises(ValueError):
            restore_backup(self.base / 'snapshot', self.base / 'small', max_bytes=1)
        self.assertFalse((self.base / 'small').exists())

    def test_backup_permissions_and_source_preserved(self):
        self.internal_save()
        before = self.sql('SELECT * FROM review_jobs')
        self.backup()
        for path in (self.base / 'snapshot').rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(self.sql('SELECT * FROM review_jobs'), before)
        self.assertEqual(self.sql('SELECT reconciliation_required FROM restore_control'), [(0,)])

    def test_backup_write_failure_leaves_no_complete_marker_or_source_loss(self):
        job = self.internal_save()['job_id']
        with patch('queue_maintenance._copy', side_effect=OSError('Synthetic full disk')):
            with self.assertRaises(HTTPException):
                self.backup()
        self.assertTrue(self.bundle(job).exists())
        self.assertFalse((self.base / 'snapshot' / 'backup.json').exists())
        self.assertEqual(self.sql('SELECT reconciliation_required FROM restore_control'), [(0,)])

    def test_missing_snapshot_document_blocks_restore(self):
        job = self.internal_save()['job_id']
        self.backup()
        (self.base / 'snapshot' / 'inputs' / self.bundle(job).name / '0000.bin').unlink()
        with self.assertRaises(FileNotFoundError):
            self.restore()

    def test_backup_serializes_database_registration_and_document_snapshot(self):
        self.internal_save()
        import queue_maintenance
        original = queue_maintenance._copy
        started = threading.Event()
        future = None
        def writer():
            started.set()
            self.internal_save(1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            def copying(*args):
                nonlocal future
                if future is None:
                    future = pool.submit(writer)
                    self.assertTrue(started.wait(2))
                    self.assertFalse(future.done())
                return original(*args)
            with patch.object(queue_maintenance, '_copy', side_effect=copying):
                self.backup()
            future.result(timeout=5)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_inputs')[0][0], 2)
        restored = self.restore()
        with restored.records._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_inputs').fetchone()[0], 1)

    def test_incomplete_cleanup_snapshot_restores_with_no_input_resurrection(self):
        job = self.complete()
        self.old(job)
        self.runtime.stop()
        with patch('queue_maintenance._remove_bundle', side_effect=OSError('Synthetic interrupted cleanup')):
            with self.assertRaises(HTTPException):
                cleanup_inputs(self.storage, dry_run=False)
        self.backup()
        restored = self.restore()
        confirm_restore_reconciled(restored, entitlements_reconciled=True, external_outcomes_reconciled=True)
        self.assertEqual(cleanup_inputs(restored, dry_run=False)['deleted'], 1)
        self.assertEqual(restored.records.get(job, self.customer())['state'], 'completed')

    def test_unreconciled_restore_cannot_be_backed_up_as_authoritative(self):
        self.internal_save()
        self.backup()
        restored = self.restore()
        with self.assertRaises(Exception):
            create_backup(restored, self.base / 'unreconciled', max_bytes=1000000)


class QueueBrowserTests(unittest.TestCase):
    def test_local_status_interface_behaviors(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js required for local interface regression tests')
        result = subprocess.run([node, '--test', 'test_quote_upload_ui.js'], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
