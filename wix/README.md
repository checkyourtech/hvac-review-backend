# Wix Pricing Plans integration — prepared, not activated

This change adds a concrete purchase adapter and installable Velo templates.
It does not publish Wix, deploy Render, disable Maintenance Mode, configure real
credentials, or turn on the queue. `render.yaml` remains unchanged with the queue
disabled. No database migration or dependency change is required.

## Verified inputs and outstanding configuration

Owner-confirmed: Wix Pricing Plans; member accounts required; Tier 1 “In-Depth
Quote Review” $29.95; Tier 2 “Quote Comparison Review” $49.95; both one-time,
one-month plans. Tier 3 “Full Protection Review” is still Active but not authorized
for fulfillment. Site origin: `https://www.checkyourtech.info`. Backend origin:
`https://check-your-tech-closed.onrender.com`.

Missing: Tier 1/2 plan GUIDs, Wix site GUID, site-scoped API credentials, two
independent server secrets, existing Upload Quote page slug, and actual published
HTML-component messaging behavior. No placeholder ID can grant access.

## Trust boundary and purchase policy

1. Member signs into Wix and uses the existing checkout and pages.
2. `quoteAnalyzer.web.js` exposes only `Permissions.SiteMember` methods. It derives
   the member GUID from backend `currentMember.getMember()`, never from page
   arguments, email, cookies supplied by our own form, or a payment-success URL.
3. Wix calls `POST /wix/handoff` server to server, with its dedicated bridge key.
   Python independently retrieves the order through Wix Pricing Plans Get Order.
4. Only an explicitly mapped Tier 1/2 plan can grant one review submission. Tier 1
   permits one logical quote; Tier 2 permits up to three. Each can contain several
   pages/photos. Tier 3 and every unmapped plan fail closed, including Active/Paid
   orders with misleading plan names.
5. A usable order must be `ONLINE`, `ACTIVE`, `lastPaymentStatus=PAID`, within its
   documented start/end dates, with `pricing.singlePaymentForDuration` of one
   month. Recurring, offline, free, indefinite, canceled, paused, expired,
   pending, failed, refunded and unknown states do not grant access. A cancellation
   object or scheduled cancellation also blocks this one-time launch contract.
   Missing/malformed authoritative fields produce unavailable verification,
   not invented payment or expiration data. Plan names and price strings do not
   authorize tiers; discounts do not silently change the purchased tier.
6. Python returns a signed one-hour, site/member/order-bound bearer proof. This
   does not consume a review. A lost handoff response can safely be requested
   again. HMAC signing and the Wix bridge use separate keys.
7. The existing `#htmlReport` embeds `/wix/connect`. A nonce/request-bound handshake
   accepts only the configured parent origin and actual parent window. Credentials
   are passed in memory by `postMessage`, never in URLs, cookies, local/session
   storage, manifests, or SQLite. The outer component fetches `/upload-page` with
   the existing authorization headers and initializes the unchanged upload UI.
8. All subsequent upload, job, HTML and JSON access rechecks the authoritative
   order; worker revalidation uses server credentials without the browser proof.
   Existing SQLite holds, idempotency, limits, ownership, retry/recovery and report
   rendering remain the owners of their existing behavior.

HTTP lookups use a fixed Wix host/GUID path, site-scoped headers, no redirects or
automatic retries, phase timeouts, bounded response size, and eight concurrent
lookups maximum. Raw provider bodies/errors and credentials are never logged or
returned. No browser CORS exception is needed because uploads and polling run
inside the backend-origin frame.

## Configuration to supply later — do not activate yet

Set the following through approved secret/configuration management, not source
code, screenshots, shell history, chat, or this repository's `.env`:

| Render setting | Required value |
| --- | --- |
| `CYT_WIX_SITE_ID` | Wix site GUID, not account ID or website domain |
| `CYT_WIX_TIER1_PLAN_ID` | Exact In-Depth Quote Review plan GUID |
| `CYT_WIX_TIER2_PLAN_ID` | Exact Quote Comparison Review plan GUID; different from Tier 1 |
| `CYT_WIX_PARENT_ORIGIN` | `https://www.checkyourtech.info` (no trailing slash) |
| `CYT_WIX_API_KEY` | Server-only Wix API key limited to this site and Read Orders |
| `CYT_WIX_BRIDGE_KEY` | Independently generated 32 random bytes encoded as 64 lowercase hex characters; same value in Wix Secrets Manager |
| `CYT_WIX_SESSION_KEY` | Different 32 random bytes encoded as 64 lowercase hex characters; Render only |

Wix Secrets Manager needs `CYT_WIX_BRIDGE_KEY` only. Do not return it from a web
method. The template uses `wix-secrets-backend.v2` and elevates only secret access,
not the call that establishes the member's identity. Secrets must be generated
with a cryptographic random generator, not repeated characters used by offline
tests. The API key and both secrets must differ.

