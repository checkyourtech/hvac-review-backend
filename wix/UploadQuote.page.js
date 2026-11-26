// Replace existing Upload Quote page code. Reuse #htmlReport and the existing
// backend's file/photo controls. Do not call uploadFiles() or analyzeQuote().
import wixLocationFrontend from 'wix-location-frontend';
import { authentication } from 'wix-members-frontend';
import { listReviewPurchases, openReview } from 'backend/quoteAnalyzer.web';

$w.onReady(() => {
  const frame = $w('#htmlReport');
  let nonce = null, selection = 0, loaded = null, signedOut = false;
  // The Wix UploadButton returns remote URLs, not File bytes. Its obsolete
  // handler is replaced by the already accepted backend document/photo UI.
  $w('#uploadButton1').collapse();
  frame.onMessage(async event => {
    const message = event.data;
    if (signedOut || !message || typeof message.nonce !== 'string' || message.nonce.length > 100) return;
    if (message.type === 'cyt-ready') {
      nonce = message.nonce;
      loaded = loaded || listReviewPurchases();
      try {
        const purchases = await loaded;
        if (signedOut || nonce !== message.nonce) return;
        frame.postMessage({type: 'cyt-orders', nonce, purchases,
          selected: wixLocationFrontend.query.orderId || ''});
      } catch (_) {
        frame.postMessage({type: 'cyt-error', nonce, message: 'Sign in again or contact support to check your review purchases.'});
      }
    } else if (message.type === 'cyt-select' && message.nonce === nonce &&
               typeof message.requestId === 'string' && message.requestId.length <= 100) {
      const current = ++selection;
      try {
        const purchases = await loaded;
        if (!purchases?.some(item => item.purchaseId === message.purchaseId)) return;
        const credentials = await openReview(message.purchaseId);
        if (signedOut || current !== selection || nonce !== message.nonce) return;
        frame.postMessage({type: 'cyt-credentials', nonce, requestId: message.requestId, ...credentials});
      } catch (_) {
        if (current === selection && !signedOut) frame.postMessage({type: 'cyt-error', nonce,
          requestId: message.requestId, message: 'Purchase verification is unavailable or this order is not eligible. Reconnect or contact support.'});
      }
    }
  });
  authentication.onLogout(() => {
    signedOut = true; selection++;
    frame.postMessage({type: 'cyt-clear', nonce});
    frame.src = 'https://check-your-tech-closed.onrender.com/wix/connect';
  });
  // Must be an Embed a Site / HTML Component that supports src and messaging.
  frame.src = 'https://check-your-tech-closed.onrender.com/wix/connect';
});
