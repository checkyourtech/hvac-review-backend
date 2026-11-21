"""Opt-in single-process queue runtime; no production verifier is supplied.

Trusted startup must explicitly inject PrivateUploadStore, a server-side
PurchaseRevalidator and the accepted processor, and confirm a private persistent
volume plus ONE application process. Nothing reads an environment enable flag.
Stage 3/Wix must implement revalidate(purchase_id) using server credentials and
authoritative owner, product mapping, payment/refund, quantity and expiry data.
Its transport must have bounded timeouts. It must never use stored customer
bearer tokens or trust browser payment callbacks. No such adapter exists here.

Two fixed worker threads and one scheduler are managed together. The scheduler
polls SQLite; notifications are only an optimization. A private flock beside
the canonical ledger is held until ALL threads exit. A stuck thread cannot be
killed safely: stop() reports False and keeps the lock; terminate/restart the
process under supervision instead. A lease/deadline never replaces a live worker.

After the process lock is acquired, interrupted pre-external preparation can be
retried within the attempt limit. Once the external boundary was durably marked,
an interrupted attempt is uncertain and is never automatically replayed.
Exactly-once external provider execution is NOT guaranteed.
"""
import asyncio
from contextlib import asynccontextmanager
import fcntl
import logging
import os
import queue as dispatch_queue
import threading
import time
from typing import Protocol
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool

from document_intake import BETTER_PHOTO
from durable_uploads import PrivateUploadStore, _directory, _private
from purchase_security import AuthorizedPurchase, EntitlementService, VerifiedPurchase
from queue_store import ClaimedReportStore, SQLiteReviewQueue
from report_jobs import JobPurchase, ReviewResponseLimit