Keep `CYT_QUEUE_ENABLED=false` and Maintenance Mode enabled until a separate
approved activation. With the queue disabled, the factory does not load Wix
credentials, create a verifier, install handoff routes, or grant customer access.
Enabling the queue later requires complete configuration and existing validated
private storage; it cannot provision a missing database or fall back to old
non-durable submission paths.

### Where to obtain the IDs

Use Wix's authenticated **Pricing Plans → Plans V3 → Query Plans** API reference
“Try It Out” for the actual site, or an authorized read-only API client:
`POST https://www.wixapis.com/pricing-plans/v3/plans/query`, body
`{"query":{"cursorPaging":{"limit":100}}}`. This queries records; it does not
create an order or charge a payment. Match the name and configured price/duration
to each intended plan, then copy that record's `id`. Names are used only for this
operator identification step. Verify the IDs against the corresponding Wix
Dashboard → Pricing Plans entries; do not recreate plans to obtain new IDs.
If the dashboard exposes a checkout link containing a plan ID, verify it against
the API record rather than guessing which GUID in a dashboard URL is the plan.

The site's Wix dashboard URL contains its site GUID; confirm it in the Wix API
site selector. Generate the API key using Wix Account Settings → API Keys with
access restricted to that site. Use Read Orders for the runtime. Temporary
operator plan discovery may also require Read Pricing Plans; the runtime does
not query or edit plans. Do not share API-key values when reporting IDs.

## Wix code to install after review

These are replacement templates, not copies of uninspected editor source. Save
the existing Velo code for rollback before making an authorized editor change.

1. Install `quoteAnalyzer.web.js` as `backend/quoteAnalyzer.web.js`. Fill only the
   two plan-ID constants. Remove the old `Permissions.Anyone` `analyzeQuote`
   export and any older public web-module proxy for it; do not leave both paths.
2. Replace the existing Thank You page code with `ThankYou.page.js`. Set
   `UPLOAD_PAGE_PATH` to the existing Upload Quote page's relative slug from Wix
   Pages → Upload Quote → page URL settings. This page no longer chooses a tier
   by name or redirects directly to Render with `package=basic/standard`.
   Only an order selector appears in the Wix-to-Wix URL; the backend still verifies
   the purchase. If Wix's receipt provides an order ID under another query key,
   map that documented selector explicitly. Without a selector the latest
   member order is selected for convenience, and the upload component offers the
   other purchases. A pending newest order is not replaced by an older paid one.
3. Replace the Upload Quote page code with `UploadQuote.page.js`, retaining
   `#htmlReport`. Configure that element as an HTML Component / Embed a Site
   supporting `.src`, `.onMessage()` and `.postMessage()`, with sufficient mobile
   width/height. It hosts the existing backend upload page and its camera/gallery,
   preview, grouping, submission, status and report controls.
4. The old Wix `#uploadButton1.uploadFiles()` handler produces Wix-hosted file
   URLs, which cannot be passed as direct multipart files. The template collapses
   that obsolete control and replaces its handler with the already built backend
   upload controls in the same page's `#htmlReport`. It does not download Wix file
   URLs or proxy private documents through a new permissive URL-fetch endpoint.
   Remove the old button's connected event handler, hardcoded customer details,
   package arguments and HTML-return handler. No HVAC engine rewrite is involved.
5. Keep Upload Quote restricted to members. Add/retain navigation back to this
   existing page for saved reviews. Its in-frame order selector and reconnect
   button retrieve existing jobs even when the purchased review is consumed.
   Reconnect warns before clearing any unsubmitted selected files. Submitted
   jobs survive closing the page. Expired/unpaid purchases remain inaccessible.
6. Disable Tier 3 purchasing and remove all checkout/direct-link references in
   Wix before launch. Hiding a homepage card does not prevent purchase of an
   Active plan. Backend rejection protects fulfillment but cannot stop Wix itself
   charging for an unsupported tier.

Do not put the bridge key, API key, or session signing key into any page/HTML
component. Never forward frontend `memberId`, `owner`, `tier` or `paid` claims.
Keep the exact HTTPS parent-origin check; do not broaden it to `*`, `null`, or
all Wix domains to make Editor Preview work.

## Failure, refund and session boundaries

- Every authenticated request and worker claim reads current Wix order state;
  there is no cached “paid” authorization fallback when Wix is unavailable.
- The adapter rejects `REFUNDED` and cancellation states reported by Wix. It
  cannot infer an unreported payment-provider refund, partial refund or chargeback.
  Before paid launch, validate propagation for the site's actual payment provider.
  Until established, the operator must also cancel/revoke the Pricing Plans order
  for any refund/chargeback before allowing further fulfillment. Do not assume
  deleting a checkout link or issuing an out-of-band refund changes the order.
