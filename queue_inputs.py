"""Stage 1 internal metadata/hold transactions. No queue processing or routes."""
import re
import time

from fastapi import HTTPException
from purchase_security import AuthorizedPurchase
from report_jobs import SQLiteReportStore
from sqlite_migrations import active_holds, check_version


class SQLiteQueuedInputs(SQLiteReportStore):
    def __init__(self, ledger):
        super().__init__(ledger)
        with self._connect() as db:
            check_version(db, require_stage1=True)

    def authorize(self, authorization):
        # Only an existing trusted authorization object; no bearer-token storage
        # and no customer-supplied purchase/tier claims accepted by this adapter.
        if not isinstance(authorization, AuthorizedPurchase) or authorization.ledger is not self.ledger:
            raise HTTPException(403, "Purchase authorization is required.")
        purchase = authorization.purchase
        if (purchase.owner_customer_id != purchase.authenticated_customer_id or
                not purchase.payment_eligible or purchase.expires_at <= time.time()):
            raise HTTPException(403, "Purchase entitlement is not eligible.")
        if purchase.tier not in {"tier1", "tier2"}:
            raise HTTPException(503, "This tier is not available.")
        with self._connect() as db:
            row = db.execute("""SELECT 1 FROM purchases WHERE purchase_id=? AND customer_id=?
                AND tier=? AND eligible=1 AND expires_at>?""", (purchase.purchase_id,
                purchase.owner_customer_id, purchase.tier, time.time())).fetchone()
            if not row:
                raise HTTPException(403, "Purchase entitlement is not eligible.")
        return purchase

    def register_inputs(self, authorization, key, fingerprint, root_id, bundle_id,
                        manifest_hash, byte_count, quote_count):
        purchase = self.authorize(authorization)
        authorization.validate_submission(quote_count)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise HTTPException(400, "A valid submission key is required.")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._create(db, purchase, key, fingerprint)
            job_id = row['job_id']
            previous = db.execute("SELECT root_id FROM review_inputs WHERE job_id=?", (job_id,)).fetchone()
            if previous:
                if previous[0] != root_id:
                    raise HTTPException(409, "This review uses a different input store.")
                return self._registered(db, row)
            if row['state'] != 'pending' or row['attempts'] or row['reservation']:
                raise HTTPException(409, "This review has already been started.")
            eligible = db.execute("""SELECT used,submission_limit FROM purchases WHERE purchase_id=?
                AND customer_id=? AND tier=? AND eligible=1 AND expires_at>?""", (purchase.purchase_id,
                purchase.owner_customer_id, purchase.tier, time.time())).fetchone()
            if not eligible or eligible[0] + active_holds(db, purchase.purchase_id) >= eligible[1]:
                raise HTTPException(409, "Purchase entitlement is unavailable or already held.")
            now = time.time()
            db.execute("INSERT INTO review_inputs VALUES (?,?,?,?,?,?,?)",
                       (job_id, root_id, bundle_id, manifest_hash, byte_count, quote_count, now))
            db.execute("INSERT INTO review_holds VALUES (?,?,'active',?,?)",
                       (job_id, purchase.purchase_id, now, now))
            return self._registered(db, row)

    def _registered(self, db, row):
        """Stage 2 may register readiness inside the same input/hold transaction."""
        return row

    def _released(self, db, job_id):
        pass

    def inputs(self, authorization, job_id):
        purchase = self.authorize(authorization)
        with self._connect() as db:
            self._row(db, job_id, purchase)
            if check_version(db, validate_schema=False) >= 3 and db.execute(
                    'SELECT 1 FROM input_retention WHERE job_id=?', (job_id,)).fetchone():
                raise HTTPException(410, 'Source documents have been retired. Saved review status and completed reports remain available.')
            row = db.execute("SELECT * FROM review_inputs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Saved inputs not found.")
            return dict(row)

    def release(self, authorization, job_id):
        """Invalid pending inputs release capacity, never refund paid execution.

        Referenced files remain protected. Retention/deletion of referenced
        customer inputs requires a later explicit lifecycle, not orphan cleanup.
        """
        purchase = self.authorize(authorization)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, job_id, purchase)
            if row['reservation'] or row['state'] in {'processing', 'completed'}:
                raise HTTPException(409, "An active or consumed review cannot release its hold.")
            db.execute("UPDATE review_holds SET state='released',updated=? WHERE job_id=? AND state='active'",
                       (time.time(), job_id))
            db.execute("""UPDATE review_jobs SET state='failed',error_code='input_rejected',
                http_status=422,retryable=0,updated=? WHERE job_id=?""", (time.time(), job_id))
            self._released(db, job_id)
