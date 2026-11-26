// Replace the existing Thank You page code; keep its existing checkout/page.
import wixLocationFrontend from 'wix-location-frontend';
import { listReviewPurchases } from 'backend/quoteAnalyzer.web';

// REQUIRED: existing Upload Quote page's relative URL slug, e.g. /your-slug.
const UPLOAD_PAGE_PATH = '';

$w.onReady(async () => {
  if (!/^\/[a-z0-9-]+$/.test(UPLOAD_PAGE_PATH)) return; // closed until configured
  try {
    const purchases = await listReviewPurchases();
    const requested = wixLocationFrontend.query.orderId;
    // An explicit checkout selector must match this member's returned list.
    // Do not silently fall back to a different purchase if it does not match.
    // Preserve a mismatched selector so Upload Quote explicitly rejects it;
    // dropping it would inadvertently fall back to a different latest order.
    const selector = requested || purchases[0]?.purchaseId;
    const query = selector ? '?orderId=' + encodeURIComponent(selector) : '';
    // Order ID is only a selector. No bearer, package, email or payment proof in URL.
    wixLocationFrontend.to(UPLOAD_PAGE_PATH + query);
  } catch (_) {
    // Upload page displays the sign-in/verification failure and reconnect path.
    wixLocationFrontend.to(UPLOAD_PAGE_PATH);
  }
});
