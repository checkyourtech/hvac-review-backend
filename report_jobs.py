"""Shared durable report/entitlement adapter and legacy submission compatibility.

Uses the same explicit SQLite database as the purchase ledger so reserving a
review and recording its job are atomic. Compatibility submissions keep inputs
in bounded memory. Opt-in Stage 2 uses the private-input adapter, managed queue
claims and fenced completion; this module does not activate that queue itself.
Deployment storage, process supervision and retention remain unconfigured.
"""
import asyncio
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid

from openai import APITimeoutError, LengthFinishReasonError
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from purchase_security import AuthorizedPurchase, SQLitePurchaseLedger
from document_intake import BETTER_PHOTO
from sqlite_migrations import REPORT_SCHEMA, active_holds, check_reports, check_version


WAIT_SECONDS = 20
PROCESSING_SECONDS = 900
MAX_ATTEMPTS = 2
MAX_DELIVERY_ATTEMPTS = 3
DELIVERY_SECONDS = 60
_SLOTS = threading.BoundedSemaphore(2)
PRIVATE_HEADERS = {"Cache-Control": "no-store, private", "Pragma": "no-cache",
                   "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
ERROR_MESSAGES = {
    "unreadable_photo": BETTER_PHOTO,
    "input_rejected": "The documents or customer fields need correction. No review was started.",
    "timeout": "The review timed out. No incomplete report is available.",
    "response_length": "The analysis exceeded its response limit. No report was generated.",
    "analysis_failed": "The review could not be completed. No incomplete report is available.",
    "interrupted": "Processing was interrupted or exceeded its deadline. Contact support before retrying.",
    "storage_failed": "The completed report could not be saved. Contact support before retrying.",
    "verification_unavailable": "Purchase verification is temporarily unavailable. Your saved submission is waiting.",
    "purchase_ineligible": "This purchase is no longer eligible. Contact support.",
    "queue_wait_expired": "This submission exceeded its queue waiting limit. No review was started. Contact support.",
    "uncertain_external": "Processing was interrupted and the external request outcome is uncertain. Contact support; it will not be automatically repeated.",
    "processing_timeout": "The worker exceeded its processing deadline. Contact support before retrying.",
    "queue_retries_exhausted": "Safe preparation retries were exhausted. Contact support; your inputs remain saved.",
}


class ReviewResponseLimit(HTTPException):
    def __init__(self):
        super().__init__(502, "The review could not be completed because the analysis response was too long. "
                         "No report was generated. Please try again.")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def request_key(request):
    value = request.headers.get("idempotency-key")
    if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value):
        raise HTTPException(400, "Use an Idempotency-Key of 16 to 128 letters, digits, underscores or hyphens.")
    return value or uuid.uuid4().hex


