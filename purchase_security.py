"""Purchase authorization contract; no Wix/payment integration is assumed.

Production is closed unless trusted startup code installs an EntitlementService
on app.state.entitlements. Its verifier must authenticate the customer AND read
the authoritative purchase on every request (including cancellations/expiry).
Browser callbacks, tier names and order IDs alone are never verification.

HTTP callers supply Authorization: Bearer <opaque proof> and X-Purchase-ID as an
untrusted order selector. The Wix adapter, its server credentials, actual product
mapping and authenticated customer handoff are still missing. The existing plain
HTML form does not supply these headers; authenticated frontend transport must
be wired before launch. No URL-token or hidden-form-credential fallback exists.

SQLitePurchaseLedger is an explicit local durable adapter, not a deployment
default. Production storage is BLOCKED pending confirmation of persistent disk,
permissions and worker topology, or an equivalent transactional ledger adapter.
No public provisioning/webhook endpoint, environment bypass, or test mode exists.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import stat
import time
from typing import Optional, Protocol
import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlite_migrations import active_holds, check_version, migrate_stage1


TIER_ALIASES = {
    "basic": "tier1", "standard": "tier2", "premium": "tier3",
    "tier1": "tier1", "tier2": "tier2", "tier3": "tier3",
}
QUOTE_LIMITS = {"tier1": 1, "tier2": 3, "tier3": 3}


def normalize_tier(value: str) -> str:
    tier = TIER_ALIASES.get(value.strip().lower())
    if tier is None:
        raise HTTPException(400, "Invalid package.")
    return tier


class VerifiedPurchase(BaseModel):
    """Internal verifier result, never a public request model or Wix API schema.

    Both customer IDs must come from trusted sources: authenticated identity and
    the purchase owner, respectively. expires_at is a Unix timestamp; adapters
    must establish a finite validity period and the purchased submission limit.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    purchase_id: StrictStr = Field(min_length=1, max_length=200)
    owner_customer_id: StrictStr = Field(min_length=1, max_length=200)
    authenticated_customer_id: StrictStr = Field(min_length=1, max_length=200)
    tier: StrictStr
    payment_eligible: StrictBool
    expires_at: StrictInt = Field(gt=0)
    submission_limit: StrictInt = Field(ge=1)


class InvalidPurchaseProof(Exception):
    """Verifier could not establish authenticated ownership/payment proof."""


class PurchaseVerifier(Protocol):
    async def verify(self, purchase_id: str, bearer_token: str) -> VerifiedPurchase:
        """Check authenticated identity and current authoritative purchase state.

        Raise InvalidPurchaseProof for invalid credentials/unknown orders.
        Other errors fail closed as verification unavailable. Do not return a
        model built from unverified browser-supplied purchase claims.
        """
        ...


class PurchaseLedger(Protocol):
    def register(self, purchase: VerifiedPurchase) -> int: ...
    def revoke(self, purchase_id: str) -> None: ...
    def consume(self, purchase: VerifiedPurchase) -> str: ...