class RevalidatedPurchase(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    purchase_id: StrictStr = Field(min_length=1, max_length=200)
    owner_customer_id: StrictStr = Field(min_length=1, max_length=200)
    tier: StrictStr
    payment_eligible: StrictBool
    expires_at: StrictInt = Field(gt=0)
    submission_limit: StrictInt = Field(ge=1)


class PurchaseRevalidator(Protocol):
    def revalidate(self, purchase_id: str) -> RevalidatedPurchase:
        """Authoritative server lookup; unavailable/unknown results fail closed.

        Return ineligible for refunded/cancelled purchases. Raise an exception
        for unavailable verification; do not return previously cached browser
        claims as a substitute. Owner/product/quantity must remain consistent.
        """
        ...


class QueueRuntime:
    def __init__(self, storage, verifier, processor, *, deployment_confirmed=False,
                 poll_seconds=0.5, wait_seconds=86400, processing_seconds=900, retry_seconds=60):
        if not isinstance(storage, PrivateUploadStore) or not callable(processor):
            raise ValueError('Explicit private storage and processor are required')
        if poll_seconds <= 0:
            raise ValueError('A positive scheduler polling interval is required')
        self.storage, self.verifier, self.processor = storage, verifier, processor
        self.records = SQLiteReviewQueue(storage.records.ledger, wait_seconds=wait_seconds,
            processing_seconds=processing_seconds, retry_seconds=retry_seconds)
        storage.records = self.records
        self.deployment_confirmed = deployment_confirmed is True
        self.poll_seconds = poll_seconds
        self.owner = uuid.uuid4().hex
        self._lock_fd = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._faulted = threading.Event()
        self._dispatch = dispatch_queue.Queue(maxsize=2)
        self._guard = threading.Lock()
        self._inflight = set()
        self._threads = []
        self._started = False
        self._healthy = False

    @property
    def ready(self):
        return (self._started and not self._stop.is_set() and not self._faulted.is_set() and self._healthy
                and len(self._threads) == 3 and all(thread.is_alive() for thread in self._threads)
                and callable(getattr(self.verifier, 'revalidate', None)))

    def require_accepting(self, authorization):
        if not self.ready or authorization.ledger is not self.records.ledger:
            raise HTTPException(503, 'The durable review queue is unavailable. No new submission was accepted.')

    def _acquire_process_lock(self):
        path = self.records.ledger.path
        if path != path.resolve() or path.stat().st_nlink != 1:
            raise OSError('Canonical private ledger required')
        parent = _directory(path.parent)
        try:
            _private(os.fstat(parent), directory=True)
            fd = os.open(path.name + '.queue.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                _private(os.fstat(fd))
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(fd)
                raise
            self._lock_fd = fd
        finally:
            os.close(parent)

    def start(self):
        if self._started:
            if not self.ready:
                raise HTTPException(503, 'This queue runtime needs a controlled process restart.')
            return
        if self._stop.is_set() or not self.deployment_confirmed or not callable(getattr(self.verifier, 'revalidate', None)):
            raise HTTPException(503, 'Queue activation requires persistent single-process deployment and trusted purchase revalidation.')
        try:
            with self.storage._locked():
                pass
            self._acquire_process_lock()
            self.records.recover(self.owner)
            self._threads = [threading.Thread(target=self._worker, name=f'cyt-queue-worker-{n}', daemon=True) for n in (1, 2)]
            self._threads.append(threading.Thread(target=self._schedule, name='cyt-queue-scheduler', daemon=True))
            self._started, self._healthy = True, True
            for thread in self._threads:
                thread.start()
        except BaseException:
            self._stop.set()
            for thread in self._threads:
                if thread.ident is not None:
                    thread.join(timeout=1)
            if not any(thread.is_alive() for thread in self._threads) and self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            self._healthy = False
            raise HTTPException(503, 'The queue could not start safely. Check storage, schema, configuration and scheduler ownership.') from None

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            self._healthy = False
            return False
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self._healthy = False
        return True

    def wake(self):
        self._wake.set()

    def _schedule(self):
        while not self._stop.is_set():
            try:
                if self._faulted.is_set():
                    self._healthy = False
                    return
                if not all(thread.is_alive() for thread in self._threads[:2]):
                    self._healthy = False
                    return  # Do not manufacture a replacement for an unknown worker.
                self.records.tick(self.owner)
                with self._guard:
                    capacity = 2 - len(self._inflight)
                for _ in range(capacity):
                    if self._stop.is_set():
                        break
                    claim = self.records.claim_next(self.owner)
                    if claim is None:
                        break
                    with self._guard:
                        self._inflight.add(claim['job_id'])
                    self._dispatch.put_nowait(claim)
                self._healthy = True
            except Exception:
                # No exception text, request body, customer or credential logging.
                self._healthy = False
                # A failed claim commit can be ambiguous. Restart reconciliation
                # must decide its state before dispatch resumes.
                self._faulted.set()
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _revalidate(self, claim):
        try:
            verified = self.verifier.revalidate(claim['purchase_id'])
            if not isinstance(verified, RevalidatedPurchase):
                raise ValueError('Invalid trusted verification result')
        except Exception:
            raise HTTPException(503, 'Trusted purchase revalidation is unavailable.') from None
        if (verified.purchase_id != claim['purchase_id'] or verified.owner_customer_id != claim['customer_id']
                or verified.tier != claim['tier'] or verified.tier not in {'tier1', 'tier2'}):
            raise HTTPException(403, 'Purchase ownership or product could not be revalidated.')
        purchase = VerifiedPurchase(**verified.model_dump(), authenticated_customer_id=verified.owner_customer_id)
        # Authenticated identity came from the original verified submission;
        # current ownership/payment came from the trusted server-side lookup.
        if not purchase.payment_eligible or purchase.expires_at <= time.time():
            self.records.ledger.revoke(purchase.purchase_id)
            raise HTTPException(403, 'Purchase entitlement is no longer eligible.')
        self.records.ledger.register(purchase)  # Replays cannot replenish usage or alter quantity.
        return AuthorizedPurchase(purchase, self.records.ledger)

    def _run_claim(self, claim):
        external = False
        try:
            if self._stop.is_set():
                self.records.fail_claim(claim, 'verification_unavailable', 503, retry=True)
                return
            authorization = self._revalidate(claim)
            manifest, files = self.storage.load(authorization, claim['job_id'])
            if self._stop.is_set():
                self.records.fail_claim(claim, 'verification_unavailable', 503, retry=True)
                return
            # Conservatively mark before invoking the existing extraction/AI
            # pipeline, including image transcription and quote classification.
            self.records.mark_external(claim)
            external = True
            bound = ClaimedReportStore(self.records, claim)
            grant = JobPurchase(authorization.purchase, authorization.ledger, bound, claim['job_id'], claim['attempts'])
            asyncio.run(self.processor(manifest, files, grant))
            if bound.get(claim['job_id'], authorization.purchase)['state'] != 'completed':
                raise RuntimeError('No complete report was published')
        except BaseException as error:
            status = error.status_code if isinstance(error, HTTPException) else 503
            detail = error.detail if isinstance(error, HTTPException) else None
            if isinstance(detail, dict):
                detail = detail.get('message')
            definite = status in {400, 413, 415, 422}
            code = 'unreadable_photo' if detail == BETTER_PHOTO else ('input_rejected' if definite else 'verification_unavailable')
            if not external and status in {403, 409}:
                code, definite = 'purchase_ineligible', True
            if isinstance(error, ReviewResponseLimit):
                code, definite = 'response_length', True
            try:
                self.records.fail_claim(claim, code, status, retry=not external and not definite, definite=definite)
            except Exception:
                self._healthy = False  # Persistent record remains fenced for restart/support.
                self._faulted.set()

    def _worker(self):
        while not self._stop.is_set() or not self._dispatch.empty():
            try:
                claim = self._dispatch.get(timeout=0.1)
            except dispatch_queue.Empty:
                continue
            try:
                self._run_claim(claim)
            finally:
                try:
                    self.records.finish_worker(claim)
                except Exception:
                    self._healthy = False
                    self._faulted.set()
                with self._guard:
                    self._inflight.discard(claim['job_id'])
                self._dispatch.task_done()
                self._wake.set()


def installed_queue(application):
    runtime = getattr(application.state, 'durable_queue', None)
    if not getattr(application.state, 'queue_enabled', False):
        if isinstance(runtime, QueueRuntime) and runtime._started:
            raise HTTPException(503, 'Changing queue mode requires a controlled process restart.')
        return None
    if not isinstance(runtime, QueueRuntime):
        raise HTTPException(503, 'Durable queue dependencies are not configured.')
    service = getattr(application.state, 'entitlements', None)
    if not isinstance(service, EntitlementService) or service.ledger is not runtime.records.ledger:
        raise HTTPException(503, 'Queue purchase verification is not configured.')
    return runtime


@asynccontextmanager
async def queue_lifespan(application):
    runtime = installed_queue(application)
    if runtime is not None:
        await run_in_threadpool(runtime.start)
    try:
        yield
    finally:
        if runtime is not None and not await run_in_threadpool(runtime.stop):
            logging.getLogger(__name__).warning('Queue shutdown incomplete: worker threads remain active; process restart required.')