class SQLiteReportStore:
    """Explicit single-host adapter sharing the entitlement transaction boundary."""
    def __init__(self, ledger):
        if not isinstance(ledger, SQLitePurchaseLedger):
            raise HTTPException(503, "A transactional report storage adapter must be configured.")
        self.ledger = ledger
        with self._connect() as db:
            if check_version(db) == 0:
                # Legacy deployments retain their existing initialization path.
                for statement in REPORT_SCHEMA:
                    db.execute(statement)
            check_reports(db)

    @contextmanager
    def _connect(self):
        try:
            with self.ledger._connect() as db:
                yield db
        except sqlite3.Error:
            raise HTTPException(503, "Report storage is unavailable. Check saved review status before retrying.") from None

    def _row(self, db, job_id, purchase):
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT * FROM review_jobs WHERE job_id = ? AND purchase_id = ?
            AND customer_id = ? AND tier = ?""", (job_id, purchase.purchase_id,
            purchase.owner_customer_id, purchase.tier)).fetchone()
        if row is None:
            raise HTTPException(404, "Review not found.")
        result = dict(row)
        if check_version(db, validate_schema=False) >= 2:
            queued = db.execute('SELECT phase,wait_deadline FROM review_queue WHERE job_id=?', (job_id,)).fetchone()
            if queued:
                result['_queue'] = dict(queued)
        return result

    def _expire(self, db):
        now = time.time()
        # Never automatically replay a process that may still be calling a provider.
        # The deadline also fences late completions and entitlement consumption.
        queue_exclusion = (" AND NOT EXISTS (SELECT 1 FROM review_queue q WHERE q.job_id=review_jobs.job_id)"
                           if check_version(db, validate_schema=False) >= 2 else "")
        db.execute("""UPDATE review_jobs SET state='failed', error_code='interrupted',
            retryable=0, http_status=503, updated=? WHERE state='processing' AND deadline<=?""" + queue_exclusion, (now, now))
        db.execute("""UPDATE review_jobs SET delivery_state='unknown', updated=?
            WHERE delivery_state='sending' AND delivery_deadline<=?""", (now, now))

    def get(self, job_id, purchase):
        with self._connect() as db:
            self._expire(db)
            return self._row(db, job_id, purchase)

    def list(self, purchase):
        with self._connect() as db:
            self._expire(db)
            ids = db.execute("""SELECT job_id FROM review_jobs WHERE purchase_id=? AND customer_id=?
                AND tier=? ORDER BY created DESC LIMIT 20""", (purchase.purchase_id,
                purchase.owner_customer_id, purchase.tier)).fetchall()
            return [public_status(self._row(db, row[0], purchase)) for row in ids]

    def create(self, purchase, key, fingerprint):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._create(db, purchase, key, fingerprint)

    def _create(self, db, purchase, key, fingerprint):
        """Caller owns the transaction, including optional durable-input hold."""
        self._expire(db)
        existing = db.execute("SELECT job_id FROM review_request_keys WHERE purchase_id=? AND request_key=?",
                              (purchase.purchase_id, key)).fetchone()
        if existing:
            row = self._row(db, existing[0], purchase)
            if row["fingerprint"] != fingerprint:
                raise HTTPException(409, "This submission key belongs to different content. Keep the original key for retries; use a new key for corrected documents.")
            return row
        existing = db.execute("SELECT job_id FROM review_jobs WHERE purchase_id=? AND fingerprint=?",
                              (purchase.purchase_id, fingerprint)).fetchone()
        if existing:
            row = self._row(db, existing[0], purchase)
        else:
            entitlement = db.execute("""SELECT used, submission_limit FROM purchases WHERE purchase_id=?
                AND customer_id=? AND tier=? AND eligible=1 AND expires_at>?""", (purchase.purchase_id,
                purchase.owner_customer_id, purchase.tier, time.time())).fetchone()
            if not entitlement or entitlement[0] + active_holds(db, purchase.purchase_id) >= entitlement[1]:
                raise HTTPException(409, "Purchase entitlement is unavailable or already used.")
            count = db.execute("SELECT COUNT(*) FROM review_jobs WHERE purchase_id=?", (purchase.purchase_id,)).fetchone()[0]
            if count >= 20:
                raise HTTPException(429, "Submission limit reached. Please contact support.")
            job_id, now = uuid.uuid4().hex, time.time()
            db.execute("""INSERT INTO review_jobs (job_id,purchase_id,customer_id,tier,fingerprint,state,created,updated)
                VALUES (?,?,?,?,?,'pending',?,?)""", (job_id, purchase.purchase_id,
                purchase.owner_customer_id, purchase.tier, fingerprint, now, now))
            row = self._row(db, job_id, purchase)
        # Bound alternate keys even when callers replay identical content.
        count = db.execute("SELECT COUNT(*) FROM review_request_keys WHERE purchase_id=?", (purchase.purchase_id,)).fetchone()[0]
        if count >= 100:
            raise HTTPException(429, "Submission key limit reached. Reuse the original key.")
        db.execute("INSERT INTO review_request_keys VALUES (?,?,?)", (purchase.purchase_id, key, row["job_id"]))
        return row

    def claim(self, job_id, purchase, retry):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            row = self._row(db, job_id, purchase)
            if '_queue' in row:
                return None  # Only the managed queue can claim a durable queue job.
            eligible = row["state"] == "pending" or (retry and row["state"] == "failed" and row["retryable"])
            if not eligible or row["attempts"] >= MAX_ATTEMPTS:
                return None
            attempt = row["attempts"] + 1
            db.execute("""UPDATE review_jobs SET state='processing', attempts=?, deadline=?, updated=?,
                error_code=NULL, http_status=NULL, retryable=0 WHERE job_id=?""",
                (attempt, time.time() + PROCESSING_SECONDS, time.time(), job_id))
            return attempt

    def consume(self, job_id, purchase, attempt):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, job_id, purchase)
            self._queue_fence(db, row, attempt)
            if row["state"] != "processing" or row["attempts"] != attempt or row["deadline"] <= time.time():
                raise HTTPException(409, "This review attempt is no longer active.")
            if row["reservation"]:
                eligible = db.execute("""SELECT 1 FROM purchases WHERE purchase_id=? AND customer_id=?
                    AND tier=? AND eligible=1 AND expires_at>?""", (purchase.purchase_id,
                    purchase.owner_customer_id, purchase.tier, time.time())).fetchone()
                if not eligible:
                    raise HTTPException(403, "Purchase entitlement is not eligible.")
                return row["reservation"]
            submission_id = uuid.uuid4().hex
            if check_version(db) >= 1:
                hold = db.execute("SELECT state FROM review_holds WHERE job_id=?", (job_id,)).fetchone()
                if hold:
                    if hold[0] != 'active':
                        raise HTTPException(409, "This review hold is no longer active.")
                    # Remove the hold from available-capacity accounting in the
                    # SAME transaction that increments usage and links the job.
                    db.execute("UPDATE review_holds SET state='consumed',updated=? WHERE job_id=?",
                               (time.time(), job_id))
            self.ledger._reserve(db, purchase, submission_id)
            db.execute("UPDATE review_jobs SET reservation=?,updated=? WHERE job_id=?",
                       (submission_id, time.time(), job_id))
            return submission_id

    def complete(self, job_id, purchase, attempt, result_json, report_html, customer_name, quote_count):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, job_id, purchase)
            self._queue_fence(db, row, attempt)
            updated = db.execute("""UPDATE review_jobs SET state='completed', result_json=?, report_html=?,
                notification_name=?, quote_count=?, updated=? WHERE job_id=? AND purchase_id=?
                AND customer_id=? AND state='processing' AND attempts=? AND deadline>?
                AND reservation IS NOT NULL""", (result_json, report_html, customer_name, quote_count,
                time.time(), job_id, purchase.purchase_id, purchase.owner_customer_id, attempt, time.time()))
            if updated.rowcount != 1:
                raise HTTPException(409, "This review attempt cannot publish a report.")
            if '_queue' in row:
                db.execute("UPDATE review_queue SET phase='finished' WHERE job_id=?", (job_id,))

    def _queue_fence(self, db, row, attempt):
        if '_queue' not in row:
            return
        claim = getattr(self, '_queue_claim', None)
        if not claim or claim[0] != row['job_id']:
            raise HTTPException(409, 'A managed queue claim is required.')
        active = db.execute("""SELECT 1 FROM review_queue q JOIN queue_owner o ON o.singleton=1
            WHERE q.job_id=? AND q.owner_token=? AND q.claim_token=? AND o.owner_token=q.owner_token
            AND q.worker_active=1 AND q.phase='external'""", claim).fetchone()
        if not active or row['state'] != 'processing' or row['attempts'] != attempt or row['deadline'] <= time.time():
            raise HTTPException(409, 'This queue attempt is no longer active.')

    def fail(self, job_id, purchase, attempt, code, status):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, job_id, purchase)
            if '_queue' in row:
                raise HTTPException(409, 'Queue failures require the managed claim owner.')
            db.execute("""UPDATE review_jobs SET state='failed', error_code=?, http_status=?, retryable=?, updated=?
                WHERE job_id=? AND purchase_id=? AND customer_id=? AND state='processing' AND attempts=?""",
                (code, status, int(code not in {"interrupted", "storage_failed"} and attempt < MAX_ATTEMPTS),
                 time.time(), job_id, purchase.purchase_id, purchase.owner_customer_id, attempt))

    def claim_delivery(self, job_id, purchase):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            row = self._row(db, job_id, purchase)
            if row["state"] != "completed":
                raise HTTPException(409, "No completed report is available for notification.")
            if row["delivery_state"] in {"sent", "sending"}:
                return None
            if row["delivery_attempts"] >= MAX_DELIVERY_ATTEMPTS:
                raise HTTPException(409, "Notification retry limit reached. Your completed report remains available.")
            number = row["delivery_attempts"] + 1
            db.execute("""UPDATE review_jobs SET delivery_state='sending',delivery_attempts=?,delivery_deadline=?,updated=?
                WHERE job_id=?""", (number, time.time() + DELIVERY_SECONDS, time.time(), job_id))
            return number

    def finish_delivery(self, job_id, purchase, number, state):
        with self._connect() as db:
            db.execute("""UPDATE review_jobs SET delivery_state=?,updated=? WHERE job_id=? AND purchase_id=?
                AND customer_id=? AND delivery_attempts=? AND delivery_state='sending'""",
                (state, time.time(), job_id, purchase.purchase_id, purchase.owner_customer_id, number))


@dataclass(frozen=True)
class JobPurchase(AuthorizedPurchase):
    store: SQLiteReportStore
    job_id: str
    attempt: int

    def consume(self):
        try:
            return self.store.consume(self.job_id, self.purchase, self.attempt)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Purchase usage storage is unavailable.") from None


def public_status(row):
    job_id = row["job_id"]
    result = {
        "job_id": job_id, "state": row["state"], "attempts": row["attempts"],
        "review_reserved": bool(row["reservation"]),
        "retry_allowed": bool(row["state"] == "failed" and row["retryable"] and row["attempts"] < MAX_ATTEMPTS),
        "error": ({"code": row["error_code"], "message": ERROR_MESSAGES.get(row["error_code"], ERROR_MESSAGES["analysis_failed"])}
                  if row["error_code"] else None),
        "notification": {"audience": "internal_business", "state": row["delivery_state"],
                         "attempts": row["delivery_attempts"],
                         "retry_allowed": row["state"] == "completed" and row["delivery_state"] in {"failed", "unconfigured", "unknown", "pending"}
                         and row["delivery_attempts"] < MAX_DELIVERY_ATTEMPTS},
        "status_url": f"/jobs/{job_id}",
        "report_url": f"/jobs/{job_id}/report" if row["state"] == "completed" else None,
        "result_url": f"/jobs/{job_id}/result" if row["state"] == "completed" else None,
    }
    if '_queue' in row:
        result['state'] = 'queued' if row['state'] == 'pending' else row['state']
        result['processing_paused'] = row['_queue']['phase'] == 'paused'
        result['queue_wait_deadline'] = row['_queue']['wait_deadline']
        result['external_outcome_uncertain'] = row['_queue']['phase'] == 'uncertain'
        result['retry_allowed'] = False  # No unattended replay of uncertain work.
    return result


def notify(store, row, purchase, sender, analysis_type, original=None):
    number = store.claim_delivery(row["job_id"], purchase)
    if number is None:
        return
    try:
        kwargs = original or dict(customer_name=row["notification_name"], customer_email="",
                                 package_key=row["tier"], file_names=["quote"] * row["quote_count"],
                                 analysis=analysis_type.model_validate_json(row["result_json"]))
        sent = sender(**kwargs, report_html=row["report_html"])
        state = "unconfigured" if sent is False else "sent"
    except Exception:
        # Provider text can contain recipient addresses or credentials. Never save/log it.
        state = "failed"
    store.finish_delivery(row["job_id"], purchase, number, state)


def preserve_report(authorization, renderer, sender, **kwargs):
    """Called after existing finalization, before any notification side effect."""
    if not isinstance(authorization, JobPurchase):
        # Explicit internal Python callers retain their existing test contract.
        # Both public submission routes always supply a JobPurchase.
        sender(**kwargs)
        return
    report_html = renderer(kwargs["analysis"], quote_count=len(kwargs["file_names"]))
    try:
        authorization.store.complete(authorization.job_id, authorization.purchase, authorization.attempt,
                                     kwargs["analysis"].model_dump_json(), report_html,
                                     kwargs["customer_name"], len(kwargs["file_names"]))
    except Exception:
        authorization.store.fail(authorization.job_id, authorization.purchase, authorization.attempt, "storage_failed", 503)
        raise HTTPException(503, ERROR_MESSAGES["storage_failed"]) from None
    row = authorization.store.get(authorization.job_id, authorization.purchase)
    # A notification storage/provider failure must not undo a saved report.
    try:
        notify(authorization.store, row, authorization.purchase, sender, type(kwargs["analysis"]), kwargs)
    except Exception:
        pass  # Durable pending/sending state remains visible and recoverable.


def response_for(row, kind):
    headers = {**PRIVATE_HEADERS, "X-Review-Job-ID": row["job_id"], "Location": f"/jobs/{row['job_id']}"}
    if row["state"] == "completed":
        if kind == "html":
            return HTMLResponse(row["report_html"], headers=headers)
        return JSONResponse(json.loads(row["result_json"]), headers=headers)
    status = 202 if row["state"] in {"pending", "processing"} else (row["http_status"] or 500)
    return JSONResponse(public_status(row), status_code=status, headers={**headers, "Retry-After": "3"})


async def submit(request, authorization, fingerprint, operation, kind):
    """Run bounded in-memory work independent of the HTTP client's lifetime.

    Completion is durable; queued inputs deliberately are not. A dead process
    leaves an expiring, fenced processing record, never an automatic paid replay.
    """
    key = request_key(request)
    retry_value = request.headers.get("x-review-retry", "false")
    if retry_value not in {"true", "false"}:
        raise HTTPException(400, "X-Review-Retry must be true or false.")
    if retry_value == "true" and "idempotency-key" not in request.headers:
        raise HTTPException(400, "Retry requires the original Idempotency-Key and unchanged documents.")
    store = SQLiteReportStore(authorization.ledger)
    row = store.create(authorization.purchase, key, fingerprint)
    if not _SLOTS.acquire(blocking=False):
        return response_for(row, kind) if row["state"] != "pending" else JSONResponse(
            public_status(row), status_code=503, headers={**PRIVATE_HEADERS, "X-Review-Job-ID": row["job_id"], "Retry-After": "3"})
    try:
        attempt = store.claim(row["job_id"], authorization.purchase, retry_value == "true")
    except BaseException:
        _SLOTS.release()
        raise
    if attempt is None:
        _SLOTS.release()
        return response_for(store.get(row["job_id"], authorization.purchase), kind)
    grant = JobPurchase(authorization.purchase, authorization.ledger, store, row["job_id"], attempt)
    future = Future()

    def work():
        try:
            asyncio.run(operation(grant))
            final = store.get(grant.job_id, grant.purchase)
            if final["state"] == "processing":
                raise RuntimeError("Analysis did not publish a complete report")
            future.set_result(None)
        except BaseException as exc:
            status = exc.status_code if isinstance(exc, HTTPException) else (504 if isinstance(exc, (TimeoutError, APITimeoutError)) else 500)
            code = "input_rejected" if status < 500 else ("timeout" if status == 504 or isinstance(exc, (TimeoutError, APITimeoutError)) else "analysis_failed")
            detail = exc.detail if isinstance(exc, HTTPException) else None
            if isinstance(detail, dict):
                detail = detail.get("message")
            if detail == BETTER_PHOTO:
                code = "unreadable_photo"
            if isinstance(exc, (ReviewResponseLimit, LengthFinishReasonError)):
                code, status = "response_length", 502
            try:
                store.fail(grant.job_id, grant.purchase, attempt, code, status)
            except Exception:
                pass  # A storage outage leaves a fenced processing record for support.
            future.set_exception(exc)
        finally:
            _SLOTS.release()

    try:
        threading.Thread(target=work, name="review-worker", daemon=True).start()
    except RuntimeError:
        _SLOTS.release()
        store.fail(grant.job_id, grant.purchase, attempt, "analysis_failed", 503)
        raise HTTPException(503, "The review could not start. Check its status before retrying.") from None
    # Poll a thread-safe Future rather than binding its completion callback to
    # the request's event loop, which may close after a disconnect or 202.
    deadline = time.monotonic() + WAIT_SECONDS
    while not future.done() and time.monotonic() < deadline:
        await asyncio.sleep(min(0.02, max(0, deadline - time.monotonic())))
    if future.done():
        try:
            future.result()
        except HTTPException as exc:
            # Existing intake/provider translators contain safe, actionable guidance.
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers={**PRIVATE_HEADERS, "X-Review-Job-ID": grant.job_id,
                                         "Location": f"/jobs/{grant.job_id}"})
        except BaseException:
            pass
    return response_for(store.get(grant.job_id, grant.purchase), kind)
