"""Offline HTTP/transaction regression tests; providers are always substituted."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openai import APITimeoutError

import main
import report_jobs as jobs
import test_purchase_security as security
from purchase_security import AuthorizedPurchase, EntitlementService, SQLitePurchaseLedger
from test_blower_live_response import saved_response
from test_blower_support_normalization import BAD
from document_intake import BETTER_PHOTO


class ReportDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.h = security.PurchaseSecurityTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.store = jobs.SQLiteReportStore(self.h.ledger)
        self.key = "synthetic-review-key-0001"

    def headers(self, **extra):
        return self.h.headers(**{"Idempotency-Key": self.key, **extra})

    def post(self, **extra):
        return self.h.client.post("/analyze", headers=self.headers(**extra), json=self.h.payload())

    def row(self, response):
        return self.store.get(response.headers["X-Review-Job-ID"], self.h.purchase)

    def get(self, path):
        return self.h.client.get(path, headers=self.h.headers())

    def sql(self, command, args=()):
        with self.h.ledger._connect() as db:
            return db.execute(command, args).fetchall()

    def test_report_is_saved_before_notification_and_matches_html_json(self):
        def send(**kwargs):
            row = self.sql("SELECT state,result_json,report_html FROM review_jobs")[0]
            self.assertEqual(row[0], "completed")
            self.assertEqual(json.loads(row[1]), kwargs["analysis"].model_dump(mode="json"))
            self.assertEqual(row[2], kwargs["report_html"])
        self.h.email.side_effect = send
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row(response)
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["delivery_state"], "sent")
        html = self.get(f"/jobs/{row['job_id']}/report")
        result = self.get(f"/jobs/{row['job_id']}/result")
        self.assertEqual(result.json(), response.json())
        self.assertEqual(html.text, main.build_report_html(main.HVACAnalysis.model_validate(result.json()), 1))
        self.assertIn("window.print()", html.text)
        self.assertIn("no-store", html.headers["cache-control"])
        self.assertEqual(self.h.usage(), 1)

    def test_report_recovery_after_browser_closure_consumes_no_review(self):
        response = self.post()
        for _ in range(3):
            listing = self.get("/jobs").json()["jobs"]
            self.assertEqual(len(listing), 1)
            self.assertEqual(self.get(listing[0]["result_url"]).json(), response.json())
        self.h.analysis.assert_called_once()
        self.assertEqual(self.h.usage(), 1)
        self.assertEqual(self.get("/upload-page").status_code, 200)

    def test_retrieval_and_retry_require_authentication(self):
        job_id = self.row(self.post())["job_id"]
        for suffix in ("", "/report", "/result"):
            self.assertEqual(self.h.client.get(f"/jobs/{job_id}{suffix}").status_code, 401)
        self.assertEqual(self.h.client.get("/jobs").status_code, 401)
        self.assertEqual(self.h.client.post(f"/jobs/{job_id}/delivery-retry").status_code, 401)

    def test_other_customer_and_purchase_cannot_read_or_notify(self):
        job_id = self.row(self.post())["job_id"]
        for owner in ("customer-a", "customer-b"):
            self.h.purchase = self.h.make_purchase(purchase_id="order-" + owner,
                owner_customer_id=owner, authenticated_customer_id=owner)
            self.assertEqual(self.get("/jobs").json(), {"jobs": []})
            for suffix in ("", "/report", "/result"):
                self.assertEqual(self.get(f"/jobs/{job_id}{suffix}").status_code, 404)
            response = self.h.client.post(f"/jobs/{job_id}/delivery-retry", headers=self.h.headers())
            self.assertEqual(response.status_code, 404)
        self.h.email.assert_called_once()

    def test_retrieval_rechecks_revocation_expiry_and_disabled_tier(self):
        original = self.h.purchase
        job_id = self.row(self.post())["job_id"]
        self.h.purchase = original.model_copy(update={"payment_eligible": False})
        self.assertEqual(self.get(f"/jobs/{job_id}/report").status_code, 403)
        self.h.purchase = original
        self.assertEqual(self.get(f"/jobs/{job_id}/report").status_code, 403)
        self.h.purchase = original.model_copy(update={"tier": "tier3"})
        self.assertEqual(self.get("/jobs").status_code, 503)

    def test_duplicate_key_and_different_key_same_input_share_one_report(self):
        first = self.post()
        self.assertEqual(self.post().json(), first.json())
        self.key = "synthetic-alternate-key"
        second = self.post()
        self.assertEqual(second.headers["X-Review-Job-ID"], first.headers["X-Review-Job-ID"])
        self.assertEqual(self.h.usage(), 1)
        self.h.analysis.assert_called_once()
        self.h.email.assert_called_once()

    def test_reused_key_with_changed_input_rejected_before_analysis(self):
        self.post()
        response = self.h.client.post("/analyze", headers=self.headers(), json=self.h.payload(city="Different"))
        self.assertEqual(response.status_code, 409)
        self.h.analysis.assert_called_once()

    def test_concurrent_duplicates_have_single_execution(self):
        started, release = threading.Event(), threading.Event()
        def analysis(**kwargs):
            started.set()
            self.assertTrue(release.wait(5))
            return saved_response()
        self.h.analysis.side_effect = analysis
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.post)
            try:
                self.assertTrue(started.wait(5))
                second = self.post()
                self.assertEqual(second.status_code, 202)
            finally:
                release.set()
            completed = first.result(10)
        self.assertEqual(completed.headers["X-Review-Job-ID"], second.headers["X-Review-Job-ID"])
        self.h.analysis.assert_called_once()
        self.assertEqual(self.h.usage(), 1)

    def test_wait_budget_returns_status_then_recovers_without_reanalysis(self):
        started, release = threading.Event(), threading.Event()
        def analysis(**kwargs):
            started.set()
            release.wait(5)
            return saved_response()
        self.h.analysis.side_effect = analysis
        with patch.object(jobs, "WAIT_SECONDS", 0.02):
            response = self.post()
        try:
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["state"], "processing")
            self.assertEqual(self.get(f"/jobs/{response.json()['job_id']}/report").status_code, 409)
        finally:
            release.set()
        self.wait_completed(response.json()["job_id"])
        self.h.analysis.assert_called_once()

    def wait_completed(self, job_id):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            row = self.store.get(job_id, self.h.purchase)
            if row["state"] == "completed" and row["delivery_state"] != "pending":
                return row
            time.sleep(0.01)
        self.fail("Worker did not complete within the offline test deadline")

    def test_disconnected_browser_does_not_cancel_processing(self):
        started, release = threading.Event(), threading.Event()
        def analysis(**kwargs):
            started.set()
            release.wait(5)
            return saved_response()
        self.h.analysis.side_effect = analysis
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.h.app), base_url="http://offline") as client:
                request = asyncio.create_task(client.post("/analyze", headers=self.headers(), json=self.h.payload()))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    request.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await request
                finally:
                    release.set()
                job_id = self.sql("SELECT job_id FROM review_jobs")[0][0]
                await asyncio.to_thread(self.wait_completed, job_id)
                await asyncio.sleep(0)
                return job_id
        job_id = asyncio.run(scenario())
        self.assertEqual(self.get(f"/jobs/{job_id}/report").status_code, 200)
        self.assertEqual(self.h.usage(), 1)

    def test_notification_failure_does_not_fail_report(self):
        self.h.email.side_effect = RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_TEXT")
        response = self.post()
        self.assertEqual(response.status_code, 200)
        row = self.row(response)
        self.assertEqual((row["state"], row["delivery_state"]), ("completed", "failed"))
        self.assertNotIn("SYNTHETIC_PRIVATE_PROVIDER_TEXT", json.dumps(row))
        self.assertEqual(self.get(f"/jobs/{row['job_id']}/report").status_code, 200)

    def test_delivery_retry_uses_saved_report_never_analysis_or_quota(self):
        self.h.email.side_effect = RuntimeError("Notification failed")
        row = self.row(self.post())
        self.h.email.side_effect = None
        response = self.h.client.post(f"/jobs/{row['job_id']}/delivery-retry", headers=self.h.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notification"]["state"], "sent")
        self.assertEqual(self.h.email.call_args.kwargs["report_html"], row["report_html"])
        self.assertEqual(self.h.email.call_args.kwargs["analysis"].model_dump_json(), row["result_json"])
        self.h.analysis.assert_called_once()
        self.assertEqual(self.h.usage(), 1)
        self.h.client.post(f"/jobs/{row['job_id']}/delivery-retry", headers=self.h.headers())
        self.assertEqual(self.h.email.call_count, 2)

    def test_notification_retry_is_bounded(self):
        self.h.email.side_effect = RuntimeError("Notification failed")
        row = self.row(self.post())
        path = f"/jobs/{row['job_id']}/delivery-retry"
        self.assertEqual([self.h.client.post(path, headers=self.h.headers()).status_code for _ in range(3)], [200, 200, 409])
        self.assertEqual(self.h.email.call_count, 3)
        self.assertEqual(self.get(f"/jobs/{row['job_id']}/report").status_code, 200)

    def test_unconfigured_email_is_not_claimed_as_sent(self):
        self.h.email.return_value = False
        row = self.row(self.post())
        self.assertEqual((row["state"], row["delivery_state"]), ("completed", "unconfigured"))

    def test_concurrent_notification_retries_do_not_duplicate_send(self):
        self.h.email.side_effect = RuntimeError("First send failed")
        row = self.row(self.post())
        started, release = threading.Event(), threading.Event()
        def send(**kwargs):
            started.set()
            self.assertTrue(release.wait(5))
        self.h.email.side_effect = send
        path = f"/jobs/{row['job_id']}/delivery-retry"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.h.client.post, path, headers=self.h.headers())
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(self.h.client.post(path, headers=self.h.headers()).json()["notification"]["state"], "sending")
            finally:
                release.set()
            self.assertEqual(first.result(10).status_code, 200)
        self.assertEqual(self.h.email.call_count, 2)

    def test_completed_report_survives_new_app_and_ledger_instances(self):
        response = self.post()
        new_app = FastAPI()
        new_app.include_router(main.app.router)
        new_app.state.entitlements = EntitlementService(self.h.verifier, SQLitePurchaseLedger(self.h.db_path))
        with TestClient(new_app) as client:
            status = client.get("/jobs", headers=self.h.headers()).json()["jobs"][0]
            recovered = client.get(status["result_url"], headers=self.h.headers())
        self.assertEqual(recovered.json(), response.json())
        self.h.analysis.assert_called_once()

    def test_expired_worker_is_failed_not_automatically_replayed_and_late_publish_is_fenced(self):
        self.h.ledger.register(self.h.purchase)
        row = self.store.create(self.h.purchase, self.key, jobs.digest("abandoned"))
        attempt = self.store.claim(row["job_id"], self.h.purchase, False)
        self.store.consume(row["job_id"], self.h.purchase, attempt)
        self.sql("UPDATE review_jobs SET deadline=0 WHERE job_id=?", (row["job_id"],))
        restored = jobs.SQLiteReportStore(SQLitePurchaseLedger(self.h.db_path))
        status = restored.get(row["job_id"], self.h.purchase)
        self.assertEqual((status["state"], status["error_code"], status["retryable"]), ("failed", "interrupted", 0))
        self.assertIsNone(restored.claim(row["job_id"], self.h.purchase, True))
        with self.assertRaises(HTTPException):
            restored.complete(row["job_id"], self.h.purchase, attempt, "{}", "late report", "Offline", 1)
        self.assertEqual(self.h.usage(), 1)

    def test_crashed_notification_becomes_unknown_without_losing_report(self):
        row = self.row(self.post())
        self.sql("UPDATE review_jobs SET delivery_state='sending',delivery_deadline=0 WHERE job_id=?", (row["job_id"],))
        restored = self.store.get(row["job_id"], self.h.purchase)
        self.assertEqual((restored["state"], restored["delivery_state"]), ("completed", "unknown"))
        self.assertTrue(jobs.public_status(restored)["notification"]["retry_allowed"])

    def test_model_timeout_is_safe_failure_with_one_reservation(self):
        self.h.analysis.side_effect = APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
        response = self.post()
        row = self.row(response)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(row["error_code"], "timeout")
        self.assertIsNone(row["result_json"])
        self.assertIsNone(row["report_html"])
        self.assertEqual(self.h.usage(), 1)
        self.h.email.assert_not_called()

    def test_length_exhaustion_is_recorded_without_incomplete_report(self):
        self.h.analysis.side_effect = jobs.ReviewResponseLimit()
        response = self.post()
        row = self.row(response)
        self.assertEqual((response.status_code, row["error_code"]), (502, "response_length"))
        self.assertIsNone(row["result_json"])
        self.assertEqual(self.get(f"/jobs/{row['job_id']}/result").status_code, 409)
        self.h.email.assert_not_called()

    def test_explicit_failed_analysis_retry_reuses_reservation(self):
        self.h.analysis.side_effect = RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_TEXT")
        failed = self.post()
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn("SYNTHETIC_PRIVATE_PROVIDER_TEXT", failed.text)
        self.assertEqual(self.post().status_code, 500)
        self.assertEqual(self.h.analysis.call_count, 1)
        self.h.analysis.side_effect = lambda **kwargs: saved_response()
        recovered = self.post(**{"X-Review-Retry": "true"})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.headers["X-Review-Job-ID"], failed.headers["X-Review-Job-ID"])
        self.assertEqual(self.h.usage(), 1)
        self.assertEqual(self.row(recovered)["attempts"], 2)

    def test_failed_analysis_retries_are_bounded(self):
        self.h.analysis.side_effect = RuntimeError("Failed")
        self.post()
        second = self.post(**{"X-Review-Retry": "true"})
        self.assertEqual(second.status_code, 500)
        self.post(**{"X-Review-Retry": "true"})
        self.assertEqual(self.h.analysis.call_count, 2)
        self.assertFalse(jobs.public_status(self.row(second))["retry_allowed"])
        self.assertEqual(self.h.usage(), 1)

    def test_report_storage_failure_never_sends_email(self):
        with patch.object(jobs.SQLiteReportStore, "complete", side_effect=sqlite3.OperationalError("PRIVATE DATABASE PATH")):
            response = self.post()
        row = self.row(response)
        self.assertEqual(response.status_code, 503)
        self.assertEqual((row["error_code"], row["retryable"]), ("storage_failed", 0))
        self.assertNotIn("PRIVATE DATABASE PATH", response.text)
        self.h.email.assert_not_called()

    def test_atomic_reservation_rolls_back_if_job_update_fails(self):
        self.h.ledger.register(self.h.purchase)
        row = self.store.create(self.h.purchase, self.key, "fingerprint")
        attempt = self.store.claim(row["job_id"], self.h.purchase, False)
        self.sql("""CREATE TRIGGER reject_reservation BEFORE UPDATE OF reservation ON review_jobs
            BEGIN SELECT RAISE(ABORT, 'synthetic rollback'); END""")
        with self.assertRaises(HTTPException) as error:
            self.store.consume(row["job_id"], self.h.purchase, attempt)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(self.h.usage(), 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM submissions")[0][0], 0)

    def test_report_render_failure_does_not_publish_partial_json(self):
        with patch.object(main, "build_report_html", side_effect=RuntimeError("Render failed")):
            response = self.post()
        row = self.row(response)
        self.assertEqual(row["state"], "failed")
        self.assertIsNone(row["result_json"])
        self.h.email.assert_not_called()

    def test_tier_limits_claims_and_tier3_still_fail_before_paid_analysis(self):
        for tier, count, claims, status in (("tier1", 2, {}, 400), ("tier2", 4, {}, 400),
                ("tier1", 1, {"package": "tier2"}, 403), ("tier3", 1, {}, 503)):
            with self.subTest(tier=tier, count=count):
                self.h.purchase = self.h.make_purchase(purchase_id=f"order-{tier}-{count}-{status}", tier=tier)
                response = self.h.client.post("/analyze", headers=self.headers(), json=self.h.payload(count=count, **claims))
                self.assertEqual(response.status_code, status)
        self.h.assert_no_paid_work()

    def test_tier2_three_quote_report_retains_comparison_and_no_vetting(self):
        self.h.purchase = self.h.make_purchase(tier="tier2")
        response = self.h.client.post("/analyze", headers=self.headers(), json=self.h.payload(count=3))
        self.assertEqual(response.status_code, 200)
        row = self.row(response)
        self.assertEqual(row["quote_count"], 3)
        self.assertEqual(response.json()["contractor_vetting"], "")
        self.assertEqual(row["report_html"], main.build_report_html(main.HVACAnalysis.model_validate(response.json()), 3))

    def test_upload_duplicates_ignore_multipart_boundaries_and_do_not_repeat_extraction(self):
        def upload():
            return self.h.upload(headers=self.headers())
        with patch.object(main, "prepare_document", wraps=main.prepare_document) as prepare:
            first, second = upload(), upload()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.text, first.text)
        prepare.assert_called_once()
        self.assertEqual(self.row(second)["quote_count"], 1)

    def test_mobile_pages_remain_one_quote_with_no_duplicate_transcription(self):
        fields = {"customer_name": "Offline", "customer_email": "offline@example.invalid",
                  "quote_groups": json.dumps([{"pages": [{"file": 0, "rotation": 0}, {"file": 1, "rotation": 0}]}])}
        files = [("files", (f"page-{i}.heic", f"synthetic bytes {i}".encode(), "application/octet-stream")) for i in range(2)]
        def prepare(data, *args):
            return [{"page": 1, "image": data.decode()}]
        # HEIC decoding itself is exercised by the existing real-format intake suite.
        with patch.object(main, "prepare_document", side_effect=prepare) as decode, \
                patch.object(main.image_text_cache, "read", side_effect=lambda purchase, image, reader: BAD) as read:
            first = self.h.client.post("/upload", headers=self.headers(), data=fields, files=files)
            second = self.h.client.post("/upload", headers=self.headers(), data=fields, files=files)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.text, first.text)
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.row(first)["quote_count"], 1)
        self.assertEqual(self.h.usage(), 1)

    def test_unreadable_photo_does_not_start_analysis_or_consume(self):
        fields = {"customer_name": "Offline", "customer_email": "offline@example.invalid"}
        with patch.object(main, "prepare_document", return_value=[{"page": 1, "image": "synthetic"}]), \
                patch.object(main.image_text_cache, "read", side_effect=HTTPException(422, BETTER_PHOTO)):
            response = self.h.client.post("/upload", headers=self.headers(), data=fields,
                files={"files": ("photo.heic", b"synthetic image", "application/octet-stream")})
        self.assertEqual(response.status_code, 422)
        self.h.assert_no_paid_work()
        self.assertFalse(self.row(response)["reservation"])

    def test_raw_inputs_credentials_and_contact_email_not_persisted(self):
        self.h.analysis.side_effect = RuntimeError("Failed before report")
        response = self.post()
        row = self.row(response)
        serialized = json.dumps(row)
        self.assertNotIn(BAD, serialized)
        self.assertNotIn("offline-verified-proof", serialized)
        self.assertNotIn("offline@example.invalid", serialized)
        listing = self.get("/jobs").text
        self.assertNotIn("fingerprint", listing)
        self.assertNotIn("customer_id", listing)

    def test_invalid_retry_and_idempotency_headers_fail_closed(self):
        for headers in ({"Idempotency-Key": "short"}, {"X-Review-Retry": "yes"}):
            self.assertEqual(self.post(**headers).status_code, 400)
        self.h.assert_no_paid_work()

    def test_pending_job_resubmission_after_restart_starts_once(self):
        self.h.ledger.register(self.h.purchase)
        payload = main.AnalyzeRequest(**self.h.payload())
        fingerprint = jobs.digest({"kind": "analysis", "request": payload.model_dump(mode="json")})
        row = self.store.create(self.h.purchase, self.key, fingerprint)
        self.assertEqual(row["state"], "pending")
        self.h.app.state.entitlements = EntitlementService(self.h.verifier, SQLitePurchaseLedger(self.h.db_path))
        result = self.post()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["X-Review-Job-ID"], row["job_id"])
        self.h.analysis.assert_called_once()
        self.assertEqual(self.h.usage(), 1)

    def test_busy_capacity_keeps_pending_job_and_no_reservation(self):
        with patch.object(jobs, "_SLOTS") as slots:
            slots.acquire.return_value = False
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["state"], "pending")
        self.assertFalse(response.json()["review_reserved"])
        self.h.assert_no_paid_work()
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.h.usage(), 1)

    def test_consumed_purchase_cannot_create_different_job_with_new_key(self):
        self.post()
        self.key = "different-new-key-0002"
        result = self.h.client.post("/analyze", headers=self.headers(), json=self.h.payload(city="Changed"))
        self.assertEqual(result.status_code, 409)
        self.h.analysis.assert_called_once()
        self.assertEqual(self.h.usage(), 1)

    def test_storage_read_failure_returns_safe_error(self):
        row = self.row(self.post())
        with patch.object(self.h.ledger, "_connect", side_effect=sqlite3.OperationalError("PRIVATE PATH")):
            with self.assertRaises(HTTPException) as error:
                self.store.get(row["job_id"], self.h.purchase)
        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn("PRIVATE PATH", error.exception.detail)

    def test_delayed_unreadable_photo_retains_retake_guidance_in_status(self):
        started, release = threading.Event(), threading.Event()
        def read(*args, **kwargs):
            started.set()
            release.wait(5)
            raise HTTPException(422, BETTER_PHOTO)
        fields = {"customer_name": "Offline", "customer_email": "offline@example.invalid"}
        with patch.object(main, "prepare_document", return_value=[{"page": 1, "image": "synthetic"}]), \
                patch.object(main.image_text_cache, "read", side_effect=read), patch.object(jobs, "WAIT_SECONDS", 0.02):
            response = self.h.client.post("/upload", headers=self.headers(), data=fields,
                files={"files": ("photo.heic", b"synthetic image", "application/octet-stream")})
            try:
                self.assertEqual(response.status_code, 202)
                self.assertTrue(started.wait(5))
            finally:
                release.set()
            until = time.monotonic() + 5
            while time.monotonic() < until:
                row = self.row(response)
                if row["state"] == "failed":
                    break
                time.sleep(0.01)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(jobs.public_status(row)["error"]["message"], BETTER_PHOTO)
        self.h.assert_no_paid_work()


if __name__ == "__main__":
    unittest.main()
