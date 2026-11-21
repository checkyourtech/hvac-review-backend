"""Offline operational tests using synthetic databases, identities and transports."""
import contextlib
import io
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from openai import APITimeoutError, RateLimitError

import main
import report_jobs
import runtime_safety
import test_purchase_security as security
from purchase_security import EntitlementService, SQLitePurchaseLedger


class PrivateStorageTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="cyt-private-storage-")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "private.sqlite3"

    def test_startup_never_creates_a_missing_database(self):
        with self.assertRaises(sqlite3.Error):
            SQLitePurchaseLedger(self.path)
        self.assertFalse(self.path.exists())

    def test_explicit_provisioning_creates_owner_only_database(self):
        SQLitePurchaseLedger.initialize(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        SQLitePurchaseLedger(self.path)

    def test_provisioning_never_overwrites_an_existing_ledger(self):
        SQLitePurchaseLedger.initialize(self.path)
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            SQLitePurchaseLedger.initialize(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_lost_volume_during_requests_does_not_create_fresh_entitlements(self):
        ledger = SQLitePurchaseLedger.initialize(self.path)
        self.path.rename(self.path.with_suffix(".disconnected"))
        with self.assertRaises(sqlite3.Error), ledger._connect():
            self.fail("Missing database was opened")
        self.assertFalse(self.path.exists())

    def test_empty_database_is_not_automatically_repaired(self):
        self.path.touch(mode=0o600)
        with self.assertRaises(sqlite3.Error):
            SQLitePurchaseLedger(self.path)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_world_readable_database_is_rejected_without_changing_permissions(self):
        SQLitePurchaseLedger.initialize(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(sqlite3.Error):
            SQLitePurchaseLedger(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_symlink_database_is_rejected(self):
        SQLitePurchaseLedger.initialize(self.path)
        link = self.path.with_name("alias.sqlite3")
        link.symlink_to(self.path)
        with self.assertRaises(sqlite3.Error):
            SQLitePurchaseLedger(link)

    def test_public_static_storage_is_rejected_before_any_creation(self):
        path = Path(main.__file__).resolve().parent / "static" / "private.sqlite3"
        for method in (SQLitePurchaseLedger, SQLitePurchaseLedger.initialize):
            with self.subTest(method=method), self.assertRaises(ValueError):
                method(path)

    def test_relative_database_path_is_rejected(self):
        with self.assertRaises(ValueError):
            SQLitePurchaseLedger.initialize(Path("private.sqlite3"))

    def test_uri_characters_in_private_filename_are_escaped(self):
        path = self.path.with_name("private?#.sqlite3")
        ledger = SQLitePurchaseLedger.initialize(path)
        with ledger._connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))


class OperationalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.h = security.PurchaseSecurityTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.store = report_jobs.SQLiteReportStore(self.h.ledger)

    def test_liveness_exposes_no_configuration_and_default_readiness_is_closed(self):
        with TestClient(main.app) as client:
            self.assertEqual(client.get("/health/live").json(), {"status": "alive"})
            response = client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.h.verifier.verify.assert_not_called()

    def test_ready_checks_only_configured_local_storage_without_provider_calls(self):
        response = self.h.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})
        self.h.verifier.verify.assert_not_called()
        self.h.assert_no_paid_work()

    def test_readiness_does_not_initialize_missing_report_schema(self):
        with self.h.ledger._connect() as db:
            db.execute("DROP TABLE review_request_keys")
        self.assertEqual(self.h.client.get("/health/ready").status_code, 503)
        with self.h.ledger._connect() as db:
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='review_request_keys'").fetchall())

    def test_readiness_storage_error_is_sanitized(self):
        with patch.object(self.h.ledger, "_connect", side_effect=sqlite3.OperationalError("synthetic-private-path")):
            response = self.h.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_lost_storage_fails_purchase_closed_without_paid_calls(self):
        self.h.db_path.rename(self.h.db_path.with_suffix(".disconnected"))
        response = self.h.submit()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(str(self.h.db_path), response.text)
        self.assertFalse(self.h.db_path.exists())
        self.h.analysis.assert_not_called()
        self.h.classifier.assert_not_called()
        self.h.email.assert_not_called()

    def test_sqlite_backup_restores_report_ownership_usage_and_notification_state(self):
        result = self.h.submit()
        self.assertEqual(result.status_code, 200)
        job_id = result.headers["X-Review-Job-ID"]
        original = self.store.get(job_id, self.h.purchase)
        backup = self.h.directory / "synthetic-restore.sqlite3"
        os.close(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        with self.h.ledger._connect() as source, contextlib.closing(sqlite3.connect(str(backup))) as target:
            source.backup(target)
            self.assertEqual(target.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        restored = SQLitePurchaseLedger(backup)
        restored_store = report_jobs.SQLiteReportStore(restored)
        self.assertEqual(restored_store.get(job_id, self.h.purchase), original)
        self.h.app.state.entitlements = EntitlementService(self.h.verifier, restored)
        self.assertEqual(self.h.client.get(f"/jobs/{job_id}/result", headers=self.h.headers()).json(), result.json())
        self.assertEqual(restored.register(self.h.purchase), 0)
        self.assertEqual(self.h.submit().status_code, 409)
        self.h.purchase = self.h.make_purchase(purchase_id="another-order", owner_customer_id="another-owner",
                                              authenticated_customer_id="another-owner")
        self.assertEqual(self.h.client.get(f"/jobs/{job_id}/report", headers=self.h.headers()).status_code, 404)
        self.h.analysis.assert_called_once()
        self.h.email.assert_called_once()

    def test_sqlite_backup_preserves_interruption_fence(self):
        self.h.ledger.register(self.h.purchase)
        row = self.store.create(self.h.purchase, "synthetic-pending-key", "synthetic-fingerprint")
        attempt = self.store.claim(row["job_id"], self.h.purchase, False)
        self.store.consume(row["job_id"], self.h.purchase, attempt)
        with self.h.ledger._connect() as db:
            db.execute("UPDATE review_jobs SET deadline=0 WHERE job_id=?", (row["job_id"],))
        # Reopen after interruption; no proposal, model call or reservation replay.
        recovered = report_jobs.SQLiteReportStore(SQLitePurchaseLedger(self.h.db_path)).get(row["job_id"], self.h.purchase)
        self.assertEqual(recovered["error_code"], "interrupted")
        self.assertFalse(recovered["retryable"])
        self.assertEqual(self.h.usage(), 1)
        self.h.analysis.assert_not_called()


class RuntimePrivacyTests(unittest.TestCase):
    def test_sensitive_runtime_files_are_ignored_but_source_is_not(self):
        private = [".env.production", "uploads/private.txt", "reports/customer.html", "private-data/key.json",
                   "purchases.sqlite3", "purchases.sqlite3-wal", "reports.db-shm", "credential.pem"]
        result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                                input="\n".join(private + ["main.py", "test_production_readiness.py"]) + "\n",
                                text=True, capture_output=True, check=False)
        self.assertEqual(set(result.stdout.splitlines()), set(private))

    def test_raw_dependency_and_access_logs_are_not_emitted_even_with_debug_enabled(self):
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        child = logging.getLogger("openai.synthetic_test")
        child.addHandler(handler)  # Simulate a preexisting verbose configuration.
        runtime_safety.configure_private_logging()
        for name in ("openai.synthetic_test", "httpx", "httpcore.synthetic_test", "uvicorn.access"):
            logger = logging.getLogger(name)
            old = logger.level
            try:
                logger.setLevel(logging.DEBUG)
                logger.debug("synthetic-document-and-token")
                logger.error("synthetic-document-and-token")
            finally:
                logger.setLevel(old)
        logging.getLogger("cyt.safe_events").warning("review_failed")
        self.assertEqual(captured.getvalue(), "review_failed\n")

    def test_analysis_timeout_is_bounded_and_sdk_retry_is_disabled(self):
        self.assertEqual(main.client.max_retries, 0)
        self.assertEqual(main.client.timeout.connect, 5)
        self.assertEqual(main.client.timeout.read, 120)
        self.assertEqual(main.client.timeout.write, 30)
        self.assertEqual(main.client.timeout.pool, 5)

    def test_provider_timeout_is_not_automatically_replayed(self):
        requests = []
        def respond(request):
            requests.append(request)
            raise httpx.ReadTimeout("synthetic-timeout", request=request)
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client = main.client.with_options(http_client=transport)
            with self.assertRaises(APITimeoutError):
                client.models.list()
        self.assertEqual(len(requests), 1)

    def test_rate_limit_is_not_automatically_replayed(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(429, json={"error": {"message": "synthetic limit", "type": "rate_limit"}})
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client = main.client.with_options(http_client=transport)
            with self.assertRaises(RateLimitError):
                client.models.list()
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
