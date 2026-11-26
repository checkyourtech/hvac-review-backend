/* Exact-parent, nonce-bound bridge. Credentials live only in memory. The
 * unchanged upload UI handles direct files, 202, polling and saved reports. */
(() => {
  'use strict';
  const frame = document.getElementById('review-frame');
  const status = document.getElementById('wix-status');
  const select = document.getElementById('review-order');
  const reconnect = document.getElementById('review-reconnect');
  const origin = frame.dataset.parentOrigin;
  const nonce = crypto.randomUUID();
  let listed = false, requestId = null, selected = null, received = false, controller = null;
  function choose() {
    if (frame.srcdoc && !window.confirm('Reconnect? Submitted reviews are saved, but unsubmitted selected files will be cleared.')) {
      select.value = selected; return;
    }
    if (controller) controller.abort();
    frame.onload = null; frame.srcdoc = '';
    selected = select.value; requestId = crypto.randomUUID(); received = false;
    status.textContent = 'Checking your purchase…';
    window.parent.postMessage({type: 'cyt-select', nonce, requestId, purchaseId: selected}, origin);
  }
  select.addEventListener('change', choose);
  reconnect.addEventListener('click', choose);
  window.addEventListener('message', async event => {
    const data = event.data;
    if (event.source !== window.parent || event.origin !== origin || !data || data.nonce !== nonce) return;
    if (data.type === 'cyt-clear') {
      listed = received = true; requestId = null;
      clearInterval(readyTimer);
      if (controller) controller.abort();
      frame.onload = null; frame.srcdoc = '';
      select.disabled = reconnect.disabled = true;
      status.textContent = 'Signed out. Sign in again to check your saved reviews.';
      return;
    }
    if (data.type === 'cyt-orders' && !listed && Array.isArray(data.purchases)) {
      listed = true; clearInterval(readyTimer);
      for (const purchase of data.purchases.slice(0, 1000)) {
        if (!purchase || typeof purchase.purchaseId !== 'string' || !['tier1', 'tier2'].includes(purchase.tier)) continue;
        const option = document.createElement('option');
        option.value = purchase.purchaseId;
        option.textContent = `${purchase.tier === 'tier1' ? 'In-Depth Quote Review' : 'Quote Comparison Review'} — ${String(purchase.createdDate)} — ${String(purchase.status)}`;
        select.append(option);
      }
      if (!select.options.length) { status.textContent = 'No launch review purchases were found. Sign in to the purchasing account or contact support.'; return; }
      select.disabled = reconnect.disabled = false;
      if (data.selected) {
        if (!Array.from(select.options).some(option => option.value === data.selected)) {
          status.textContent = 'That purchase is not in this account. Choose your purchase to continue.';
          return;
        }
        select.value = data.selected;
      }
      choose(); return;
    }
    if (data.type === 'cyt-error' && (!data.requestId || data.requestId === requestId)) {
      status.textContent = 'Purchase verification is unavailable or this order is not eligible. Reconnect, sign in again, or contact support.';
      return;
    }
    if (received || !requestId || data.type !== 'cyt-credentials' || data.requestId !== requestId ||
        data.purchaseId !== selected ||
        typeof data.authorization !== 'string' || !data.authorization.startsWith('Bearer ') ||
        data.authorization.length > 2055 || typeof data.purchaseId !== 'string') return;
    received = true;
    clearInterval(readyTimer);
    controller = new AbortController();
    const thisController = controller, current = requestId;
    const timeout = setTimeout(() => thisController.abort(), 15000);
    try {
      const response = await fetch('/upload-page', {headers: {
        Authorization: data.authorization, 'X-Purchase-ID': data.purchaseId},
        cache: 'no-store', credentials: 'omit', redirect: 'error', signal: thisController.signal});
      if (!response.ok) throw new Error('unavailable');
      const html = await response.text();
      if (current !== requestId) return;
      frame.onload = () => {
        if (current !== requestId) return;
        if (typeof frame.contentWindow.configureQuoteUpload !== 'function') {
          status.textContent = 'Review access could not be opened. Reconnect from the Wix page.';
          return;
        }
        frame.onload = null;
        frame.contentWindow.configureQuoteUpload({authorization: data.authorization, purchaseId: data.purchaseId});
        status.textContent = 'Connected. Return to this Wix page to reconnect with saved reviews. Access expires after one hour.';
      };
      frame.srcdoc = html;
    } catch (_) {
      if (current === requestId) status.textContent = 'Review access is unavailable or expired. Reconnect from the Wix page; do not repeat a paid submission.';
    } finally { clearTimeout(timeout); }
  });
  let announcements = 0;
  function ready() {
    if (listed || announcements++ >= 15) { clearInterval(readyTimer); return; }
    window.parent.postMessage({type: 'cyt-ready', nonce}, origin);
  }
  const readyTimer = setInterval(ready, 1000);
  ready();
})();
