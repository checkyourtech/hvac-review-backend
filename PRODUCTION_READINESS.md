# Check Your Tech production readiness

Work Package 5 reviewed `structured-verdict-refactor` at accepted commit `2fbc9cb`.
The owner confirmed on November 19, 2026 that production hosting and storage are
not configured. Paid production operation is **BLOCKED**. The backend remains
closed without a trusted purchase verifier and explicitly configured storage.
No host, deployment, Wix integration, live transaction or customer data migration
is part of this work. Work Package 6 has not started.

PASS means implemented and verified offline. BLOCKED means a necessary dependency
or owner decision is missing. UNVERIFIED means there is insufficient operational
evidence; a passing local health probe does not remove these limitations.

## Hosting and deployment decisions

| Area | Status | Required decision or validation |
| --- | --- | --- |
| Hosting provider and deployment | BLOCKED | Owner must select/approve a host; no deployed backend is established. |
| Python runtime and reproducible build | BLOCKED | Choose a maintained runtime, lock dependencies, and run the full suite in a clean build. Local validation uses Python 3.9.6; requirements are currently unpinned and a legacy `source/` dependency tree is tracked. Do not ship a local virtual environment. |
| Worker count and supervision | BLOCKED | Approve topology, process restart policy, graceful termination and memory/CPU limits. SQLite and process-local limits are not an approved multi-host design. |
| Durable private volume | BLOCKED | Approve an absolute persistent path, dedicated service account, directory mode 0700, encrypted volume, sufficient disk capacity and backup destination. Do not use ephemeral container storage or assume network filesystems provide SQLite locking guarantees. |
| HTTPS and proxy | BLOCKED | HTTPS is mandatory for bearer proofs and reports. Approve TLS termination, redirect/HSTS policy, trusted proxy addresses, host allowlist, ingress body limits and rate limits. Do not expose the origin server directly or trust arbitrary forwarded headers. |
| Purchase identity and product mapping | BLOCKED | Trusted verification and authenticated customer handoff remain unconfigured. Tier 3 stays disabled. |
| Actual provider and email operation | UNVERIFIED | Offline mocks do not prove OpenAI access, spending limits or SMTP delivery. Email goes to the business inbox, not the customer. Customer report access uses authenticated job routes. |

For the existing SQLite adapter, a single supervised application process on one
host with one private persistent local volume is the smallest topology to
evaluate. This is a proposal, not an approved deployment. Multiple hosts require
shared transactional storage and coordinated worker/rate-limit behavior that are
not implemented here. Same-file concurrent quota transactions are tested; this
does not establish deployment-wide throughput or availability.

## Storage provisioning and recovery

**PASS:** purchase registration, quota reservation and job association are
transactional. Every request opens its own connection with foreign keys enabled;
`BEGIN IMMEDIATE` serializes reservations. Completed report JSON and HTML are
stored together before business notification. Ownership is checked on every
retrieval and notification retry. Tests preserve these guarantees.

New databases now require the explicit administrative
`SQLitePurchaseLedger.initialize(absolute_private_path)` call. It creates a new
file exclusively with mode 0600 and refuses to overwrite an existing file.
Provision the report tables with `SQLiteReportStore(ledger)` at the same time.
Neither operation is a public endpoint. No real database was provisioned here.

Normal startup must use `SQLitePurchaseLedger(absolute_private_path)`, then the
same ledger for `SQLiteReportStore` and `EntitlementService`. Startup must never
call `initialize` in response to a missing volume or an error. Opens use SQLite
`mode=rw`; missing, empty, wrong-schema, symlinked or non-private ledgers fail
closed. Paths under the application's public `static` directory are rejected.
Existing files are not repaired, deleted or chmodded automatically. A filesystem
administrator must approve any permission correction. Keep all data outside the
web root and use a service-owned parent directory to prevent path substitution.

**PASS locally:** completed reports survive reopened application/store instances.
After interruption, processing records expire after the existing 900-second
deadline; status access marks them failed and fences late completion. Pending
inputs are not durable: the customer must resubmit the original documents/key.
There is no automatic paid replay and no durable worker queue. Monitoring and
supervised restart on the actual host are **BLOCKED**. Expiry is checked on store
access, not by a background recovery service.

## Backups and support reconciliation

