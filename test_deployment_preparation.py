"""Deployment-only regressions; synthetic storage and no external providers."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import shlex
from unittest.mock import patch

from fastapi.testclient import TestClient

from deployment_app import create_app
from deployment_ops import closed_check, main as operator, migrate, provision, verify
from deployment_storage import DeploymentConfig, DeploymentError, open_storage
from durable_uploads import PrivateUploadStore, UploadSource
from purchase_security import AuthorizedPurchase, SQLitePurchaseLedger, VerifiedPurchase
from queue_maintenance import create_backup, stopped_queue
from runtime_safety import local_readiness
from sqlite_migrations import migrate_stage2
import test_queue_stage2 as stage2


class DeploymentPreparationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='cyt-deployment-'))).resolve()
        self.config = DeploymentConfig(disk_mount=self.base, data_dir=self.base / 'cyt',
            max_bytes=4_000_000, reserve_bytes=1_000_000, require_mount=False)
        self.stack.enter_context(patch.dict(os.environ, {'CYT_QUEUE_ENABLED': 'false', 'PYTHON_DOTENV_DISABLED': '1'}))
        self.purchases = {}
        self.verifier = stage2.OfflineVerifier(self.purchases)

    def provision(self):
        provision(self.config)
        return open_storage(self.config)

    def cli(self, command, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = operator([command, '--disk-mount', str(self.base), '--data-dir', str(self.config.data_dir), '--synthetic', *args])
        return code, json.loads(output.getvalue())

    def test_default_config_is_queue_disabled_and_mount_required(self):
        with patch.dict(os.environ, {}, clear=True):
            config = DeploymentConfig.from_env()
        self.assertFalse(config.queue_enabled)
        self.assertTrue(config.require_mount)
        self.assertEqual(config.ledger_path, Path('/var/data/cyt/ledger.sqlite3'))

    def test_invalid_environment_switch_or_budget_fails_closed(self):
        for env in ({'CYT_QUEUE_ENABLED': 'yes'}, {'CYT_INPUT_MAX_BYTES': '-1'}, {'CYT_INPUT_MAX_BUNDLES': 'invalid'}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), self.assertRaises(DeploymentError):
                DeploymentConfig.from_env()

    def test_traversal_repository_and_outside_mount_rejected(self):
        for path in (self.base / '..' / 'bad', Path(__file__).resolve().parent / 'private', self.base / 'nested' / 'cyt'):
            with self.subTest(path=path), self.assertRaises(DeploymentError):
                replace(self.config, data_dir=path)

    def test_unmounted_directory_is_not_a_production_volume(self):
        with self.assertRaises(DeploymentError):
            provision(replace(self.config, require_mount=True))
        self.assertFalse(self.config.data_dir.exists())

    def test_missing_mount_never_created(self):
        config = replace(self.config, disk_mount=self.base / 'absent', data_dir=self.base / 'absent' / 'cyt')
        with self.assertRaises(OSError):
            provision(config)
        self.assertFalse(config.disk_mount.exists())

    def test_explicit_fresh_provisioning_and_private_permissions(self):
        result = provision(self.config)
        self.assertEqual(result['schema'], 3)
        self.assertEqual(result['job_counts'], {})
        self.assertEqual(self.config.data_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.config.ledger_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.input_path.stat().st_mode & 0o777, 0o700)

    def test_initialization_never_overwrites_existing_data(self):
        self.provision()
        before = self.config.ledger_path.read_bytes()
        with self.assertRaises(FileExistsError):
            provision(self.config)
        self.assertEqual(self.config.ledger_path.read_bytes(), before)

    def test_failed_initialization_stays_closed_without_automatic_repair(self):
        with patch('deployment_ops.migrate_stage3', side_effect=OSError('synthetic storage failure')):
            with self.assertRaises(OSError):
                provision(self.config)
        self.assertTrue(self.config.ledger_path.exists())
        with self.assertRaises(FileExistsError):
            provision(self.config)
        with self.assertRaises(DeploymentError):
            open_storage(self.config)

    def test_disk_headroom_failure_creates_no_ledger(self):
        with patch('deployment_ops.os.fstatvfs') as stat:
            stat.return_value.f_bavail = 0
            stat.return_value.f_frsize = 4096
            with self.assertRaises(DeploymentError):
                provision(self.config)
        self.assertFalse(self.config.data_dir.exists())

    def test_private_root_permissions_and_symlink_rejected(self):
        self.provision()
        self.config.data_dir.chmod(0o755)
        with self.assertRaises(OSError):
            open_storage(self.config)
        self.config.data_dir.chmod(0o700)
        alias = self.base / 'alias'
        alias.symlink_to(self.config.data_dir, target_is_directory=True)
        with self.assertRaises(OSError):
            open_storage(replace(self.config, data_dir=alias))

    def test_unknown_schema_and_foreign_key_corruption_rejected(self):
        self.provision()
        with contextlib.closing(sqlite3.connect(self.config.ledger_path)) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(sqlite3.Error):
            open_storage(self.config)
        with contextlib.closing(sqlite3.connect(self.config.ledger_path)) as db:
            db.execute('PRAGMA user_version=3')
            db.execute("INSERT INTO submissions VALUES ('orphan','missing-owner',0)")
            db.commit()
        with self.assertRaises(DeploymentError):
            verify(self.config)

    def test_migration_preserves_existing_purchase_records(self):
        self.config.data_dir.mkdir(mode=0o700)
        ledger = SQLitePurchaseLedger.initialize(self.config.ledger_path)
        PrivateUploadStore.provision(self.config.input_path, ledger, **self.config.limits)
        with ledger._connect() as db:
            db.execute("INSERT INTO purchases VALUES ('synthetic','owner','tier1',2,1,9999999999,1)")
        migrate_stage2(ledger)
        self.assertEqual(migrate(self.config)['schema'], 3)
        with ledger._connect() as db:
            self.assertEqual(db.execute('SELECT used,submission_limit FROM purchases').fetchone(), (1, 2))

    def test_migration_and_backup_refuse_running_scheduler(self):
        self.provision()
        app = create_app(replace(self.config, queue_enabled=True), verifier=self.verifier)
        with TestClient(app):
            with self.assertRaises(BlockingIOError):
                migrate(self.config)
            with self.assertRaises(BlockingIOError):
                create_backup(app.state.durable_queue.storage, self.base / 'backup', max_bytes=8_000_000)

    def test_closed_service_boots_without_provisioning(self):
        self.assertTrue(closed_check(self.config)['closed'])
        self.assertFalse(self.config.data_dir.exists())

    def test_closed_service_with_storage_still_denies_purchase_and_tier_claims(self):
        self.provision()
        app = create_app(self.config)
        with TestClient(app) as client:
            for tier in ('tier1', 'tier2', 'tier3'):
                result = client.post('/upload', headers={'Authorization': 'Bearer invented', 'X-Purchase-ID': 'unknown'}, data={'package': tier})
                self.assertEqual(result.status_code, 503)
            self.assertEqual(client.get('/docs').status_code, 404)
            self.assertEqual(client.get('/static/quote_upload.js').status_code, 200)
        self.assertFalse(hasattr(app.state, 'entitlements'))
        self.assertFalse(hasattr(app.state, 'durable_queue'))

    def test_queue_enable_flag_without_verifier_cannot_enable_paid_access(self):
        self.provision()
        with self.assertRaises(DeploymentError):
            create_app(replace(self.config, queue_enabled=True))

    def test_verifier_cannot_expose_legacy_nonqueued_submission_path(self):
        self.provision()
        with self.assertRaises(DeploymentError):
            create_app(self.config, verifier=self.verifier)

    def test_enabled_queue_missing_storage_fails_before_startup(self):
        with self.assertRaises(DeploymentError):
            create_app(replace(self.config, queue_enabled=True), verifier=self.verifier)
        self.assertFalse(self.config.data_dir.exists())

    def test_factory_startup_shutdown_and_report_persistence(self):
        ledger, _, storage = self.provision()
        purchase = VerifiedPurchase(purchase_id='synthetic', owner_customer_id='owner', authenticated_customer_id='owner',
            tier='tier1', payment_eligible=True, expires_at=int(time.time()) + 3600, submission_limit=1)
        self.purchases['synthetic'] = purchase
        async def process(manifest, files, grant):
            grant.consume()
            grant.store.complete(grant.job_id, grant.purchase, grant.attempt,
                '{"synthetic":true}', '<p>Synthetic report</p>', manifest['customer_name'], 1)
        app = create_app(replace(self.config, queue_enabled=True), verifier=self.verifier, processor=process)
        headers = {'Authorization': 'Bearer proof-synthetic', 'X-Purchase-ID': 'synthetic', 'Idempotency-Key': 'synthetic-deploy-key'}
        with TestClient(app) as client:
            self.assertTrue(app.state.durable_queue.ready)
            self.assertEqual(len(app.state.durable_queue._threads), 3)
            response = client.post('/analyze', headers=headers, json={'customerName': 'Synthetic', 'customerEmail': 'test@example.invalid',
                'files': [{'fileName': 'quote.txt', 'extractedText': 'Replace a failed part for $300. Verify operation.'}]})
            self.assertEqual(response.status_code, 202, response.text)
            job = response.json()['job_id']
            deadline = time.monotonic() + 5
            while client.get('/jobs/' + job, headers=headers).json()['state'] != 'completed' and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(client.get('/jobs/' + job + '/report', headers=headers).text, '<p>Synthetic report</p>')
        self.assertTrue(all(not thread.is_alive() for thread in app.state.durable_queue._threads))
        with stopped_queue(ledger):
            pass
        self.assertEqual(verify(self.config)['job_counts'], {'completed': 1})

    def test_readiness_does_not_wait_for_upload_lock(self):
        self.provision()
        app = create_app(replace(self.config, queue_enabled=True), verifier=self.verifier)
        with TestClient(app) as client:
            storage = app.state.durable_queue.storage
            with storage._locked():
                started = time.monotonic()
                response = local_readiness(app)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(client.get('/health/live').status_code, 200)
            self.assertEqual(client.get('/health/ready').status_code, 200)

    def test_readiness_does_not_compete_with_normal_sqlite_writer(self):
        self.provision()
        app = create_app(replace(self.config, queue_enabled=True), verifier=self.verifier)
        with TestClient(app), app.state.entitlements.ledger._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            response = local_readiness(app)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(response.status_code, 200)

    def test_readiness_bounds_exclusive_sqlite_lock_wait(self):
        self.provision()
        app = create_app(replace(self.config, queue_enabled=True), verifier=self.verifier)
        with TestClient(app), app.state.entitlements.ledger._connect() as db:
            db.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            response = local_readiness(app)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(response.status_code, 503)

    def test_cli_initialization_verification_and_retention_preview(self):
        self.assertEqual(self.cli('init')[0], 0)
        self.assertEqual(self.cli('verify')[1]['schema'], 3)
        code, report = self.cli('retention-preview')
        self.assertEqual(code, 0)
        self.assertTrue(report['dry_run'])
        self.assertEqual(self.cli('closed-check')[0], 0)

    def test_cli_requires_closed_confirmation_and_sanitizes_errors(self):
        code, result = self.cli('migrate')
        self.assertEqual(code, 1)
        self.assertNotIn(str(self.base), json.dumps(result))
        self.assertEqual(self.cli('init')[0], 0)
        self.assertEqual(self.cli('init')[0], 1)

    def test_cli_backup_restore_stays_fenced(self):
        self.provision()
        snapshot, restored = self.base / 'snapshot', self.base / 'restored'
        self.assertEqual(self.cli('backup', '--confirm-closed', '--destination', str(snapshot), '--max-bytes', '8000000')[0], 0)
        code, result = self.cli('restore', '--confirm-closed', '--snapshot', str(snapshot), '--destination', str(restored), '--max-bytes', '8000000')
        self.assertEqual(code, 0, result)
        self.assertTrue(result['reconciliation_required'])
        with self.assertRaises(sqlite3.Error):
            open_storage(replace(self.config, data_dir=restored))

    def test_cli_synthetic_mode_cannot_target_production_paths(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = operator(['init', '--synthetic', '--disk-mount', '/var/data', '--data-dir', '/var/data/cyt'])
        self.assertEqual(code, 1)

    def test_blueprint_keeps_deployment_closed_single_process_and_persistent(self):
        # JSON is a YAML subset, so safety-critical config can be tested without
        # introducing a new application dependency just to parse the Blueprint.
        document = json.loads(Path(__file__).with_name('render.yaml').read_text())
        self.assertEqual(set(document), {'services'})
        self.assertEqual(len(document['services']), 1)
        service = document['services'][0]
        self.assertEqual(service['maintenanceMode'], {'enabled': True})
        self.assertEqual(service['autoDeployTrigger'], 'off')
        self.assertEqual(service['numInstances'], 1)
        self.assertEqual(service['healthCheckPath'], '/health/live')
        self.assertEqual(service['disk']['mountPath'], '/var/data')
        self.assertEqual(service['disk']['sizeGB'], 10)
        env = {item['key']: item['value'] for item in service['envVars']}
        self.assertEqual(env['CYT_QUEUE_ENABLED'], 'false')
        self.assertEqual(env['EMAIL_APP_PASSWORD'], '')
        self.assertEqual(env['OPENAI_API_KEY'], 'closed-deployment-placeholder')
        self.assertEqual(env['PYTHON_DOTENV_DISABLED'], '1')
        self.assertEqual(env['PYTHON_VERSION'], Path(__file__).with_name('.python-version').read_text().strip())
        args = shlex.split(shlex.split(service['startCommand'])[2].split('exec ', 1)[1])
        self.assertEqual(args[args.index('--workers') + 1], '1')
        self.assertIn('deployment_app:create_app', args)
        self.assertIn('--factory', args)
        self.assertNotIn('--reload', args)
        self.assertIn('--require-hashes', service['buildCommand'])
        self.assertIn('--no-build-isolation', service['buildCommand'])


if __name__ == '__main__':
    unittest.main()
