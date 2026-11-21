"""Explicit, additive SQLite migrations. Never repair an unknown/partial schema.

Version zero is the accepted pre-queue schema. Existing installations must call
migrate_stage1(ledger) during a controlled maintenance window before using the
internal durable-input adapter. Opening a ledger never performs this migration.
"""
import sqlite3

SCHEMA_VERSION = 1
REPORT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS review_jobs (
        job_id TEXT PRIMARY KEY, purchase_id TEXT NOT NULL REFERENCES purchases(purchase_id),
        customer_id TEXT NOT NULL, tier TEXT NOT NULL, fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','processing','completed','failed')),
        attempts INTEGER NOT NULL DEFAULT 0, reservation TEXT, deadline REAL,
        created REAL NOT NULL, updated REAL NOT NULL, error_code TEXT, http_status INTEGER,
        retryable INTEGER NOT NULL DEFAULT 0, result_json TEXT, report_html TEXT,
        notification_name TEXT, quote_count INTEGER, delivery_state TEXT NOT NULL DEFAULT 'pending',
        delivery_attempts INTEGER NOT NULL DEFAULT 0, delivery_deadline REAL,
        UNIQUE(purchase_id, fingerprint))""",
    """CREATE TABLE IF NOT EXISTS review_request_keys (
        purchase_id TEXT NOT NULL REFERENCES purchases(purchase_id), request_key TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES review_jobs(job_id), PRIMARY KEY(purchase_id, request_key))""",
)
STAGE1_SCHEMA = (
    """CREATE TABLE queue_storage (
        root_id TEXT PRIMARY KEY, root_path TEXT NOT NULL UNIQUE, created REAL NOT NULL)""",
    """CREATE TABLE review_inputs (
        job_id TEXT PRIMARY KEY REFERENCES review_jobs(job_id),
        root_id TEXT NOT NULL REFERENCES queue_storage(root_id), bundle_id TEXT NOT NULL,
        manifest_hash TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK(byte_count > 0),
        quote_count INTEGER NOT NULL CHECK(quote_count BETWEEN 1 AND 3),
        created REAL NOT NULL, UNIQUE(root_id, bundle_id))""",
    """CREATE TABLE review_holds (
        job_id TEXT PRIMARY KEY REFERENCES review_jobs(job_id),
        purchase_id TEXT NOT NULL REFERENCES purchases(purchase_id),
        state TEXT NOT NULL CHECK(state IN ('active','consumed','released')),
        created REAL NOT NULL, updated REAL NOT NULL)""",
    "CREATE INDEX review_holds_purchase_state ON review_holds(purchase_id, state)",
    "CREATE INDEX review_inputs_created ON review_inputs(created)",
)


def check_version(db, *, require_stage1=False, validate_schema=True):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION) or (require_stage1 and version != SCHEMA_VERSION):
        raise sqlite3.OperationalError("Unsupported database schema version")
    if version == SCHEMA_VERSION and validate_schema:
        # Missing objects are corruption/configuration errors, never recreated.
        db.execute("SELECT root_id,root_path,created FROM queue_storage LIMIT 0")
        db.execute("SELECT job_id,root_id,bundle_id,manifest_hash,byte_count,quote_count,created FROM review_inputs LIMIT 0")
        db.execute("SELECT job_id,purchase_id,state,created,updated FROM review_holds LIMIT 0")
        check_reports(db)
    return version


def check_reports(db):
    db.execute("""SELECT job_id,purchase_id,customer_id,tier,fingerprint,state,attempts,reservation,
        deadline,created,updated,error_code,http_status,retryable,result_json,report_html,
        notification_name,quote_count,delivery_state,delivery_attempts,delivery_deadline
        FROM review_jobs LIMIT 0""")
    db.execute("SELECT purchase_id,request_key,job_id FROM review_request_keys LIMIT 0")


def active_holds(db, purchase_id):
    if db.execute("PRAGMA user_version").fetchone()[0] == 0:
        return 0
    return db.execute("SELECT COUNT(*) FROM review_holds WHERE purchase_id=? AND state='active'",
                      (purchase_id,)).fetchone()[0]


def migrate_stage1(ledger):
    """Atomic additive migration; no executescript implicit transaction commits."""
    with ledger._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if check_version(db) == SCHEMA_VERSION:
            return
        db.execute("SELECT purchase_id,customer_id,tier,submission_limit,used,expires_at,eligible FROM purchases LIMIT 0")
        db.execute("SELECT submission_id,purchase_id,started_at FROM submissions LIMIT 0")
        for statement in REPORT_SCHEMA:
            db.execute(statement)
        check_reports(db)
        for statement in STAGE1_SCHEMA:
            db.execute(statement)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        check_version(db, require_stage1=True)