The synthetic restore regression uses SQLite's backup API and `PRAGMA
integrity_check`, reopens the copied database, retrieves the exact saved report,
preserves consumed quota and notification state, and denies another customer.
This is **PASS** for the local adapter. Production backups and disaster recovery
are **UNVERIFIED** until exercised on the selected host.

Before opening purchases, approve an initial recovery-point target of at most
one hour and recovery-time target of four hours, or explicitly choose different
targets. Schedule encrypted backups to a separate restricted destination and
alert on age, failure and free-space thresholds. Back up the entire shared
purchase/report database consistently using the SQLite backup API; never copy
only a live database file while omitting its journal/WAL. Do not separately
restore purchases and jobs from different moments.

Restore drill: close paid intake and stop writers; validate a backup in a private
temporary location using integrity and foreign-key checks; verify schema,
ownership, quota, completed HTML/JSON and notification states; reconcile activity
after the snapshot; then switch the stopped service to the verified copy and
check local readiness. Do not open intake immediately after restoring an old
snapshot: lost post-snapshot usage could otherwise grant duplicate reviews.
Use synthetic records for drills; do not print reports or credentials. No actual
customer database or existing backup was accessed or changed in this task.

Support should record an opaque job ID and safe state/error code, authenticate
the owner through the trusted purchase system, and inspect the saved job before
retrying. Do not reset `used`, delete idempotency keys, or recreate the ledger to
resolve a complaint. Interrupted/storage-failed jobs need manual reconciliation
of the purchase, provider usage and any saved result before issuing a verified
replacement entitlement/refund through the authoritative purchase system.
Those support integrations are not implemented. Notification `unknown` can mean
SMTP accepted a message before acknowledgement was lost; retry may duplicate
email but must not repeat analysis. Existing notification retries remain bounded.

## Customer data and proposed retention

The following periods are proposed owner decisions, not legal compliance claims
or an implemented deletion schedule. **BLOCKED:** approve the access period,
retention policy, deletion workflow, privacy notice and processor arrangements
before paid launch. No existing customer files were deleted.

| Data | Present location and purpose | Proposed retention and access |
| --- | --- | --- |
| Original upload bytes and extracted text | Bounded request/worker memory and temporary parser/multipart spools; not written to `uploads/` by accepted intake | Close/remove temporary files after success/failure as implemented; use a private ephemeral temp directory and a host-specific orphan cleanup policy for crashes. No document contents in logs. |
| Image transcription cache | Process memory scoped to verified owner and purchase, bounded size and attempts | Existing 15-minute expiry and scheduled cleanup; not a persistent archive. Restart may require paid re-extraction. |
| Finalized HTML and JSON, source excerpts, notification customer name | Private shared SQLite job records; report recovery and business notification | Propose 90 days from completion. Access still requires a valid owner purchase; decide purchase/report expiry together. |
| Failed job metadata, fingerprints and idempotency keys | Private SQLite; diagnosis, retry and duplicate prevention | Propose 30 days for optional error metadata. Keep minimal deduplication/usage tombstones through the entire entitlement validity and reconciliation period. |
| Purchase owner/order IDs, tier, eligibility, usage and timestamps | Private SQLite; authorization, replay protection and reconciliation | Owner must select the minimum operational/accounting period, initially proposing 180 days after expiry. Never purge while an order can be presented again as eligible. No bearer proofs are stored. |
| Business notification emails | Configured business mailbox; internal notification includes the report and customer name | Align mailbox retention with the report period, restrict access, require MFA, and handle mailbox deletion independently. No customer inbox delivery is promised. |
| Safe operational event logs | Application/host logging | Propose 30 days; record status, safe error category, elapsed time and aggregate counts, not documents, identities, authorization headers, full URLs or reports. |
| Encrypted backups | Not configured | Propose a rolling 30-day window with restricted restore access. Restores must reapply the deletion/tombstone record before serving customers. |

Deletion needs a reviewed administrative workflow: verify the requester, stop
relevant work, expire access, remove report bodies and names while retaining only
necessary replay/usage tombstones, remove mailbox copies, and let encrypted
backups expire under policy. The current completed-report routes expect report
bodies; do not simply null columns in a live database. An expired/tombstone
response and auditable deletion mechanism need implementation after the owner
approves the policy. There is no automatic retention job or public deletion
endpoint in this package. Encryption at rest is a hosting dependency, not a
feature supplied by ordinary SQLite. External model/mail retention settings and
processor contracts remain **UNVERIFIED**.

## Logging and repository security

**PASS:** application diagnostics contain module names, support enums, counts
and length-limit metadata rather than full documents. Raw OpenAI/HTTP transport
logs are now discarded, including child debug logs, because SDK debug request
options can contain prompts/images even when headers are redacted. Default
Uvicorn access logs are suppressed because they contain IP addresses and URLs.
Application-owned sanitized warnings remain available. Deployment must preserve
these settings and disable proxy/APM body tracing, query/header capture and
request dumps; host-level logging is **UNVERIFIED**. Do not configure SMTP debug.

**PASS:** `.env` variants, private keys, uploads, report directories and SQLite
databases/journals/WAL files are ignored. Git ignores do not untrack historical
files or make a public directory private. A pattern scan of 726 current tracked
text files found no API-token, private-key or AWS-key matches; this is not proof
that every possible secret format is absent. The local `.env` was not read.
Historical commit `9cb44ea` contains `.env` with a nonempty SerpAPI assignment;
the owner previously reported the old key revoked/regenerated. History remains
unchanged. Reconfirm revocation and repository visibility before publication;
provider-side revocation was not verified. No credential values are reported.

Only `static/` is publicly mounted; customer uploads/reports/databases have no
public file route. Authenticated purchase ownership remains mandatory on
submission, report listing, report HTML/JSON, upload access and delivery retry.
Private report responses use no-store headers. Tests deny cross-customer access,
forged payment claims, altered tiers and disabled Tier 3 purchases.

## Timeouts limits and health

**PASS:** analysis now uses explicit I/O timeouts: connect/pool 5 seconds, write
30 seconds, read 120 seconds, with SDK retries disabled. The accepted one-time
structured response-length retry and bounded explicit job retry remain intact.
Image transcription keeps its existing shorter timeout and zero retries; SMTP
keeps its 20-second timeout. The [official OpenAI Python documentation](https://developers.openai.com/api/reference/python)
documents timeout and retry configuration. These are per-I/O bounds, not an
absolute whole-job deadline; the existing job fence prevents late publication.
A stalled provider can still occupy a daemon worker until transport returns.
Host supervision and a tested shutdown/restart procedure remain necessary.

Existing request body, file, page, decoded-image, parser-time, extracted-text,
per-purchase image-attempt and job/key limits remain in place. Analysis has two
process-local worker slots. These are not a shared ingress rate limiter.
**BLOCKED:** configure authorization-endpoint abuse limits, connection/time/body
limits and provider spending alerts at the approved ingress; validate global
behavior for the chosen worker topology. Do not increase workers to bypass
per-process resource limits.

`GET /health/live` returns only process liveness. `GET /health/ready` returns 503
unless a verifier and private readable/writable SQLite purchase/report schema
are configured. It does not create missing storage or call external providers.
It exposes no paths, identities or exceptions. A 200 means local dependencies
are accessible, not that payment verification, HTTPS, model billing or email has
been accepted. Restrict/rate-limit health polling at ingress too. Production
monitoring should alert on failed readiness, disk/backup failure, old processing
jobs, notification failure, provider timeouts and rejected requests using only
sanitized operational metadata.

## Completion boundary

Offline validation on November 19, 2026: **22 new tests passed**, **262 focused
operational/workflow tests passed**, and **1,255 full-suite tests passed** with
zero failures, errors or skips. All 1,233 previous tests remain passing. Seven
changed/new Python files passed syntax compilation; tracked and new-file diff
checks passed. The harness blocked external networking and local `.env`/original
upload access and recorded zero prohibited attempts. Outputs are temporary.

This validates the current working tree, including the pre-existing untracked
`test_photo_readability.py` and its readability fixture; it is not a clean-clone
production-build claim. Every pre-existing function in `main.py` is unchanged,
including HVAC analysis, canonical support, pricing and report/PDF rendering.
Purchase verification and reservation methods are unchanged. Test helpers and
the mobile acceptance runner only changed their temporary database provisioning
to the explicit initializer. `httpx` is now a declared direct dependency for the
timeout settings; production version locking still requires the chosen runtime.

The hosting-independent corrections are ready for offline review. The next work
package can be planned after this review, but paid launch must wait for the
owner-approved hosting/storage/TLS plan, reproducible build, trusted purchase
handoff, ingress controls, retention/deletion implementation and production
backup/recovery acceptance. No production readiness or multi-host compatibility
is implied by the regression results.
