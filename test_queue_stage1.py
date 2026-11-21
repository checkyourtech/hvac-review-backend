"""Synthetic offline Stage 1 security, durability and transaction regressions."""
from concurrent.futures import ThreadPoolExecutor
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from durable_uploads import PrivateUploadStore, UploadSource
from purchase_security import AuthorizedPurchase, SQLitePurchaseLedger, VerifiedPurchase
from queue_inputs import SQLiteQueuedInputs
from report_jobs import SQLiteReportStore
from sqlite_migrations import migrate_stage1


def groups(*indexes):
    return [{'pages': [{'file': i, 'rotation': 0} for i in group]} for group in indexes]


class QueueStage1Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cyt-stage1-test-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.ledger = SQLitePurchaseLedger.initialize(self.base / 'ledger.sqlite3')
        self.purchase = VerifiedPurchase(purchase_id='synthetic-order', owner_customer_id='customer-a',
            authenticated_customer_id='customer-a', tier='tier1', payment_eligible=True,
            expires_at=int(time.time()) + 3600, submission_limit=1)
        self.auth = self.authorize(self.purchase)
        self.store = PrivateUploadStore.provision(self.base / 'inputs', self.ledger,
            max_bytes=32 * 1024 * 1024, reserve_bytes=0)
        self.records = self.store.records

    def authorize(self, purchase):
        self.ledger.register(purchase)
        return AuthorizedPurchase(purchase, self.ledger)

    def sql(self, statement, values=()):
        with self.ledger._connect() as db:
            return db.execute(statement, values).fetchall()

    def save(self, *, key='synthetic-key-0001', contents=(b'Synthetic repair quote, total $300.',),
             filenames=None, grouping=None, auth=None):
        filenames = filenames or [f'proposal-{i}.txt' for i in range(len(contents))]
        sources = [UploadSource(name, 'application/octet-stream', io.BytesIO(data))
                   for name, data in zip(filenames, contents)]
        return self.store.save(auth or self.auth, key, sources, grouping or groups(list(range(len(sources)))),
            customer_name='Synthetic Customer', customer_email='synthetic@example.invalid')

    def reference(self, row):
        return self.records.inputs(self.auth, row['job_id'])

    def bundle(self, row):
        return self.store.root / self.reference(row)['bundle_id']

    def counts(self):
        return (self.sql('SELECT used FROM purchases WHERE purchase_id=?', (self.purchase.purchase_id,))[0][0],
                self.sql("SELECT COUNT(*) FROM review_holds WHERE state='active'")[0][0],
                self.sql('SELECT COUNT(*) FROM submissions')[0][0])

    def assert_status(self, code, fn):
        with self.assertRaises(HTTPException) as caught:
            fn()
        self.assertEqual(caught.exception.status_code, code)

    def test_durable_reopen_preserves_order_metadata_and_raw_bytes(self):
        row = self.save(contents=(b'Page one repair scope', b'Page two pricing'), grouping=groups([1, 0]))
        reopened = PrivateUploadStore(self.store.root, SQLitePurchaseLedger(self.ledger.path), max_bytes=2**25, reserve_bytes=0)
        auth = AuthorizedPurchase(self.purchase, reopened.records.ledger)
        manifest, files = reopened.load(auth, row['job_id'])
        self.assertEqual([i['filename'] for i in manifest['quotes'][0]], ['proposal-1.txt', 'proposal-0.txt'])
        self.assertEqual(files['0000.bin'], b'Page one repair scope')
        self.assertEqual(manifest['quotes'][0][0]['pages'], [1])
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_permissions_and_immutable_manifest(self):
        row = self.save()
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.bundle(row).stat().st_mode & 0o777, 0o700)
        for file in self.bundle(row).iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o400)
        self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)

    def test_modified_document_hash_is_rejected(self):
        row = self.save()
        path = self.bundle(row) / '0000.bin'
        path.chmod(0o600)
        path.write_bytes(b'Synthetic tampered quote')
        self.assert_status(422, lambda: self.store.load(self.auth, row['job_id']))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_modified_manifest_is_rejected(self):
        row = self.save()
        path = self.bundle(row) / 'manifest.json'
        path.chmod(0o600)
        path.write_text('{}')
        self.assert_status(422, lambda: self.store.load(self.auth, row['job_id']))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_missing_document_is_rejected_without_empty_reconstruction(self):
        row = self.save()
        (self.bundle(row) / '0000.bin').unlink()
        self.assert_status(422, lambda: self.store.load(self.auth, row['job_id']))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_filename_traversal_rejected_before_registration(self):
        for name in ['../quote.txt', '/quote.txt', 'folder\\quote.txt']:
            with self.subTest(name=name):
                self.assert_status(400, lambda: self.save(filenames=[name]))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_file_symlink_and_hardlink_rejected(self):
        row = self.save()
        target = self.base / 'external.txt'
        target.write_bytes(b'Synthetic outside data')
        target.chmod(0o600)
        path = self.bundle(row) / '0000.bin'
        path.unlink()
        path.symlink_to(target)
        self.assert_status(503, lambda: self.store.load(self.auth, row['job_id']))
        path.unlink()
        os.link(target, path)
        self.assert_status(503, lambda: self.store.load(self.auth, row['job_id']))
        self.assertEqual(target.read_bytes(), b'Synthetic outside data')

    def test_root_and_parent_symlinks_rejected(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.store.root, target_is_directory=True)
        self.assert_status(503, lambda: PrivateUploadStore(alias, self.ledger, max_bytes=1000))
        parent = self.base / 'parent-alias'
        parent.symlink_to(self.base, target_is_directory=True)
        self.assert_status(503, lambda: PrivateUploadStore(parent / 'inputs', self.ledger, max_bytes=1000))

    def test_root_replacement_and_permission_change_rejected(self):
        self.store.root.chmod(0o755)
        self.assert_status(503, self.save)
        self.store.root.chmod(0o700)
        old = self.store.root.with_name('disconnected')
        self.store.root.rename(old)
        self.store.root.mkdir(mode=0o700)
        self.assert_status(503, self.save)

    def test_missing_storage_never_auto_created(self):
        missing = self.base / 'missing'
        self.assert_status(503, lambda: PrivateUploadStore(missing, self.ledger, max_bytes=1000))
        self.assertFalse(missing.exists())

    def test_storage_binding_prevents_wrong_database_cleanup(self):
        other = SQLitePurchaseLedger.initialize(self.base / 'other.sqlite3')
        self.assert_status(503, lambda: PrivateUploadStore(self.store.root, other, max_bytes=1000))

    def test_public_or_relative_storage_is_rejected(self):
        for path in [Path('relative-inputs'), Path(__file__).parent / 'static' / 'inputs']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                PrivateUploadStore.provision(path, self.ledger, max_bytes=1000)

    def test_duplicate_key_and_content_reuses_one_hold(self):
        first = self.save()
        second = self.save()
        third = self.save(key='synthetic-key-0002')
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(first['job_id'], third['job_id'])
        self.assertEqual(self.counts(), (0, 1, 0))
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_inputs')[0][0], 1)

    def test_same_key_different_content_is_conflict(self):
        first = self.save()
        self.assert_status(409, lambda: self.save(contents=(b'Different synthetic proposal',)))
        self.assertEqual(self.counts(), (0, 1, 0))
        self.store.load(self.auth, first['job_id'])

    def test_twenty_concurrent_unique_holds_cannot_oversell(self):
        # Independent connections and distinct store objects; filesystem lock is
        # not the entitlement protection. Direct DB registration races below.
        def reserve(i):
            try:
                return self.records.register_inputs(self.auth, f'synthetic-key-{i:04}', f'synthetic-{i}',
                    self.store.root_id, f'{i:032x}', 'a' * 64, 100, 1)['job_id']
            except HTTPException as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(reserve, range(20)))
        self.assertEqual(sum(isinstance(v, str) for v in results), 1)
        self.assertEqual(results.count(409), 19)
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_concurrent_duplicate_registration_reuses_hold(self):
        def reserve(_):
            return self.records.register_inputs(self.auth, 'synthetic-key-0001', 'same-fingerprint',
                self.store.root_id, 'a' * 32, 'b' * 64, 100, 1)['job_id']
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(reserve, range(20)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_consumed_reviews_plus_holds_respect_larger_allowance(self):
        purchase = self.purchase.model_copy(update={'purchase_id': 'three-reviews', 'submission_limit': 3})
        auth = self.authorize(purchase)
        self.ledger.consume(purchase)
        for index in range(2):
            self.save(auth=auth, key=f'allowance-key-{index:04}', contents=(f'Synthetic scope {index}'.encode(),))
        self.assertEqual(self.ledger.register(purchase), 0)
        self.assert_status(409, lambda: self.save(auth=auth, key='allowance-key-0003', contents=(b'Extra scope',)))
        self.assert_status(409, lambda: self.ledger.consume(purchase))

    def test_independent_store_instances_serialize_storage_and_duplicates(self):
        second = PrivateUploadStore(self.store.root, self.ledger, max_bytes=2**25, reserve_bytes=0)
        def save(store):
            return store.save(self.auth, 'concurrent-key-0001',
                [UploadSource('same.txt', 'text/plain', io.BytesIO(b'Synthetic same proposal'))], groups([0]),
                customer_name='Synthetic', customer_email='synthetic@example.invalid')
        with ThreadPoolExecutor(max_workers=2) as executor:
            rows = list(executor.map(save, [self.store, second]))
        self.assertEqual(rows[0]['job_id'], rows[1]['job_id'])
        self.store.load(self.auth, rows[0]['job_id'])
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_storage_loss_preserves_hold_until_input_validity_is_known(self):
        row = self.save()
        original = self.store.root.with_name('disconnected-inputs')
        self.store.root.rename(original)
        self.assert_status(503, lambda: self.store.load(self.auth, row['job_id']))
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_corrupt_inputs_never_refund_consumed_review(self):
        row = self.save()
        attempt = self.records.claim(row['job_id'], self.purchase, False)
        self.records.consume(row['job_id'], self.purchase, attempt)
        (self.bundle(row) / '0000.bin').unlink()
        self.assert_status(422, lambda: self.store.load(self.auth, row['job_id']))
        self.assertEqual(self.counts(), (1, 0, 1))

    def test_release_rolls_back_when_job_failure_cannot_be_saved(self):
        row = self.save()
        self.sql("""CREATE TRIGGER reject_failure BEFORE UPDATE OF state ON review_jobs
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        self.assert_status(503, lambda: self.records.release(self.auth, row['job_id']))
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_active_hold_blocks_legacy_consumption_and_new_job(self):
        self.save()
        self.assert_status(409, lambda: self.ledger.consume(self.purchase))
        self.assert_status(409, lambda: SQLiteReportStore(self.ledger).create(self.purchase, 'other', 'other'))
        self.assertEqual(self.ledger.register(self.purchase), 0)

    def test_atomic_conversion_and_retry_consumes_once(self):
        row = self.save()
        attempt = self.records.claim(row['job_id'], self.purchase, False)
        reservation = self.records.consume(row['job_id'], self.purchase, attempt)
        self.assertEqual(self.counts(), (1, 0, 1))
        self.records.fail(row['job_id'], self.purchase, attempt, 'timeout', 504)
        retry = self.records.claim(row['job_id'], self.purchase, True)
        self.assertEqual(self.records.consume(row['job_id'], self.purchase, retry), reservation)
        self.assertEqual(self.counts(), (1, 0, 1))

    def test_conversion_rollback_preserves_hold_on_job_update_failure(self):
        row = self.save()
        attempt = self.records.claim(row['job_id'], self.purchase, False)
        self.sql("""CREATE TRIGGER reject_reservation BEFORE UPDATE OF reservation ON review_jobs
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        self.assert_status(503, lambda: self.records.consume(row['job_id'], self.purchase, attempt))
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_release_invalid_pending_input_is_idempotent(self):
        row = self.save()
        self.records.release(self.auth, row['job_id'])
        self.records.release(self.auth, row['job_id'])
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertIsNone(self.records.claim(row['job_id'], self.purchase, True))
        self.save(key='replacement-key-01', contents=(b'Replacement readable proposal',))
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_release_never_refunds_consumed_or_active_job(self):
        row = self.save()
        attempt = self.records.claim(row['job_id'], self.purchase, False)
        self.assert_status(409, lambda: self.records.release(self.auth, row['job_id']))
        self.records.consume(row['job_id'], self.purchase, attempt)
        self.records.fail(row['job_id'], self.purchase, attempt, 'timeout', 504)
        self.assert_status(409, lambda: self.records.release(self.auth, row['job_id']))
        self.assertEqual(self.counts(), (1, 0, 1))

    def test_purchase_revocation_blocks_saved_job_conversion(self):
        row = self.save()
        self.ledger.revoke(self.purchase.purchase_id)
        attempt = self.records.claim(row['job_id'], self.purchase, False)
        self.assert_status(409, lambda: self.records.consume(row['job_id'], self.purchase, attempt))
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_customer_isolation_and_untrusted_auth_rejected(self):
        row = self.save()
        other = self.purchase.model_copy(update={'purchase_id': 'other', 'owner_customer_id': 'b', 'authenticated_customer_id': 'b'})
        auth = self.authorize(other)
        self.assert_status(404, lambda: self.store.load(auth, row['job_id']))
        self.assert_status(404, lambda: self.records.release(auth, row['job_id']))
        self.assert_status(403, lambda: self.store.load(None, row['job_id']))
        spoof = AuthorizedPurchase(self.purchase.model_copy(update={'authenticated_customer_id': 'b'}), self.ledger)
        self.assert_status(403, lambda: self.store.load(spoof, row['job_id']))

    def test_tier1_multi_page_is_one_quote_and_two_quotes_rejected(self):
        self.save(contents=(b'Page A scope', b'Page B price'))
        self.assert_status(400, lambda: self.save(contents=(b'Quote A', b'Quote B'), grouping=groups([0], [1])))

    def test_tier2_three_quotes_and_four_quote_rejection(self):
        auth = self.authorize(self.purchase.model_copy(update={'purchase_id': 'tier-two', 'tier': 'tier2'}))
        row = self.save(auth=auth, contents=(b'Quote A', b'Quote B', b'Quote C'), grouping=groups([0], [1], [2]))
        manifest, _ = self.store.load(auth, row['job_id'])
        self.assertEqual(len(manifest['quotes']), 3)
        self.assert_status(400, lambda: self.save(auth=auth, contents=(b'A', b'B', b'C', b'D'), grouping=groups([0], [1], [2], [3])))

    def test_tier3_remains_closed(self):
        auth = self.authorize(self.purchase.model_copy(update={'purchase_id': 'tier-three', 'tier': 'tier3'}))
        self.assert_status(503, lambda: self.save(auth=auth))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_corrupt_or_empty_upload_creates_no_hold(self):
        for filename, data in [('bad.pdf', b'%PDF-corrupt'), ('empty.txt', b''), ('bad.png', b'not PNG')]:
            with self.subTest(filename=filename), self.assertRaises(HTTPException):
                self.save(filenames=[filename], contents=(data,))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_total_capacity_limit_and_disk_full_are_fail_closed(self):
        self.store.max_bytes = 5
        self.assert_status(507, self.save)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.store.max_bytes = 2**25
        with patch('durable_uploads.os.fsync', side_effect=OSError(errno.ENOSPC, 'synthetic disk full')):
            self.assert_status(503, self.save)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_storage_headroom_and_file_limit(self):
        self.store.reserve_bytes = 2**60
        self.assert_status(507, self.save)
        self.store.reserve_bytes = 0
        self.assert_status(413, lambda: self.save(contents=(b'x' * (10 * 1024 * 1024 + 1),)))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_combined_quote_text_limit_includes_page_separators(self):
        self.assert_status(413, lambda: self.save(contents=(b'a' * 50000, b'b' * 50000)))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_interrupted_stream_leaves_no_hold_and_orphan_is_recoverable(self):
        class Interrupted(io.BytesIO):
            def read(self, size):
                raise OSError('synthetic interruption')
        self.assert_status(503, lambda: self.store.save(self.auth, 'synthetic-key-0001',
            [UploadSource('proposal.txt', 'text/plain', Interrupted())], groups([0]),
            customer_name='Synthetic', customer_email='synthetic@example.invalid'))
        self.assertEqual(self.counts(), (0, 0, 0))
        self.age_bundles()
        self.assertEqual(self.store.cleanup_orphans(), 1)

    def age_bundles(self):
        for directory in self.store.root.iterdir():
            if directory.is_dir():
                os.utime(directory, (time.time() - 90000,) * 2)

    def test_db_failure_leaves_unreferenced_final_files_for_safe_cleanup(self):
        self.sql("""CREATE TRIGGER reject_hold BEFORE INSERT ON review_holds
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        self.assert_status(503, self.save)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM review_jobs')[0][0], 0)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.age_bundles()
        self.assertEqual(self.store.cleanup_orphans(), 1)

    def test_lost_response_reuses_registration_and_cleanup_keeps_reference(self):
        original = self.records.register_inputs
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('synthetic response lost')
        with patch.object(self.records, 'register_inputs', side_effect=lost):
            self.assert_status(503, self.save)
        row = self.save()
        self.age_bundles()
        self.assertEqual(self.store.cleanup_orphans(), 1)
        self.store.load(self.auth, row['job_id'])
        self.records.release(self.auth, row['job_id'])
        self.assertEqual(self.store.cleanup_orphans(), 0)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_cleanup_never_follows_symlink_or_deletes_target(self):
        target = self.base / 'outside'
        target.mkdir()
        (target / 'keep').write_text('synthetic')
        (self.store.root / ('a' * 32)).symlink_to(target, target_is_directory=True)
        self.assert_status(503, self.store.cleanup_orphans)
        self.assertTrue((target / 'keep').exists())

    def test_manifest_has_only_allowlisted_metadata(self):
        row = self.save()
        manifest, _ = self.store.load(self.auth, row['job_id'])
        self.assertEqual(set(manifest), {'version', 'customer_name', 'customer_email', 'quotes'})
        encoded = json.dumps(manifest)
        for field in ['Authorization', 'bearer_token', 'api_key', 'authenticated_customer_id']:
            self.assertNotIn(field, encoded)

    def test_invalid_key_and_grouping_do_not_write_documents(self):
        self.assert_status(400, lambda: self.save(key='short'))
        self.assert_status(400, lambda: self.save(contents=(b'One', b'Two'), grouping=groups([0], [0])))
        self.assertEqual({p.name for p in self.store.root.iterdir()}, {'.lock', 'store.json'})

    def test_heic_grouping_preserves_rotation_and_photo_duplicate_rejection(self):
        from test_image_intake import photo
        data = photo(format='HEIF')
        other = photo(format='HEIF', marker='Second synthetic page')
        grouping = groups([1, 0])
        grouping[0]['pages'][0]['rotation'] = 90
        row = self.save(contents=(data, other), filenames=['first.heic', 'second.HEIC'], grouping=grouping)
        manifest, _ = self.store.load(self.auth, row['job_id'])
        self.assertEqual(len(manifest['quotes']), 1)
        self.assertEqual(manifest['quotes'][0][0]['rotation'], 90)
        self.assert_status(400, lambda: self.save(contents=(data, data), filenames=['a.heic', 'b.HEIC']))

    def test_bundle_count_bounds_empty_interrupted_uploads(self):
        self.store.max_bundles = 1
        self.assert_status(422, lambda: self.save(contents=(b'',)))
        self.assert_status(507, self.save)
        self.age_bundles()
        self.assertEqual(self.store.cleanup_orphans(), 1)
        self.save()

    def test_stage1_requires_explicit_migration_for_legacy_database(self):
        with sqlite3.connect(self.ledger.path) as db:
            for table in ['review_holds', 'review_inputs', 'queue_storage']:
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=0')
        ledger = SQLitePurchaseLedger(self.ledger.path)
        self.assert_status(503, lambda: SQLiteQueuedInputs(ledger))
        self.assertEqual(self.sql('PRAGMA user_version')[0][0], 0)
        migrate_stage1(ledger)
        SQLiteQueuedInputs(ledger)
        indexes = {row[0] for row in self.sql("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('review_holds_purchase_state', indexes)
        self.assertIn('review_inputs_created', indexes)

    def test_supported_real_formats_pass_existing_decoder_and_preserve_originals(self):
        from test_document_intake import pdf, docx
        from test_image_intake import photo, scan
        cases = [('proposal.pdf', pdf()), ('proposal.docx', docx()),
                 ('proposal.JPG', photo()), ('proposal.jpeg', photo(marker='JPEG')),
                 ('proposal.png', photo(format='PNG')), ('proposal.HEIC', photo(format='HEIF')),
                 ('scan.pdf', scan())]
        for i, (name, data) in enumerate(cases):
            with self.subTest(name=name):
                auth = self.authorize(self.purchase.model_copy(update={'purchase_id': f'format-{i}'}))
                row = self.save(auth=auth, filenames=[name], contents=(data,))
                manifest, files = self.store.load(auth, row['job_id'])
                self.assertEqual(files['0000.bin'], data)
                self.assertEqual(manifest['quotes'][0][0]['filename'], name)

    def test_existing_report_payload_survives_migration_and_holds(self):
        # A synthetic version-zero copy exercises the actual additive upgrade.
        legacy = self.base / 'legacy.sqlite3'
        with sqlite3.connect(self.ledger.path) as source, sqlite3.connect(legacy) as target:
            source.backup(target)
        legacy.chmod(0o600)
        with sqlite3.connect(legacy) as db:
            for table in ['review_holds', 'review_inputs', 'queue_storage']:
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=0')
        ledger = SQLitePurchaseLedger(legacy)
        store = SQLiteReportStore(ledger)
        row = store.create(self.purchase, 'legacy-key', 'legacy-content')
        attempt = store.claim(row['job_id'], self.purchase, False)
        reservation = store.consume(row['job_id'], self.purchase, attempt)
        store.complete(row['job_id'], self.purchase, attempt, '{"synthetic":true}', '<p>Synthetic</p>', 'Synthetic', 1)
        before = store.get(row['job_id'], self.purchase)
        migrate_stage1(ledger)
        migrate_stage1(ledger)
        self.assertEqual(store.get(row['job_id'], self.purchase), before)
        with ledger._connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT submission_id FROM submissions').fetchone()[0], reservation)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_unknown_or_missing_schema_fails_without_repair(self):
        with sqlite3.connect(self.ledger.path) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(sqlite3.Error):
            SQLitePurchaseLedger(self.ledger.path)
        with self.assertRaises(sqlite3.Error):
            migrate_stage1(self.ledger)
        with sqlite3.connect(self.ledger.path) as db:
            db.execute('PRAGMA user_version=1')
            db.execute('DROP TABLE review_holds')
        with self.assertRaises(sqlite3.Error):
            SQLitePurchaseLedger(self.ledger.path)

    def test_migration_failure_rolls_back_every_new_object(self):
        with sqlite3.connect(self.ledger.path) as db:
            for table in ['review_holds', 'review_inputs', 'queue_storage']:
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=0')
        import sqlite_migrations
        with patch.object(sqlite_migrations, 'STAGE1_SCHEMA', (*sqlite_migrations.STAGE1_SCHEMA, 'INVALID SQL')):
            with self.assertRaises(sqlite3.Error):
                migrate_stage1(self.ledger)
        with self.ledger._connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='review_inputs'").fetchone())
        migrate_stage1(self.ledger)


if __name__ == '__main__':
    unittest.main()
