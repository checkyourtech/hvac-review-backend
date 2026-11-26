"""Wix Pricing Plans authority and order-bound, short-lived browser proofs.

No network on import/startup, no dotenv, no customer proof persistence. Only
explicit Tier 1/2 plan IDs can grant ONE review (not one review per quote).
Wix backend authenticates the current member; Python independently reads the
order on every access and worker claim. Nothing trusts plan names or emails.
"""
import base64
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import httpx
from starlette.concurrency import run_in_threadpool

from purchase_security import InvalidPurchaseProof, VerifiedPurchase
from queue_runtime import RevalidatedPurchase

GUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SESSION_SECONDS = 3600
ORDER_URL = 'https://www.wixapis.com/pricing-plans/v2/orders/'


class WixUnavailable(Exception):
    """Sanitized provider/configuration error. Never include provider bodies."""


def identifier(value):
    if not isinstance(value, str) or not GUID.fullmatch(value):
        raise InvalidPurchaseProof()
    return value


@dataclass(frozen=True)
class WixConfig:
    site_id: str
    tier1_plan_id: str
    tier2_plan_id: str
    parent_origin: str
    api_key: str = field(repr=False)
    bridge_key: str = field(repr=False)
    session_key: str = field(repr=False)

    def __post_init__(self):
        try:
            for value in (self.site_id, self.tier1_plan_id, self.tier2_plan_id):
                identifier(value)
            if self.tier1_plan_id == self.tier2_plan_id:
                raise ValueError()
            origin = urlsplit(self.parent_origin)
            if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                    or origin.port not in (None, 443) or origin.path or origin.query or origin.fragment
                    or not re.fullmatch(r'https://[a-z0-9.-]+(?::443)?', self.parent_origin)):
                raise ValueError()
            if not self.api_key or not self.api_key.isascii() or any(c.isspace() for c in self.api_key):
                raise ValueError()
            # Two separately generated 256-bit secrets. Never use a Wix API key
            # as a browser-token signing key or share that signing key with Wix.
            for key in (self.bridge_key, self.session_key):
                if not re.fullmatch(r'[0-9a-f]{64}', key):
                    raise ValueError()
            if len({self.api_key, self.bridge_key, self.session_key}) != 3:
                raise ValueError()
        except (ValueError, TypeError, InvalidPurchaseProof):
            raise WixUnavailable('Wix purchase configuration is incomplete or invalid.') from None

    @classmethod
    def from_env(cls):
        # Called only by the explicitly enabled factory, never while closed.
        return cls(*(os.environ.get(name, '') for name in (
            'CYT_WIX_SITE_ID', 'CYT_WIX_TIER1_PLAN_ID', 'CYT_WIX_TIER2_PLAN_ID',
            'CYT_WIX_PARENT_ORIGIN', 'CYT_WIX_API_KEY', 'CYT_WIX_BRIDGE_KEY', 'CYT_WIX_SESSION_KEY')))

    @property
    def plans(self):
        return {self.tier1_plan_id: 'tier1', self.tier2_plan_id: 'tier2'}