class SQLitePurchaseLedger:
    """Atomic records on one explicitly chosen persistent SQLite filesystem.

    No connection is shared between requests. BEGIN IMMEDIATE serializes quota
    checks and updates across threads/processes using this same database file.
    A repeated verified purchase never replenishes usage. Ownership, tier and
    purchased quantity cannot be changed by duplicate/replayed notifications.

    Startup opens an existing private database. Provision a genuinely new ledger
    once with initialize(); never call it as a fallback when a volume is missing.
    """
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.parent.is_dir():
            raise ValueError("An explicit absolute ledger path with an existing parent is required")
        public = Path(__file__).resolve().parent / "static"
        if self.path.resolve() == public or public in self.path.resolve().parents:
            raise ValueError("The ledger must be outside the public static directory")
        with self._connect() as db:
            # Do not repair an empty/wrong database into a fresh entitlement store.
            db.execute("SELECT purchase_id,customer_id,tier,submission_limit,used,expires_at,eligible FROM purchases LIMIT 0")
            db.execute("SELECT submission_id,purchase_id,started_at FROM submissions LIMIT 0")
            check_version(db)

    @classmethod
    def initialize(cls, path: Path):
        """Explicit first-time provisioning; exclusive creation, never overwrite."""
        path = Path(path)
        public = Path(__file__).resolve().parent / "static"
        if (not path.is_absolute() or not path.parent.is_dir() or
                path.resolve() == public or public in path.resolve().parents):
            raise ValueError("Choose an absolute private database path with an existing parent")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        self = cls.__new__(cls)
        self.path = path
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS purchases (
                    purchase_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    submission_limit INTEGER NOT NULL CHECK(submission_limit > 0),
                    used INTEGER NOT NULL DEFAULT 0 CHECK(used >= 0),
                    expires_at INTEGER NOT NULL,
                    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT PRIMARY KEY,
                    purchase_id TEXT NOT NULL REFERENCES purchases(purchase_id),
                    started_at INTEGER NOT NULL
                );
            """)
        migrate_stage1(self)
        return cls(path)

    @contextmanager
    def _connect(self):
        try:
            info = self.path.lstat()
        except OSError:
            raise sqlite3.OperationalError("Private database is unavailable") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise sqlite3.OperationalError("Private database permissions are required")
        # mode=rw is essential: volume loss must not silently replenish purchases.
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            check_version(connection, validate_schema=False)
            with connection:
                yield connection
        finally:
            connection.close()

    def register(self, purchase: VerifiedPurchase) -> int:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO purchases
                (purchase_id, customer_id, tier, submission_limit, expires_at, eligible)
                VALUES (?, ?, ?, ?, ?, ?)""", (
                purchase.purchase_id, purchase.owner_customer_id, purchase.tier,
                purchase.submission_limit, purchase.expires_at, int(purchase.payment_eligible)))
            row = db.execute("""SELECT customer_id, tier, submission_limit, used,
                expires_at, eligible FROM purchases WHERE purchase_id = ?""",
                (purchase.purchase_id,)).fetchone()
            if row[:3] != (purchase.owner_customer_id, purchase.tier, purchase.submission_limit):
                raise HTTPException(409, "Purchase details changed. Please contact support.")
            # Revocation is terminal here; a replay cannot re-enable an order.
            expiry = min(row[4], purchase.expires_at)
            eligible = row[5] and purchase.payment_eligible
            db.execute("UPDATE purchases SET expires_at = ?, eligible = ? WHERE purchase_id = ?",
                       (expiry, int(eligible), purchase.purchase_id))
            remaining = row[2] - row[3] - active_holds(db, purchase.purchase_id)
        # Commit a cancellation/shortened expiry before rejecting the request.
        # Raising inside the transaction would roll that restriction back.
        if not eligible or expiry <= time.time():
            raise HTTPException(403, "Purchase entitlement is not eligible.")
        return remaining

    def revoke(self, purchase_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE purchases SET eligible = 0 WHERE purchase_id = ?", (purchase_id,))

    def consume(self, purchase: VerifiedPurchase) -> str:
        submission_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reserve(db, purchase, submission_id)
        return submission_id

    def _reserve(self, db, purchase, submission_id):
        """Reserve within the caller transaction, including a durable job transaction."""
        updated = db.execute("""UPDATE purchases SET used = used + 1
            WHERE purchase_id = ? AND customer_id = ? AND tier = ?
            AND eligible = 1 AND expires_at > ? AND used + ? < submission_limit""",
            (purchase.purchase_id, purchase.owner_customer_id, purchase.tier, time.time(),
             active_holds(db, purchase.purchase_id)))
        if updated.rowcount != 1:
            raise HTTPException(409, "Purchase entitlement is unavailable or already used.")
        db.execute("INSERT INTO submissions VALUES (?, ?, ?)",
                   (submission_id, purchase.purchase_id, int(time.time())))


@dataclass(frozen=True)
class AuthorizedPurchase:
    purchase: VerifiedPurchase
    ledger: PurchaseLedger

    @property
    def tier(self) -> str:
        return self.purchase.tier

    def validate_claims(self, *claims: Optional[str]) -> str:
        normalized = [normalize_tier(value) for value in claims if value is not None]
        if len(set(normalized)) > 1:
            raise HTTPException(400, "Conflicting package parameters.")
        if any(value != self.tier for value in normalized):
            raise HTTPException(403, "Requested package does not match the verified purchase.")
        return self.tier

    def validate_submission(self, count: int, *claims: Optional[str]) -> str:
        tier = self.validate_claims(*claims)
        if count < 1 or count > QUOTE_LIMITS[tier]:
            raise HTTPException(400, f"This purchase allows 1 to {QUOTE_LIMITS[tier]} quote uploads.")
        return tier

    def consume(self) -> str:
        """Reserve one use after validation, immediately before paid analysis.

        Once analysis begins the reservation is retained even on failure. Public
        submission routes use JobPurchase to link this reservation atomically
        and reuse it only for a bounded explicit retry of the same job.
        """
        try:
            return self.ledger.consume(self.purchase)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Purchase usage storage is unavailable.") from None


class EntitlementService:
    def __init__(self, verifier: PurchaseVerifier, ledger: PurchaseLedger):
        self.verifier, self.ledger = verifier, ledger

    async def authorize(self, purchase_id: str, bearer_token: str, *, require_unused=True) -> AuthorizedPurchase:
        try:
            purchase = await self.verifier.verify(purchase_id, bearer_token)
        except InvalidPurchaseProof:
            raise HTTPException(403, "Purchase authorization failed.") from None
        except Exception:
            raise HTTPException(503, "Purchase verification is unavailable.") from None
        if not isinstance(purchase, VerifiedPurchase):
            raise HTTPException(503, "Purchase verification is unavailable.")
        if (purchase.purchase_id != purchase_id or
                purchase.owner_customer_id != purchase.authenticated_customer_id):
            raise HTTPException(403, "Purchase authorization failed.")
        if purchase.tier not in QUOTE_LIMITS:
            raise HTTPException(503, "Verified purchase tier is not configured.")
        # There is no vetting fulfillment integration yet. A flag or a claimed
        # premium purchase must not enable an unfulfilled paid service.
        if purchase.tier == "tier3":
            raise HTTPException(503, "Tier 3 contractor vetting is not currently available.")
        try:
            if not purchase.payment_eligible or purchase.expires_at <= time.time():
                self.ledger.revoke(purchase.purchase_id)
                raise HTTPException(403, "Purchase entitlement is not eligible.")
            remaining = self.ledger.register(purchase)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Purchase usage storage is unavailable.") from None
        if require_unused and remaining < 1:
            raise HTTPException(409, "Purchase entitlement is already used.")
        return AuthorizedPurchase(purchase, self.ledger)


async def require_purchase(request: Request) -> AuthorizedPurchase:
    """Only a trusted server verifier can turn this selector/proof into a grant."""
    authorization = request.headers.get("authorization", "")
    scheme, _, proof = authorization.partition(" ")
    purchase_id = request.headers.get("x-purchase-id", "")
    if (scheme.lower() != "bearer" or not proof.strip() or len(proof) > 8192 or
            not purchase_id.strip() or len(purchase_id) > 200):
        raise HTTPException(401, "Verified purchase authorization is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    service = getattr(request.app.state, "entitlements", None)
    if not isinstance(service, EntitlementService):
        raise HTTPException(503, "Paid submissions are unavailable until purchase verification is configured.")
    return await service.authorize(purchase_id, proof, require_unused=(
        not getattr(request.state, "report_access", False) and
        not (request.method == "POST" and request.url.path.rstrip("/") in {"/analyze", "/upload"}
             and "idempotency-key" in request.headers)))


async def require_report_access(request: Request) -> AuthorizedPurchase:
    """Reverify ownership/payment/tier even after quota is consumed; never grant more quota."""
    request.state.report_access = True
    return await require_purchase(request)