- One-hour browser proofs may be reused only for their bound order/member while
  current purchase checks pass. The Wix logout hook clears the visible frame;
  copied bearer proofs are not individually revoked by Wix logout and expire
  within an hour. Rotation of the Render signing key invalidates all proofs.
  Queued work does not depend on browser-session lifetime. No refresh token is
  stored: reconnect authenticates through Wix again.
- Current report access retains the existing backend rule: an order must still
  be eligible and unexpired even after its review allowance is used. The one-month
  purchase duration therefore limits online report access; report retention and
  browser PDF controls themselves have not changed. Communicate this to customers.
- HTTP 202 means accepted in durable storage, not a completed analysis. Existing
  stable job IDs, polling, duplicate-submission handling and uncertain-work fences
  are unchanged. A verification outage does not authorize paid work.
- No customer identity, full order, proof, private proposal or report belongs in
  integration logs. Existing private logging remains active in the factory.

## Validation and remaining launch gates

Offline tests exercise actual Python adapter routes with mocked Wix HTTP and
temporary private SQLite/filesystem storage, plus the actual Velo/browser source
under Node with mocked Wix/DOM APIs. They test ownership, malformed/unknown states,
Tier 3, cancellation/refund/expiry, token tampering, secret separation, bounded
network behavior, source/origin checks, sign-out/late-response isolation,
multi-page upload, 202/idempotency, consumed-purchase report reconnection and
worker revalidation. Existing regression suites remain required.

These are not live Wix Editor or browser-engine tests. No actual plan IDs, API-key
scope, payment-provider transitions, published iframe ancestry/origin, camera
permissions or cross-frame print dialog have been proven. Preview-only results
are insufficient for member/plan APIs. A separately authorized closed acceptance
run must confirm:

- real Tier 1/2 IDs and one-month online order response shape;
- wrong member/unpaid/refunded/expired/Tier 3 rejection;
- exact `https://www.checkyourtech.info` messaging origin and published iframe
  ancestry, same-origin embedded UI load, mobile selection and existing PDF print;
- two photos as one quote, Tier 2 up to three quote groups, 202 and close/reconnect;
- receipt selector, multiple purchases, re-login, session expiry and logout;
- HTTP/provider failure handling and worker purchase revalidation without any
  repeated entitlement consumption.

Maintenance Mode currently prevents customer and Wix bridge access to the Render
service, which is intentional. Do not bypass that gate for testing without a
separate approved controlled-access deployment plan.

## Official contracts checked

- [Pricing Plans Get Order, ownership, status and payment fields](https://dev.wix.com/docs/api-reference/business-solutions/pricing-plans/orders/order-management-service-get-order)
- [Site-scoped REST API-key headers](https://dev.wix.com/docs/develop-websites/articles/coding-with-velo/authorization/make-rest-api-calls-with-an-api-key)
- [Member-scoped order enumeration](https://dev.wix.com/docs/velo/apis/wix-pricing-plans-backend/orders/list-current-member-orders)
- [Backend current-member identity](https://dev.wix.com/docs/velo/apis/wix-members-backend/current-member/introduction)
- [Secrets V2 access](https://dev.wix.com/docs/velo/apis/wix-secrets-backend-v2/secrets/get-secret-value)
- [HTML component messaging and load handshake](https://dev.wix.com/docs/velo/api-reference/%24w/html-component/on-mouse-out)
- [Plans V3 Query Plans for operator ID discovery](https://dev.wix.com/docs/api-reference/business-solutions/pricing-plans/plans-v3/query-plans)

## Local validation record — 2026-11-25

- 41 new Python regressions pass, including mocked Wix-to-queue HTTP integration.
- 15 new JavaScript tests and all 11 existing upload UI tests pass (26 total).
- Relevant combined regression run: 311 passed, no failures/errors/skips.
- Full run 1: 1,461 executed, two failure records in the existing
  `test_original_document_formats_pass_durable_upload_and_existing_processor`
  test (PDF stale-attempt fence and resulting submission-count assertion).
- Full run 2: 1,461 executed, one failure in existing
  `test_temporary_server_verification_outage_pauses_then_recovers`
  (`processing_timeout` before external work). The document-format test passed.
- Each affected existing test passed three bounded isolated repeats. Each repeat
  batch also passed the 41 new Python tests: 44/44 in both batches. All new Wix
  tests passed in both full runs. Cause of the intermittent queue failures remains
  unproven; no queue code, assertion or timeout was changed to hide them.
- Python/JavaScript syntax and whitespace checks pass. The audited offline runner
  reported no network or protected-path attempts. Tests use temporary data only.

Overall full-suite validation is **not PASS**. Resolve the intermittent queue
validation failures before deployment acceptance. Node uses mocked Wix/DOM APIs;
real Wix publishing, browser integration, credentials and payment-state behavior
remain unverified. No activation is authorized by this validation record.
