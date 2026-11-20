# Customer report delivery and recovery

Work Package 4 adds durable report recovery around the accepted analysis and document intake. Both public submission routes use the same job lifecycle. The current adapter is tested offline on a single host with an explicitly selected SQLite database. Production hosting, persistent storage and worker supervision have not been approved. Purchase verification remains closed until a trusted verifier is installed; no Wix integration or customer email delivery is implied.

## Storage and execution

`SQLiteReportStore` uses the database already selected for `SQLitePurchaseLedger`. It stores job and authenticated owner identifiers, purchase and tier, hashed request identity, attempt counters, safe failure categories, finalized analysis JSON, the exact rendered HTML, customer name for the internal notification, and logical quote count. Raw documents, original extracted proposal text, authorization tokens and contact email addresses are not copied into the job table. Reports can themselves contain source excerpts and personal information and must be treated as private customer data.

Job states are `pending`, `processing`, `completed`, and `failed`. A job becomes completed only when the finalized JSON and complete HTML have been committed in the same transaction. Browser Print / Save as PDF remains part of the saved HTML; there is no server-generated PDF artifact or changed PDF layout.

At most two submissions execute at once per process. Inputs remain in bounded process memory, not a durable queue. A request waits up to 20 seconds, then returns the saved status. A disconnected browser does not cancel the independent worker. A restarted application can retrieve completed reports from the same database. A pending job can be submitted again with the original documents. Processing records have a 15-minute deadline; status checks reconcile expired records to a non-retryable interrupted failure. Late workers cannot reserve a review or publish over that failure. A deployment supervisor must terminate unhealthy workers; the local adapter does not forcibly cancel a running provider call or provide distributed worker capacity limits.

This does not guarantee exactly-once external API execution. A process can die after a provider receives a request. Interrupted attempts are never automatically replayed. Provider retries already present in the analysis SDK and the accepted single length-exhaustion recovery remain in effect; no additional automatic job retry loop was added.

## Verified access

Every endpoint requires both headers:

- `Authorization: Bearer <authenticated opaque proof>`
- `X-Purchase-ID: <purchase selector>`

The trusted verifier must authenticate the customer and verify current payment eligibility, purchase owner, exact Tier 1 or Tier 2 product mapping, quantity and expiry. Customer names and email fields do not establish ownership. Every retrieval and retry rechecks the verifier and the ledger, including cancellation and expiry. Consumed quantity does not block report access; expired or revoked eligibility does. The production purchase adapter must define an appropriate report access period and support recovery after that period. Tier 3 remains blocked before entitlement registration or job access.

No credential belongs in a URL, hidden input, browser storage, email link, report file name or log. Job IDs and idempotency keys are identifiers, not access credentials. All successful status/report responses use `Cache-Control: no-store, private`. Reports are served only by the authenticated API, never by `/static` or an uploads route.

## Submission contract for Work Package 6

Use the existing `POST /upload` multipart format or `POST /analyze` JSON format. Keep all existing field validation and quote grouping. Tier 1 allows one logical proposal with multiple pages; Tier 2 allows up to three logical proposals. The JSON route retains the accepted `AnalyzeRequest` fields. The multipart route retains `files`, `customer_name`, `customer_email`, and the ordered `quote_groups` page manifest with file indexes and rotations.

Generate a random `Idempotency-Key` before submission. It must contain 16 to 128 ASCII letters, digits, underscores or hyphens. Reuse the same key and unchanged submission when reconnecting or retrying. A changed payload with the same key returns 409. A new key with identical content under the same purchase finds the existing job. Upload identity includes ordered form fields, file names, MIME types and file content hashes, excluding the random multipart boundary. Preserve those fields and grouping on retry. The fingerprint is deliberately exact, not a semantic comparison of different scans or paraphrased proposals. JSON and multipart submissions are distinct representations; do not switch transports on retry.

Use a new key for corrected content. A new job still needs an unused purchased review. A failed review that already reserved its entitlement cannot be changed into a new scope or new documents without support reconciliation. An intake failure before reservation leaves that review unused.

Legacy callers without an idempotency key still receive stored reports, but an exhausted entitlement can reject a later POST before duplicate lookup. They can recover using `GET /jobs`. The Wix launch transport must always send a stable key.

Responses:

| Response | Frontend action |
| --- | --- |
| 200 from `/upload` | Display the saved HTML, preserving its existing print controls. |
| 200 from `/analyze` | Read the finalized analysis JSON. Retrieve HTML through the returned job ID when needed. |
| 202 | Read the job status JSON. Poll its status URL, approximately every three seconds, while pending or processing. Do not render status JSON as a report. |
| 503 with a pending job and `Retry-After` | Local worker slots are busy. Resubmit the same key and unchanged content after the delay. No work is queued in the process. |
| 400, 413, 415 or 422 | Show the safe field/page guidance in `detail`; preserve selected pages for correction. No report is complete. |
| Failed-job response | Show the safe error. Query status before offering any explicit retry. Never automatically replay paid analysis. |
| 401 or 403 | Reauthenticate or resolve purchase eligibility. Never fall back to a client-claimed tier. |
| 409 | Check purchase, job state, key/content mismatch or exhausted retry count. Do not create another charge. |

