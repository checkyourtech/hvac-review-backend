"""SQLite-authoritative queue claims and fenced report publication (one host).

The scheduler's process lock, not a deadline/lease, establishes exclusive
ownership. A timed-out worker retains its occupied slot until it actually exits.
The caller must hold that process lock throughout recovery and worker lifetime.
"""
import time
import uuid
import sqlite3

from fastapi import HTTPException
from queue_inputs import SQLiteQueuedInputs
from report_jobs import MAX_ATTEMPTS, SQLiteReportStore
from sqlite_migrations import check_version


class SQLiteReviewQueue(SQLiteQueuedInputs):
    def __init__(self, ledger, *, wait_seconds=86400, processing_seconds=900, retry_seconds=60):
        super().__init__(ledger)
        with self._connect() as db:
            if check_version(db) != 2:
                raise HTTPException(503, 'An explicit Stage 2 database migration is required.')
        if min(wait_seconds, processing_seconds, retry_seconds) <= 0:
            raise ValueError('Queue time limits must be positive')
        self.wait_seconds = wait_seconds
        self.processing_seconds = processing_seconds
        self.retry_seconds = retry_seconds

    def _registered(self, db, row):
        if db.execute('SELECT 1 FROM review_queue WHERE job_id=?', (row['job_id'],)).fetchone():
            return row
        hold = db.execute("SELECT 1 FROM review_holds WHERE job_id=? AND state='active'", (row['job_id'],)).fetchone()
        if row['state'] != 'pending' or row['attempts'] or row['reservation'] or not hold:
            raise HTTPException(409, 'This submission cannot enter the queue.')
        now = time.time()
        db.execute("INSERT INTO review_queue (job_id,phase,ready_at,wait_deadline) VALUES (?,'ready',?,?)",
                   (row['job_id'], now, now + self.wait_seconds))
        return row

    def _released(self, db, job_id):
        row = db.execute('SELECT worker_active,phase FROM review_queue WHERE job_id=?', (job_id,)).fetchone()
        if row and (row[0] or row[1] == 'uncertain'):
            # Stage 1's release transaction rolls back as a whole. Corrupt inputs
            # discovered by a replay must not release an uncertain/live claim.
            raise HTTPException(409, 'This queue attempt requires reconciliation before releasing its hold.')
        db.execute("UPDATE review_queue SET phase='failed' WHERE job_id=? AND worker_active=0", (job_id,))

    def _owner(self, db, owner):
        if not db.execute('SELECT 1 FROM queue_owner WHERE singleton=1 AND owner_token=?', (owner,)).fetchone():
            raise HTTPException(409, 'This scheduler no longer owns the queue.')

    def _terminal(self, db, job_id, phase, code, status, *, release=False):
        if db.execute("SELECT 1 FROM review_jobs WHERE job_id=? AND state='completed'", (job_id,)).fetchone():
            return
        db.execute("UPDATE review_queue SET phase=? WHERE job_id=?", (phase, job_id))
        db.execute("""UPDATE review_jobs SET state='failed',error_code=?,http_status=?,retryable=0,updated=?
            WHERE job_id=?""", (code, status, time.time(), job_id))
        if release:
            db.execute("""UPDATE review_holds SET state='released',updated=? WHERE job_id=? AND state='active'
                AND EXISTS (SELECT 1 FROM review_jobs j WHERE j.job_id=review_holds.job_id AND j.reservation IS NULL)""",
                       (time.time(), job_id))

    def recover(self, owner):
        """Only after exclusive OS lock acquisition proves prior workers exited."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("""SELECT 1 FROM review_jobs j WHERE j.state='processing' AND NOT EXISTS
                (SELECT 1 FROM review_queue q WHERE q.job_id=j.job_id) LIMIT 1""").fetchone():
                raise HTTPException(503, 'Legacy processing jobs need reconciliation before enabling the queue.')
            db.execute('INSERT OR REPLACE INTO queue_owner VALUES (1,?)', (owner,))
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT j.job_id,j.state,j.attempts,j.reservation,q.external_started,q.wait_deadline,q.phase
                FROM review_queue q JOIN review_jobs j USING(job_id)
                WHERE q.worker_active=1 OR q.phase IN ('preparing','external')""").fetchall()
            for row in rows:
                job_id = row['job_id']
                if row['state'] == 'completed':
                    db.execute("UPDATE review_queue SET phase='finished' WHERE job_id=?", (job_id,))
                elif row['state'] == 'failed' and row['phase'] in {'failed', 'uncertain'}:
                    pass
                elif row['external_started'] or row['reservation']:
                    self._terminal(db, job_id, 'uncertain', 'uncertain_external', 503)
                elif row['state'] == 'pending' and row['phase'] == 'paused' and row['attempts'] < MAX_ATTEMPTS and row['wait_deadline'] > time.time():
                    pass  # A recorded safe retry survives a crash during worker cleanup.
                elif row['state'] == 'processing' and row['attempts'] < MAX_ATTEMPTS and row['wait_deadline'] > time.time():
                    db.execute("UPDATE review_queue SET phase='ready',retry_at=0 WHERE job_id=?", (job_id,))
                    db.execute("""UPDATE review_jobs SET state='pending',deadline=NULL,error_code=NULL,
                        http_status=NULL,retryable=0,updated=? WHERE job_id=?""", (time.time(), job_id))
                else:
                    self._terminal(db, job_id, 'failed', 'queue_retries_exhausted', 503, release=True)
                db.execute('UPDATE review_queue SET worker_active=0,claim_token=NULL,owner_token=NULL WHERE job_id=?', (job_id,))
            self._expire_queue(db)

    def _expire_queue(self, db):
        now = time.time()
        waiting = db.execute("SELECT job_id FROM review_queue WHERE phase IN ('ready','paused') AND wait_deadline<=?", (now,)).fetchall()
        for row in waiting:
            self._terminal(db, row[0], 'failed', 'queue_wait_expired', 503, release=True)
        active = db.execute("""SELECT q.job_id,q.external_started FROM review_queue q JOIN review_jobs j USING(job_id)
            WHERE q.worker_active=1 AND j.state='processing' AND j.deadline<=?""", (now,)).fetchall()
        for row in active:
            self._terminal(db, row[0], 'uncertain' if row[1] else 'failed',
                           'uncertain_external' if row[1] else 'processing_timeout', 504, release=not row[1])
        # worker_active intentionally remains 1: a deadline does not kill a thread.

    def claim_next(self, owner):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owner(db, owner)
            self._expire_queue(db)
            if db.execute('SELECT COUNT(*) FROM review_queue WHERE worker_active=1').fetchone()[0] >= 2:
                return None
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT j.* FROM review_queue q JOIN review_jobs j USING(job_id)
                WHERE q.phase IN ('ready','paused') AND q.worker_active=0 AND q.retry_at<=? AND j.state='pending'
                AND j.attempts<? ORDER BY q.ready_at,j.job_id LIMIT 1""", (time.time(), MAX_ATTEMPTS)).fetchone()
            if row is None:
                return None
            row = dict(row)
            token, now = uuid.uuid4().hex, time.time()
            db.execute("""UPDATE review_queue SET phase='preparing',owner_token=?,claim_token=?,
                worker_active=1,external_started=0 WHERE job_id=?""", (owner, token, row['job_id']))
            db.execute("""UPDATE review_jobs SET state='processing',attempts=attempts+1,deadline=?,
                error_code=NULL,http_status=NULL,retryable=0,updated=? WHERE job_id=?""",
                       (now + self.processing_seconds, now, row['job_id']))
            row.update(attempts=row['attempts'] + 1, claim_token=token, owner_token=owner)
            return row

    def _owned(self, db, claim):
        self._owner(db, claim['owner_token'])
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT j.*,q.phase,q.external_started,q.wait_deadline FROM review_jobs j
            JOIN review_queue q USING(job_id) WHERE q.job_id=? AND q.owner_token=? AND q.claim_token=?
            AND q.worker_active=1""", (claim['job_id'], claim['owner_token'], claim['claim_token'])).fetchone()
        if row is None or row['attempts'] != claim['attempts']:
            raise HTTPException(409, 'This queue claim is no longer active.')
        return dict(row)

    def mark_external(self, claim):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned(db, claim)
            if row['state'] != 'processing' or row['phase'] != 'preparing' or row['deadline'] <= time.time():
                raise HTTPException(409, 'This queue attempt cannot start external work.')
            db.execute("UPDATE review_queue SET phase='external',external_started=1 WHERE job_id=?", (row['job_id'],))

    def fail_claim(self, claim, code, status, *, retry=False, definite=False):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned(db, claim)
            if row['state'] != 'processing':
                return  # Never overwrite a saved report or a fenced timeout.
            if row['external_started']:
                self._terminal(db, row['job_id'], 'failed' if definite else 'uncertain',
                               code if definite else 'uncertain_external', status, release=definite)
            elif retry and row['attempts'] < MAX_ATTEMPTS and row['wait_deadline'] > time.time():
                db.execute("UPDATE review_queue SET phase='paused',retry_at=? WHERE job_id=?",
                           (time.time() + self.retry_seconds, row['job_id']))
                db.execute("""UPDATE review_jobs SET state='pending',error_code=?,http_status=?,
                    deadline=NULL,updated=? WHERE job_id=?""", (code, status, time.time(), row['job_id']))
            else:
                self._terminal(db, row['job_id'], 'failed', 'queue_retries_exhausted' if retry else code,
                               status, release=True)

    def finish_worker(self, claim):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, claim)
            db.execute('UPDATE review_queue SET worker_active=0 WHERE job_id=?', (claim['job_id'],))

    def tick(self, owner):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owner(db, owner)
            self._expire_queue(db)


class ClaimedReportStore(SQLiteReportStore):
    """Existing report/entitlement operations with a non-persisted claim capability."""
    def __init__(self, queue, claim):
        super().__init__(queue.ledger)
        self.queue, self.claim = queue, claim
        self._queue_claim = (claim['job_id'], claim['owner_token'], claim['claim_token'])

    def fail(self, job_id, purchase, attempt, code, status):
        if job_id != self.claim['job_id'] or attempt != self.claim['attempts']:
            raise HTTPException(409, 'This queue attempt is no longer active.')
        self.queue.fail_claim(self.claim, code, status)
