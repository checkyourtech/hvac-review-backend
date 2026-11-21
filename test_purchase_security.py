"""Offline purchase-boundary tests. No payment, Wix, SMTP or model calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import main
from offline_test_client import AuthorizedTestClient
from purchase_security import (
    EntitlementService, InvalidPurchaseProof, SQLitePurchaseLedger,
    VerifiedPurchase, require_purchase, require_report_access,
)
from test_blower_live_response import saved_response
from test_blower_support_normalization import BAD, classified


class PurchaseSecurityTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(TemporaryDirectory(prefix="cyt-security-"))
        self.directory = Path(directory)
        self.uploads = self.directory / "uploads"
        self.uploads.mkdir()
        self.db_path = self.directory / "purchases.sqlite3"
        self.ledger = SQLitePurchaseLedger.initialize(self.db_path)
        self.purchase = self.make_purchase()
        self.verifier = AsyncMock()
        self.verifier.verify.side_effect = self.verify
        self.app = FastAPI()
        self.app.include_router(main.app.router)
        self.app.state.entitlements = EntitlementService(self.verifier, self.ledger)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.stack.enter_context(patch.object(main, "UPLOAD_DIR", self.uploads))
        self.classifier = self.stack.enter_context(patch.object(main, "classify_quotes", return_value=classified()))
        self.analysis = self.stack.enter_context(patch.object(main, "request_structured_analysis",
            side_effect=lambda **kwargs: saved_response()))
        self.email = self.stack.enter_context(patch.object(main, "send_review_email"))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden")))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def make_purchase(self, **changes):
        values = dict(purchase_id="synthetic-order", owner_customer_id="customer-a",
                      authenticated_customer_id="customer-a", tier="tier1", payment_eligible=True,
                      expires_at=int(time.time()) + 3600, submission_limit=1)
        return VerifiedPurchase(**{**values, **changes})

    async def verify(self, purchase_id, proof):
        if proof != "offline-verified-proof" or purchase_id != self.purchase.purchase_id:
            raise InvalidPurchaseProof("Invalid offline proof")
        return self.purchase

    def headers(self, **changes):
        return {"Authorization": "Bearer offline-verified-proof", "X-Purchase-ID": self.purchase.purchase_id, **changes}

    def payload(self, count=1, **changes):
        return {"customerName": "Offline", "customerEmail": "offline@example.invalid",
                "files": [{"fileName": f"quote-{i}.txt", "extractedText": BAD} for i in range(count)], **changes}

    def submit(self, **changes):
        return self.client.post("/analyze", headers=self.headers(), json=self.payload(**changes))

    def upload(self, count=1, data=None, headers=None, content=None, filename="quote.txt"):
        return self.client.post("/upload", headers=self.headers() if headers is None else headers,
            data=data or {"customer_name": "Offline", "customer_email": "offline@example.invalid"},
            files=[("files", (filename, BAD.encode() if content is None else content, "application/octet-stream")) for _ in range(count)])

    def usage(self):
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as db:
            return db.execute("SELECT COALESCE(SUM(used), 0) FROM purchases").fetchone()[0]

    def assert_no_paid_work(self):
        self.classifier.assert_not_called()
        self.analysis.assert_not_called()
        self.email.assert_not_called()
        self.assertEqual(self.usage(), 0)

    def test_authorized_tier1_uses_verified_tier_and_records_one_submission(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.usage(), 1)
        self.assertEqual(self.email.call_args.kwargs["package_key"], "tier1")
        self.assertIn(main.PACKAGE_RULES["tier1"], self.analysis.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(response.json()["contractor_vetting"], "")
        self.assertEqual(response.json()["quote_comparison"], "")
        self.assertEqual(response.json()["best_quote_recommendation"], "")

    def test_authorized_tier2_three_quotes_uses_verified_scope(self):
        self.purchase = self.make_purchase(tier="tier2")
        response = self.submit(count=3)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.email.call_args.kwargs["package_key"], "tier2")
        self.assertEqual(len(self.email.call_args.kwargs["file_names"]), 3)
        self.assertIn(main.PACKAGE_RULES["tier2"], self.analysis.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(response.json()["contractor_vetting"], "")
        self.assertEqual(self.usage(), 1)

    def test_unauthorized_analysis_and_trailing_slash_do_not_run(self):
        for url in ("/analyze", "/analyze/"):
            with self.subTest(url=url):
                response = self.client.post(url, json=self.payload())
                self.assertEqual(response.status_code, 401)
        self.verifier.verify.assert_not_called()
        self.assert_no_paid_work()

    def test_unauthorized_upload_does_not_save_files(self):
        self.assertEqual(self.upload(headers={}).status_code, 401)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assert_no_paid_work()

    def test_upload_page_url_is_not_authorization(self):
        for query in ("?package=basic", "?package=premium&paid=true", "?token=offline-verified-proof"):
            self.assertEqual(self.client.get("/upload-page" + query).status_code, 401)
        self.assert_no_paid_work()

    def test_default_application_fails_closed_even_with_claimed_purchase(self):
        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.post("/analyze", headers=self.headers(), json=self.payload())
        self.assertEqual(response.status_code, 503)
        self.assert_no_paid_work()

    def test_forged_payment_proof_is_rejected(self):
        response = self.client.post("/analyze", headers=self.headers(Authorization="Bearer forged-proof"), json=self.payload())
        self.assertEqual(response.status_code, 403)
        self.assert_no_paid_work()

    def test_forged_purchase_identifier_is_rejected(self):
        response = self.client.post("/analyze", headers=self.headers(**{"X-Purchase-ID": "forged-order"}), json=self.payload())
        self.assertEqual(response.status_code, 403)
        self.assert_no_paid_work()

    def test_browser_payment_success_and_owner_claims_do_not_authorize(self):
        response = self.client.post("/analyze", json=self.payload(
            paid=True, payment_status="success", purchase_id="synthetic-order",
            customer_id="customer-a", token="offline-verified-proof"))
        self.assertEqual(response.status_code, 401)
        self.assert_no_paid_work()

    def test_unpaid_purchase_is_rejected(self):
        self.purchase = self.make_purchase(payment_eligible=False)
        self.assertEqual(self.submit().status_code, 403)
        self.assert_no_paid_work()

    def test_expired_purchase_is_rejected(self):
        self.purchase = self.make_purchase(expires_at=int(time.time()) - 1)
        self.assertEqual(self.submit().status_code, 403)
        self.assert_no_paid_work()

    def test_cancelled_purchase_cannot_be_revived_by_replayed_eligible_snapshot(self):
        self.assertEqual(self.client.get("/upload-page", headers=self.headers()).status_code, 200)
        self.purchase = self.make_purchase(payment_eligible=False)
        self.assertEqual(self.submit().status_code, 403)
        self.purchase = self.make_purchase()
        self.assertEqual(self.submit().status_code, 403)
        self.assert_no_paid_work()

    def test_correct_purchase_ownership_is_accepted(self):
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.usage(), 1)

    def test_incorrect_owner_is_rejected_before_storage(self):
        self.purchase = self.make_purchase(authenticated_customer_id="customer-b")
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assert_no_paid_work()

    def test_verifier_returning_different_order_is_rejected(self):
        self.verifier.verify.side_effect = None
        self.verifier.verify.return_value = self.make_purchase(purchase_id="different-order")
        self.assertEqual(self.submit().status_code, 403)
        self.assert_no_paid_work()

    def test_tier_escalation_and_downgrade_claims_are_rejected(self):
        for claimed in ("tier2", "standard", "premium", "tier3"):
            with self.subTest(claimed=claimed):
                self.assertEqual(self.submit(packageName=claimed).status_code, 403)
        self.purchase = self.make_purchase(tier="tier2", purchase_id="second-order")
        self.assertEqual(self.submit(package="basic").status_code, 403)
        self.assert_no_paid_work()

    def test_tier1_excess_quotes_rejected_without_consumption(self):
        self.assertEqual(self.submit(count=2).status_code, 400)
        self.assertEqual(self.upload(count=2).status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assert_no_paid_work()
        self.assertEqual(self.submit().status_code, 200)

    def test_tier2_excess_quotes_rejected_without_consumption(self):
        self.purchase = self.make_purchase(tier="tier2")
        self.assertEqual(self.submit(count=4).status_code, 400)
        self.assertEqual(self.upload(count=4).status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assert_no_paid_work()

    def test_duplicate_verified_purchase_notifications_do_not_replenish_usage(self):
        self.assertEqual(self.ledger.register(self.purchase), 1)
        self.assertEqual(self.ledger.register(self.purchase), 1)
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.ledger.register(self.purchase), 0)
        self.assertEqual(self.ledger.register(self.purchase), 0)
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(self.analysis.call_count, 1)

    def test_replay_across_analyze_and_upload_is_rejected(self):
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual(self.analysis.call_count, 1)
        self.assertEqual(self.usage(), 1)

    def test_ineligible_registration_commits_restriction_before_rejection(self):
        for index, changes in enumerate(({"payment_eligible": False}, {"expires_at": int(time.time()) - 1})):
            with self.subTest(changes=changes):
                order = f"restriction-{index}"
                purchase = self.make_purchase(purchase_id=order)
                self.ledger.register(purchase)
                with self.assertRaises(HTTPException):
                    self.ledger.register(self.make_purchase(purchase_id=order, **changes))
                with self.assertRaises(HTTPException):
                    self.ledger.register(purchase)
                with self.assertRaises(HTTPException):
                    self.ledger.consume(purchase)
        self.assert_no_paid_work()

    def test_conflicting_package_parameters_rejected_on_both_routes(self):
        self.assertEqual(self.submit(package="basic", packageName="standard").status_code, 400)
        self.assertEqual(self.upload(data={"package": "basic", "packageName": "standard",
            "customer_name": "Offline", "customer_email": "offline@example.invalid"}).status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assert_no_paid_work()

    def test_package_alias_no_longer_shadowed_by_default(self):
        self.purchase = self.make_purchase(tier="tier2")
        self.assertEqual(self.submit(count=2, package="standard").status_code, 200)
        self.assertEqual(self.email.call_args.kwargs["package_key"], "tier2")

    def test_equivalent_alias_claims_are_allowed(self):
        self.purchase = self.make_purchase(tier="tier2")
        self.assertEqual(self.submit(package=" Standard ", packageName="TIER2").status_code, 200)

    def test_unknown_or_blank_aliases_are_rejected(self):
        for alias in ("gold", "tier4", "", "  "):
            with self.subTest(alias=alias):
                self.assertEqual(self.submit(package=alias).status_code, 400)
        self.assert_no_paid_work()

    def test_upload_page_uses_verified_tier_and_does_not_consume(self):
        self.purchase = self.make_purchase(tier="tier2")
        for alias in ("standard", "tier2"):
            response = self.client.get("/upload-page", params={"package": alias}, headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.assertIn('name="package" value="tier2"', response.text)
            self.assertNotIn("offline-verified-proof", response.text)
        self.assertEqual(self.client.get("/upload-page?package=premium", headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.get("/upload-page?package=unknown", headers=self.headers()).status_code, 400)
        self.assertEqual(self.client.get("/upload-page?package=tier2&packageName=basic", headers=self.headers()).status_code, 400)
        self.assert_no_paid_work()

    def test_tier3_is_closed_while_vetting_is_unfulfilled(self):
        self.purchase = self.make_purchase(tier="tier3")
        self.assertEqual(self.submit(package="premium").status_code, 503)
        self.assertEqual(self.upload().status_code, 503)
        self.assertEqual(self.client.get("/upload-page?package=premium", headers=self.headers()).status_code, 503)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assert_no_paid_work()

    def test_disabled_tier_never_registers_a_paid_entitlement(self):
        service = self.app.state.entitlements
        with patch.object(self.ledger, "register", wraps=self.ledger.register) as register, \
                patch.object(self.ledger, "consume", wraps=self.ledger.consume) as consume:
            # Even a trusted verifier claiming a paid order cannot enable Tier 3.
            # Noncanonical verifier tiers must also fail closed, not become grants.
            for tier in ("tier3", "premium", "TIER3", " tier3 "):
                with self.subTest(tier=tier):
                    self.purchase = self.make_purchase(tier=tier, payment_eligible=True)
                    with self.assertRaises(HTTPException) as error:
                        asyncio.run(service.authorize(self.purchase.purchase_id, "offline-verified-proof"))
                    self.assertEqual(error.exception.status_code, 503)
            register.assert_not_called()
            consume.assert_not_called()
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM purchases").fetchone()[0], 0)
        self.assert_no_paid_work()

    def test_preexisting_tier3_record_cannot_be_used_or_downgraded_by_customer(self):
        self.purchase = self.make_purchase(tier="tier3")
        # Model a stale internal record, not a public provisioning mechanism.
        self.ledger.register(self.purchase)
        for claim in ("basic", "standard", "premium"):
            with self.subTest(claim=claim):
                self.assertEqual(self.submit(package=claim).status_code, 503)
                self.assertEqual(self.upload(data={"packageName": claim}).status_code, 503)
                self.assertEqual(self.client.get("/upload-page", params={"package": claim},
                    headers=self.headers()).status_code, 503)
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)
        self.assert_no_paid_work()

    def test_launch_tiers_reject_premium_claims_on_every_customer_entry_point(self):
        for tier in ("tier1", "tier2"):
            self.purchase = self.make_purchase(tier=tier, purchase_id="claim-" + tier)
            for field in ("package", "packageName"):
                for claim in ("tier3", " Premium "):
                    with self.subTest(tier=tier, field=field, claim=claim):
                        self.assertEqual(self.submit(**{field: claim}).status_code, 403)
                        self.assertEqual(self.upload(data={field: claim,
                            "customer_name": "Offline", "customer_email": "offline@example.invalid"}).status_code, 403)
                        self.assertEqual(self.client.get("/upload-page", params={field: claim},
                            headers=self.headers()).status_code, 403)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assert_no_paid_work()

    def test_launch_reports_strip_disabled_vetting_before_json_html_and_delivery(self):
        marker = "SYNTHETIC_DISABLED_VETTING_CONTENT"
        incoming = saved_response()
        incoming.contractor_vetting = marker
        self.analysis.side_effect = lambda **kwargs: incoming.model_copy(deep=True)
        for tier, count in (("tier1", 1), ("tier2", 3)):
            with self.subTest(tier=tier):
                self.purchase = self.make_purchase(tier=tier, purchase_id="reports-" + tier, submission_limit=2)
                response = self.submit(count=count)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["contractor_vetting"], "")
                self.assertNotIn(marker, response.text)
                uploaded = self.upload(count=count)
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                delivered = self.email.call_args.kwargs["analysis"]
                self.assertEqual(delivered.contractor_vetting, "")
                self.assertEqual(uploaded.text, main.build_report_html(delivered, count))
                self.assertNotIn(marker, uploaded.text)
                self.assertNotIn("<h2>Contractor Vetting</h2>", uploaded.text)
                self.assertIn('onclick="window.print()"', uploaded.text)
        for call in self.analysis.call_args_list:
            self.assertNotIn("Contractor vetting search results:", call.kwargs["messages"][1]["content"])
        for call in self.email.call_args_list:
            self.assertEqual(call.kwargs["analysis"].contractor_vetting, "")
        self.assertEqual(incoming.contractor_vetting, marker)

    def test_launch_upload_pages_expose_only_verified_quote_limits(self):
        for tier, limit in (("tier1", 1), ("tier2", 3)):
            with self.subTest(tier=tier):
                self.purchase = self.make_purchase(tier=tier, purchase_id="page-" + tier)
                response = self.client.get("/upload-page", headers=self.headers())
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'data-quote-limit="{limit}"', response.text)
                self.assertIn(f'name="package" value="{tier}"', response.text)
                self.assertNotIn("tier3", response.text.lower())
                self.assertNotIn("premium", response.text.lower())
                self.assertNotIn("vetting", response.text.lower())
        self.assert_no_paid_work()

    def test_concurrent_requests_cannot_consume_same_entitlement(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertIn(200, [r.status_code for r in responses])
        self.assertTrue(all(r.status_code in {200, 202, 409} for r in responses))
        jobs = {r.headers.get("X-Review-Job-ID") for r in responses if r.status_code != 409}
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.usage(), 1)
        self.assertEqual(self.analysis.call_count, 1)

    def test_ledger_reopen_preserves_consumption(self):
        self.assertEqual(self.submit().status_code, 200)
        reopened = SQLitePurchaseLedger(self.db_path)
        self.app.state.entitlements = EntitlementService(self.verifier, reopened)
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(self.analysis.call_count, 1)

    def test_empty_whitespace_and_missing_text_do_not_consume(self):
        for files, status in (([], 400), ([{"extractedText": "  "}], 422), ([{"fileName": "quote.txt"}], 422)):
            self.assertEqual(self.submit(files=files).status_code, status)
        self.assert_no_paid_work()

    def test_schema_validation_failure_does_not_consume(self):
        response = self.client.post("/analyze", headers=self.headers(), json={"files": "not-a-list"})
        self.assertEqual(response.status_code, 422)
        self.assert_no_paid_work()

    def test_file_extraction_failure_does_not_consume(self):
        self.assertEqual(self.upload(content=b"invalid-pdf", filename="invalid.pdf").status_code, 422)
        self.assert_no_paid_work()

    def test_analysis_failure_retains_record_without_automatic_reuse(self):
        self.analysis.side_effect = RuntimeError("Synthetic failure after analysis started")
        self.assertEqual(self.submit().status_code, 500)
        self.assertEqual(self.usage(), 1)
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(self.analysis.call_count, 1)
        self.email.assert_not_called()

    def test_multiple_purchased_uses_are_bounded_by_trusted_limit(self):
        self.purchase = self.make_purchase(submission_limit=2)
        self.assertEqual([self.submit(city=f"Synthetic City {i}").status_code for i in range(3)], [200, 200, 409])
        self.assertEqual(self.usage(), 2)

    def test_changed_verified_owner_tier_or_quantity_cannot_reset_record(self):
        self.ledger.register(self.purchase)
        variants = [self.make_purchase(owner_customer_id="customer-b", authenticated_customer_id="customer-b"),
                    self.make_purchase(tier="tier2"), self.make_purchase(submission_limit=2)]
        for variant in variants:
            with self.subTest(variant=variant.tier), self.assertRaises(HTTPException) as error:
                self.ledger.register(variant)
            self.assertEqual(error.exception.status_code, 409)
        self.assert_no_paid_work()

    def test_expiry_is_rechecked_atomically_at_consumption(self):
        self.ledger.register(self.purchase)
        with patch("purchase_security.time.time", return_value=self.purchase.expires_at + 1):
            with self.assertRaises(HTTPException):
                self.ledger.consume(self.purchase)
        self.assert_no_paid_work()

    def test_verification_outage_fails_closed_without_disclosing_details(self):
        self.verifier.verify.side_effect = RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_DETAIL")
        response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC_PRIVATE_PROVIDER_DETAIL", response.text)
        self.assert_no_paid_work()

    def test_unstructured_verification_result_fails_closed(self):
        self.verifier.verify.side_effect = None
        self.verifier.verify.return_value = {"paid": True, "tier": "tier1"}
        self.assertEqual(self.submit().status_code, 503)
        self.assert_no_paid_work()

    def test_ledger_failure_fails_closed_before_analysis(self):
        with patch.object(self.ledger, "_reserve", side_effect=sqlite3.OperationalError("private path")):
            response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private path", response.text)
        self.assert_no_paid_work()

    def test_no_public_purchase_provisioning_or_alternate_analysis_route(self):
        paid_routes = [r for r in self.app.routes if "POST" in getattr(r, "methods", set())]
        self.assertEqual({r.path for r in paid_routes}, {"/upload", "/analyze", "/jobs/{job_id}/delivery-retry"})
        for route in paid_routes:
            expected = require_report_access if route.path.endswith("/delivery-retry") else require_purchase
            self.assertTrue(any(d.call is expected for d in route.dependant.dependencies))
        self.assertEqual(self.client.post("/purchase", json={"paid": True}).status_code, 404)

    def test_direct_python_call_requires_explicit_authorization(self):
        with self.assertRaises(HTTPException) as error:
            asyncio.run(main.analyze_hvac_quote(main.AnalyzeRequest(**self.payload())))
        self.assertEqual(error.exception.status_code, 401)
        self.assert_no_paid_work()

    def test_existing_internal_client_does_not_open_production_app(self):
        before = dict(main.app.dependency_overrides)
        self.assertEqual(AuthorizedTestClient(main.app).post("/analyze", json=self.payload()).status_code, 200)
        self.assertEqual(main.app.dependency_overrides, before)
        self.assertEqual(TestClient(main.app).post("/analyze", json=self.payload()).status_code, 401)
        self.assertEqual(self.usage(), 0)


if __name__ == "__main__":
    unittest.main()
