"""Explicit authorization for existing offline HVAC regression tests only.

Production never imports this module. Each client clones the application routes
and overrides authorization on that clone only; main.app remains closed. Tier 3
is allowed solely to preserve historical renderer coverage, not to represent
working vetting. New purchase-security tests use the real dependency instead.
"""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from purchase_security import (
    AuthorizedPurchase, SQLitePurchaseLedger, VerifiedPurchase,
    normalize_tier, require_purchase,
)


@contextmanager
def offline_purchase(tier="tier1"):
    with TemporaryDirectory(prefix="cyt-offline-purchase-") as directory:
        ledger = SQLitePurchaseLedger(Path(directory) / "purchases.sqlite3")
        purchase = VerifiedPurchase(
            purchase_id="offline-" + uuid.uuid4().hex,
            owner_customer_id="synthetic-owner", authenticated_customer_id="synthetic-owner",
            tier=tier, payment_eligible=True, expires_at=int(time.time()) + 3600,
            submission_limit=1,
        )
        ledger.register(purchase)
        yield AuthorizedPurchase(purchase, ledger)


class AuthorizedTestClient(TestClient):
    def __init__(self, app, *args, **kwargs):
        isolated_app = FastAPI()
        isolated_app.include_router(app.router)
        super().__init__(isolated_app, *args, **kwargs)

    def request(self, *args, **kwargs):
        payload = kwargs.get("json") or kwargs.get("data") or {}
        tier = normalize_tier(payload.get("packageName") or payload.get("package") or "tier1")
        with offline_purchase(tier) as grant:
            self.app.dependency_overrides[require_purchase] = lambda: grant
            try:
                return super().request(*args, **kwargs)
            finally:
                self.app.dependency_overrides.pop(require_purchase, None)