def timestamp(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        result = int(parsed.timestamp())
        if result <= 0:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise WixUnavailable('Wix order dates could not be verified.') from None


class WixOrdersClient:
    """Bounded, uncached, read-only Wix REST lookups; no retry or redirects.

    A fresh client per lookup avoids sharing connections across queue threads.
    HTTPX phase timeouts plus response/deadline bounds protect worker resources.
    """
    def __init__(self, config, *, transport=None):
        self.config, self.transport = config, transport
        self.slots = threading.BoundedSemaphore(8)

    def get_order(self, purchase_id):
        identifier(purchase_id)  # fixed host + GUID path; no caller-selected URL
        if not self.slots.acquire(blocking=False):
            raise WixUnavailable('Wix purchase verification is busy.')
        try:
            deadline = time.monotonic() + 15
            with httpx.Client(timeout=httpx.Timeout(8, connect=3, write=3, pool=1),
                    follow_redirects=False, trust_env=False, transport=self.transport) as client:
                with client.stream('GET', ORDER_URL + purchase_id, params={'fieldSet': 'BASIC'},
                        headers={'Authorization': self.config.api_key, 'wix-site-id': self.config.site_id,
                                 'Accept': 'application/json', 'Accept-Encoding': 'identity'}) as response:
                    if response.status_code == 404:
                        raise InvalidPurchaseProof()
                    if response.status_code != 200:
                        raise WixUnavailable('Wix purchase verification is unavailable.')
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536 or time.monotonic() > deadline:
                            raise WixUnavailable('Wix order response exceeded its limits.')
                    data = json.loads(body)
                    if not isinstance(data, dict) or not isinstance(data.get('order'), dict):
                        raise WixUnavailable('Wix order response could not be verified.')
                    return data['order']
        except (InvalidPurchaseProof, WixUnavailable):
            raise
        except Exception:
            raise WixUnavailable('Wix purchase verification is unavailable.') from None
        finally:
            self.slots.release()


class WixPurchaseVerifier:
    def __init__(self, config, *, client=None):
        self.config = config
        self.client = client or WixOrdersClient(config)

    def revalidate(self, purchase_id):
        order = self.client.get_order(identifier(purchase_id))
        try:
            if order['id'] != purchase_id:
                raise InvalidPurchaseProof()
            owner = identifier(order['buyer']['memberId'])
            tier = self.config.plans.get(order['planId'])
            if tier is None:  # Includes Tier 3, even when renamed or ACTIVE/PAID.
                raise InvalidPurchaseProof()
            starts, expires = timestamp(order['startDate']), timestamp(order['endDate'])
            if expires <= starts:
                raise WixUnavailable('Wix order validity could not be verified.')
            pricing = order['pricing']
            duration = pricing.get('singlePaymentForDuration')
            # These are the owner's confirmed launch plans. Recurring/free/
            # indefinite orders need a separately reviewed entitlement policy.
            eligible = (order['type'] == 'ONLINE' and order['status'] == 'ACTIVE'
                and order['lastPaymentStatus'] == 'PAID' and starts <= time.time() < expires
                and duration == {'count': 1, 'unit': 'MONTH'}
                and 'subscription' not in pricing and 'singlePaymentUnlimited' not in pricing
                and not order.get('cancellation') and not order.get('autoRenewCanceled'))
            return RevalidatedPurchase(purchase_id=purchase_id, owner_customer_id=owner,
                tier=tier, payment_eligible=eligible, expires_at=expires, submission_limit=1)
        except (KeyError, TypeError, AttributeError):
            raise WixUnavailable('Wix order response could not be verified.') from None

    def issue(self, purchase_id, member_id):
        """Only the server-authenticated Velo bridge may call this operation."""
        member_id = identifier(member_id)
        purchase = self.revalidate(purchase_id)
        if purchase.owner_customer_id != member_id or not purchase.payment_eligible:
            raise InvalidPurchaseProof()
        now = int(time.time())
        claims = dict(v=1, sub=member_id, order=purchase_id, site=self.config.site_id,
            aud='cyt-review', iat=now, exp=min(now + SESSION_SECONDS, purchase.expires_at),
            nonce=secrets.token_hex(16))
        payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(',', ':')).encode()).rstrip(b'=')
        signature = hmac.new(bytes.fromhex(self.config.session_key), b'cyt-wix-v1.' + payload, hashlib.sha256).hexdigest()
        return dict(authorization='Bearer ' + payload.decode() + '.' + signature,
                    purchaseId=purchase_id, expiresAt=claims['exp'])

    def _member(self, purchase_id, token):
        try:
            identifier(purchase_id)
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError()
            payload, signature = token.split('.')
            if not re.fullmatch(r'[A-Za-z0-9_-]+', payload) or not re.fullmatch(r'[0-9a-f]{64}', signature):
                raise ValueError()
            expected = hmac.new(bytes.fromhex(self.config.session_key),
                b'cyt-wix-v1.' + payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            now = time.time()
            if (claims['v'] != 1 or claims['site'] != self.config.site_id or claims['aud'] != 'cyt-review'
                    or claims['order'] != purchase_id or type(claims['iat']) is not int
                    or type(claims['exp']) is not int or not claims['iat'] <= now < claims['exp']
                    or not 0 < claims['exp'] - claims['iat'] <= SESSION_SECONDS):
                raise ValueError()
            return identifier(claims['sub'])
        except Exception:
            raise InvalidPurchaseProof() from None

    async def verify(self, purchase_id, bearer_token):
        member = self._member(purchase_id, bearer_token)
        purchase = await run_in_threadpool(self.revalidate, purchase_id)
        if member != purchase.owner_customer_id:
            raise InvalidPurchaseProof()
        return VerifiedPurchase(**purchase.model_dump(), authenticated_customer_id=member)
