"""Private Velo-to-backend handoff and an unprivileged embedded bootstrap.

The bridge secret is server-only. Member ID comes from Wix currentMember,
never page input. No tokens in redirects, cookies, browser storage or SQLite.
Routes are installed only with the explicitly configured durable queue.
"""
import hmac
from html import escape
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError
from starlette.concurrency import run_in_threadpool

from purchase_security import InvalidPurchaseProof
from wix_purchase import WixPurchaseVerifier

PRIVATE_HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                   'X-Content-Type-Options': 'nosniff'}


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    purchaseId: StrictStr
    memberId: StrictStr


def install_wix_handoff(application, verifier):
    if not isinstance(verifier, WixPurchaseVerifier) or not application.state.queue_enabled:
        raise ValueError('A configured durable Wix integration is required.')
    router = APIRouter()

    def failure(status, detail):
        return JSONResponse({'detail': detail}, status_code=status, headers=PRIVATE_HEADERS)

    @router.post('/wix/handoff', include_in_schema=False)
    async def handoff(request: Request):
        supplied = request.headers.get('x-cyt-wix-key', '')
        if (request.headers.get('origin') or len(supplied) != 64 or
                not hmac.compare_digest(supplied.encode(), verifier.config.bridge_key.encode())):
            return failure(403, 'Wix handoff authorization failed.')
        runtime = getattr(request.app.state, 'durable_queue', None)
        if runtime is None or not runtime.ready:
            return failure(503, 'Customer reviews are unavailable. Please try again later.')
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 2048:
                    return failure(413, 'Invalid Wix handoff request.')
            body = HandoffRequest.model_validate(json.loads(raw))
            result = await run_in_threadpool(verifier.issue, body.purchaseId, body.memberId)
        except (ValidationError, ValueError, UnicodeError):
            return failure(400, 'Invalid Wix handoff request.')
        except InvalidPurchaseProof:
            return failure(403, 'This review purchase is not eligible for this member.')
        except Exception:
            return failure(503, 'Purchase verification is unavailable. Please try again later.')
        return JSONResponse(result, headers=PRIVATE_HEADERS)

    @router.get('/wix/connect', include_in_schema=False)
    async def connect():
        # This page has no customer data and grants no access. The real upload
        # page still requires the existing purchase dependency on every fetch.
        origin = escape(verifier.config.parent_origin, quote=True)
        return HTMLResponse('''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Check Your Tech review access</title></head><body>
<p id="wix-status" role="status">Connecting your verified review purchase…</p>
<label>Review purchase <select id="review-order" disabled></select></label>
<button id="review-reconnect" type="button" disabled>Reconnect to this purchase</button>
<iframe id="review-frame" title="Your secure quote review" style="width:100%;height:1100px;border:0"
 data-parent-origin="''' + origin + '''"></iframe>
<script src="/static/wix_connect.js" defer></script></body></html>''', headers={**PRIVATE_HEADERS,
            'Content-Security-Policy': "frame-ancestors " + verifier.config.parent_origin + " 'self'; base-uri 'self'; object-src 'none'"})

    application.include_router(router)
