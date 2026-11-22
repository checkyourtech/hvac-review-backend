"""Closed Render entry point: uvicorn deployment_app:create_app --factory.

No environment value installs a verifier. A future reviewed integration must
supply a trusted verifier object to create_app; there is no test-proof bypass or
dynamic module loader. Merely setting CYT_QUEUE_ENABLED=true fails closed.
"""
import os

from fastapi import FastAPI

from deployment_storage import DeploymentConfig, DeploymentError, open_storage
from purchase_security import EntitlementService
from queue_http import process_saved_submission
from queue_runtime import QueueRuntime, queue_lifespan


def create_app(config=None, *, verifier=None, processor=process_saved_submission):
    config = config or DeploymentConfig.from_env()
    if verifier is not None and not config.queue_enabled:
        # Never accidentally enable the non-durable legacy submission path.
        raise DeploymentError('Purchase access requires the approved durable queue configuration.')
    if config.queue_enabled and (verifier is None or not callable(getattr(verifier, 'verify', None))
                                 or not callable(getattr(verifier, 'revalidate', None))):
        raise DeploymentError('Trusted purchase verification and revalidation are not configured.')

    ledger = reports = storage = None
    try:
        ledger, reports, storage = open_storage(config)
    except Exception:
        if config.queue_enabled:
            raise DeploymentError('Persistent storage is not ready; paid access remains closed.') from None
        # A closed service must boot so the operator can provision its new disk.
        # It has NO EntitlementService and cannot serve/consume paid reviews.

    # Do not discover or read a developer .env in this entry point.
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    import main

    application = FastAPI(title=main.app.title, lifespan=queue_lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    # Reuse every accepted route, static mount, intake middleware and handler.
    # OpenAPI/documentation endpoints from the development app are excluded.
    application.router.routes.extend(route for route in main.app.router.routes
        if getattr(route, 'path', None) not in {'/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc'})
    application.user_middleware = list(main.app.user_middleware)
    application.exception_handlers.update(main.app.exception_handlers)
    application.state.queue_enabled = config.queue_enabled
    application.state.deployment_storage_ready = storage is not None
    if config.queue_enabled:
        application.state.entitlements = EntitlementService(verifier, ledger)
        application.state.report_store = reports
        application.state.durable_queue = QueueRuntime(storage, verifier, processor, deployment_confirmed=True)
    return application
