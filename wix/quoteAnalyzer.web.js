// Install as backend/quoteAnalyzer.web.js. No public analyzeQuote proxy remains.
import { Permissions, webMethod } from 'wix-web-module';
import { currentMember } from 'wix-members-backend';
import { orders } from 'wix-pricing-plans-backend';
import { secrets } from 'wix-secrets-backend.v2';
import { elevate } from 'wix-auth';
import { fetch } from 'wix-fetch';

const BACKEND = 'https://check-your-tech-closed.onrender.com';
// REQUIRED: exact plan GUIDs from Wix, also configured on Render. Never names.
const TIER1_PLAN_ID = '';
const TIER2_PLAN_ID = '';
const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const getBridgeSecret = elevate(secrets.getSecretValue);

function configured() {
  if (!GUID.test(TIER1_PLAN_ID) || !GUID.test(TIER2_PLAN_ID) || TIER1_PLAN_ID === TIER2_PLAN_ID) {
    throw new Error('Review purchase integration is not configured.');
  }
}

async function memberId() {
  const member = await currentMember.getMember(); // never elevate member identity
  if (!member || !GUID.test(member._id)) throw new Error('Please sign in to your Wix account.');
  return member._id;
}

export const listReviewPurchases = webMethod(Permissions.SiteMember, async () => {
  configured();
  try {
    const member = await memberId();
    const result = [];
    // Bound enumeration; pagination keeps reconnect access to older purchases.
    for (let skip = 0; skip < 1000; skip += 100) {
      const page = await orders.listCurrentMemberOrders({},
        {fieldName: ['createdDate'], order: ['DESC']}, {limit: 100, skip});
      if (!Array.isArray(page)) throw new Error('Invalid orders');
      for (const order of page) {
        const tier = order.planId === TIER1_PLAN_ID ? 'tier1' : order.planId === TIER2_PLAN_ID ? 'tier2' : null;
        if (!tier || order.buyer?.memberId !== member || !GUID.test(order._id)) continue;
        // This is a selector, NOT authorization. Include pending/failed orders
        // so a new unpaid checkout never silently selects an older paid order.
        result.push({purchaseId: order._id, tier,
          createdDate: new Date(order._createdDate || order.createdDate).toISOString(),
          status: String(order.status || 'UNKNOWN')});
      }
      if (page.length < 100) return result;
    }
    throw new Error('Order limit exceeded');
  } catch (_) {
    throw new Error('Review purchases could not be checked. Please sign in again or contact support.');
  }
});

export const openReview = webMethod(Permissions.SiteMember, async purchaseId => {
  configured();
  if (typeof purchaseId !== 'string' || !GUID.test(purchaseId)) throw new Error('Choose a review purchase.');
  let timeout;
  try {
    const authenticatedMember = await memberId();
    const secret = await getBridgeSecret('CYT_WIX_BRIDGE_KEY');
    if (!secret || !/^[0-9a-f]{64}$/.test(secret.value)) throw new Error('Bridge not configured');
    // Only this backend adds the authenticated member. The caller supplies just
    // an untrusted order selector. No customer email, tier, or uploaded files.
    const request = async () => {
      const response = await fetch(BACKEND + '/wix/handoff', {
        method: 'post', headers: {'Content-Type': 'application/json', 'X-CYT-Wix-Key': secret.value},
        body: JSON.stringify({purchaseId, memberId: authenticatedMember})
      });
      if (!response.ok) throw new Error('Purchase unavailable');
      const proof = await response.json();
      if (proof.purchaseId !== purchaseId || typeof proof.authorization !== 'string' ||
          !proof.authorization.startsWith('Bearer ') || proof.authorization.length > 2055 ||
          !Number.isInteger(proof.expiresAt)) throw new Error('Invalid handoff');
      return {purchaseId, authorization: proof.authorization, expiresAt: proof.expiresAt};
    };
    // No automatic retry. The Python lookup is bounded too; a lost handoff
    // response consumes no review and can safely be requested again.
    return await Promise.race([request(), new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error('Handoff timed out')), 20000);
    })]);
  } catch (_) {
    // Never forward Wix/API response bodies, secrets, or customer identifiers.
    throw new Error('This purchase could not be verified. Confirm payment and sign in again, or contact support.');
  } finally { clearTimeout(timeout); }
});