Once a job exists, responses include `X-Review-Job-ID`. Normal job responses also include `Location: /jobs/<job_id>`; clients can always derive this path from the ID. A pre-job validation or storage failure may not have an ID. A lost HTTP response is recovered using the authenticated job list, not by purchasing again.

## Recovery endpoints

| Method and path | Result |
| --- | --- |
| `GET /jobs` | Up to 20 jobs for the selected verified purchase and authenticated owner, newest first. |
| `GET /jobs/{job_id}` | State, attempts, reservation status, safe error, retry availability, notification status and authorized report/result paths. |
| `GET /jobs/{job_id}/report` | Exact stored HTML when completed; otherwise 409. |
| `GET /jobs/{job_id}/result` | Exact stored finalized JSON when completed; otherwise 409. |
| `POST /jobs/{job_id}/delivery-retry` | Bounded internal business-notification retry; no analysis and no entitlement consumption. |

An unknown job or a job belonging to a different owner or purchase returns 404 after authentication. Status and lists contain no proposal text or customer identifiers. Store only a job identifier if the frontend needs a recovery reference. After browser closure, authenticate again and fetch `/jobs`; neither the original open tab nor a saved bearer token is necessary.

The local upload form now sends a stable key, handles 202 status polling and offers authenticated recovery of completed reports. It still requires the real Wix handoff via `window.configureQuoteUpload({authorization, purchaseId})`. It is a local integration aid, not a complete Wix account dashboard. Wix must wire receipt/account navigation back to authenticated recovery, show failed-state guidance and offer explicit retries. No live Wix changes were made.

## Failed analysis and entitlement handling

Validation and extraction precede entitlement reservation. The reservation and its job link commit atomically immediately before paid HVAC analysis. Image transcription still uses the accepted purchase authorization, resource limits and image cache; a failed image extraction does not reserve an HVAC review.

After reservation, a failure retains the reservation. It never automatically refunds, replenishes, charges or consumes a second review. When status has `retry_allowed: true`, the customer can explicitly resubmit the same endpoint, unchanged source, original `Idempotency-Key` and `X-Review-Retry: true`. There are at most two processing attempts per job, including the first. A successful retry uses the original reservation. A duplicate ordinary submission only returns the existing job. Retries still need current valid purchase authorization.

Timeouts, response-length exhaustion and normal returned processing failures can be retried within that bound. An interrupted/expired worker or an uncertain report storage failure requires support reconciliation and has no automatic retry. Support must determine whether processing is still active before authorizing another execution. No public entitlement-reset or quota-replenishment endpoint was added. Corrected inputs before reservation can create a new job; bounded job/key limits prevent unlimited durable records under one purchase.

## Notification handling

Email remains an internal notification to the existing business mailbox. It does not deliver to an unverified customer-supplied email address. Customer access is through authenticated report retrieval. The saved HTML is sent without rebuilding the report on notification retry.

Notification states are `pending`, `sending`, `sent`, `failed`, `unconfigured` and `unknown`. Missing email configuration is reported as unconfigured, not sent. SMTP has a 20-second socket timeout. Failure does not change completed job state. The initial send plus retries is limited to three attempts per job, and concurrent retries are serialized by the database. A successful notification is not resent. A send left unresolved past its deadline becomes unknown; an explicit retry can duplicate an email already accepted by the mail server. This ambiguity does not trigger analysis or consume another review.

Errors never persist provider exception text, document contents, mail credentials or recipient details. Notification-state storage failure leaves the completed report recoverable with pending/sending/unknown notification state. Requesting notification retry is not a promise of delivery to a customer inbox.

## Production dependencies and remaining launch blockers

Before paid production operation, approve the host and storage topology. The current adapter requires one explicit persistent database file shared by the entitlement and report stores, transactional locking, restricted filesystem access, backup/restore procedures and process supervision. Ephemeral/container-local disks, network filesystem assumptions and independent per-worker databases are not approved. Multi-host or autoscaled deployment needs an equivalent shared transactional adapter and supervised job execution before launch. No such environment has been deployed or verified.

Wix purchase verification, authoritative product mapping and authenticated customer proof delivery are still required. The frontend must implement the contract above, including tier-specific grouping, failed-state recovery and stable idempotency. Its origin/CORS and credential transport configuration require controlled acceptance testing. The live website has not been inspected or changed in this work package.

Work Package 5 must decide and enforce report retention/deletion periods, purchase access expiry, storage encryption and permissions, backups and deletion from backups, operational logging and monitoring, rate limits, and support procedures for interrupted jobs. Finalized reports and notification names are sensitive even though raw uploaded documents are not persisted. This package does not implement those policies.

Before launch, use a controlled environment to verify authorized purchase-to-report delivery, reconnect/restart behavior and actual business email delivery. Offline tests use substituted analysis, payment verification and notification providers and do not establish production availability. No live API, payment or email call is needed to run the regression suite.
