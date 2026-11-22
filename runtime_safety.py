"""Hosting-independent privacy defaults and local dependency health checks.

No provider provisioning, external health probes, credentials or customer data.
Readiness here is local readiness, not approval to expose the app to customers.
"""
import logging

import httpx
from fastapi.responses import JSONResponse

from purchase_security import EntitlementService, SQLitePurchaseLedger
from sqlite_migrations import require_reconciled


# The SDK otherwise retries twice and permits ten minutes per attempt. Keep the
# accepted application-level length retry and explicit job retry as the owners.
# HTTPX timeouts bound I/O phases, not total job wall time; job fencing remains.
ANALYSIS_TIMEOUT = httpx.Timeout(120.0, connect=5.0, write=30.0, pool=5.0)
PRIVATE_LOG_NAMES = ("openai", "httpx", "httpcore", "uvicorn.access")


def configure_private_logging():
    """Discard raw transport/debug and URL/IP access logs, including child logs.

    SDK debug records can contain entire prompts/images; redacting credentials
    alone is insufficient. Application-owned sanitized events remain enabled.
    Deployment logging must not replace these rules with request/body tracing.
    """
    for name in PRIVATE_LOG_NAMES:
        logger = logging.getLogger(name)
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.WARNING)
        for child_name, child in list(logging.root.manager.loggerDict.items()):
            if child_name.startswith(name + ".") and isinstance(child, logging.Logger):
                child.handlers.clear()
                child.propagate = True
                child.setLevel(logging.NOTSET)


def local_readiness(application):
    """Check configured local dependencies, without creating data or calling APIs."""
    service = getattr(application.state, "entitlements", None)
    ready = False
    if isinstance(service, EntitlementService) and isinstance(service.ledger, SQLitePurchaseLedger):
        try:
            # A readiness read must not compete for the scheduler's writer lock.
            # Allow at most 50 ms for a brief SQLite commit, never its normal
            # five-second busy wait. Render restarts use /health/live instead.
            with service.ledger._connect(timeout=0.05) as db:
                db.execute("BEGIN")
                require_reconciled(db)
                db.execute("SELECT used,eligible FROM purchases LIMIT 0")
                db.execute("SELECT submission_id FROM submissions LIMIT 0")
                db.execute("SELECT state,result_json,report_html FROM review_jobs LIMIT 0")
                db.execute("SELECT request_key FROM review_request_keys LIMIT 0")
                db.rollback()
            ready = callable(getattr(service.verifier, "verify", None))
            if getattr(application.state, 'queue_enabled', False) or getattr(application.state, 'durable_queue', None) is not None:
                from queue_runtime import installed_queue
                runtime = installed_queue(application)
                ready = ready and runtime is not None and runtime.ready
                if ready:
                    with runtime.storage._locked(blocking=False):
                        pass
        except Exception:
            ready = False  # Never expose a database path, exception or verifier configuration.
    return JSONResponse({"status": "ready" if ready else "unavailable"},
                        status_code=200 if ready else 503, headers={"Cache-Control": "no-store"})
