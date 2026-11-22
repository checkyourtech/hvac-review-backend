# SQLite queue Stage 3 operations (not production activation)

The queue remains disabled by default. This work does not configure Render, Wix,
credentials, email, recurring maintenance or cloud backups. Every command below
is an operator procedure for approved private storage, not a customer endpoint.
Do not run against existing customer data until the owner approves the policy.

## Supported topology and launch gates

Use one web-service instance, one application process, two managed analysis
workers, and the scheduler. Put the SQLite database, its journals, scheduler lock
and private document store on the same persistent POSIX volume. Use restrictive
permissions (directories 0700, database/locks 0600, immutable documents 0400).
Do not place them in the repository, static content, ephemeral deployment storage,
network filesystems or public download directories. No Redis or extra database
service is required. Fail readiness if the volume, schema, restore reconciliation,
purchase verification or managed runtime is unavailable. Never initialize a fresh
database as recovery for a missing volume.

Render configuration is still UNVERIFIED: disk mount/size, one process/instance,
restart and shutdown behavior, HTTPS/proxy trust, monitoring, persistent paths,
resource limits, independent backup destination and a restore drill must be
confirmed. The scheduler holds its flock until every worker exits. A timeout
fences publication but cannot kill a Python thread; a stuck worker keeps its
slot. Supervision must stop the process before a replacement can own the queue.
Test cold restarts and deployment termination on the actual one-CPU/2-GB host.
Do not advertise local synthetic throughput as production capacity.

Wix work remains BLOCKED: authoritative customer authentication, paid product to
tier mapping, refund/cancellation/expiry verification, purchase quantities, and
bounded server-side revalidation must be implemented and accepted. Tier 3 stays
disabled. No browser payment callback or supplied package name is authoritative.
The existing runtime requires explicit trusted configuration; no environment
flag or UI control activates it.

## Schema and customer reconnect contract

Stop the service, take a verified pre-upgrade snapshot using the existing
coordinated operational procedure, and call `migrate_stage3(ledger)` from
`sqlite_migrations`. It upgrades version 2 to 3 transactionally, preserves all
records, adds a stable completion timestamp, retention tombstones/index and a
restore reconciliation gate. It does not start cleanup. Do not migrate while
an older application binary is serving requests. Roll back application/schema
changes only through a separately reconciled restore; do not replay an old
snapshot over paid activity.

The local upload UI receives proof through `window.configureQuoteUpload` after
trusted authentication. It keeps proof only in memory, uses authenticated
`/jobs`, `/jobs/{id}`, `/jobs/{id}/report`, and `/jobs/{id}/result` requests with
no-store caching. On reauthentication it clears prior customer UI and ignores
late responses from the previous session. A stable job ID is returned on durable
acceptance. Closing the page does not cancel a queued job. Reauthentication lists
up to the existing 20 jobs for that purchase, then follows an outstanding job.
Polling is bounded to 60 attempts with 5–15-second intervals; status/report fetches
have a 15-second timeout, and a manual refresh remains available afterward. Initial
submission allows four minutes for the existing upload/preparation budgets. Queue
position is approximate (revalidation pauses may alter order). No completion
estimate is promised. Status never contains documents, customer identity, worker
claim tokens or storage paths.

Customers may reopen the original HTML report and use its existing browser-print
PDF controls. No report layout changes were made. A permitted notification retry
uses the existing bounded, idempotent `/delivery-retry` operation; that notification
is to the internal business, not a newly implemented customer email service.
Queue preparation retries remain automatic. There is deliberately no customer
analysis-retry button for uncertain external execution: contact support instead.
A corrected unreadable document can be submitted as new content when its hold was
released and the purchase is still eligible. Replaying identical content retains
the original job, even after its source files have been retired.

## Controlled document retention

`queue_maintenance.RetentionPolicy` is explicit configuration, with defaults:

- Incomplete `.tmp-*` uploads: eligible after one hour.
- Completed-review source documents: eligible 24 hours after report completion.
- Terminal failed inputs: eligible after seven days only if no retry is allowed,
  no live worker exists, no active hold remains, and the outcome is not uncertain.
- Reports: no deletion. The proposed 90-day access policy needs owner approval and
  a separate controlled rollout. Setting `report_days` is currently rejected.
- Purchases, entitlement usage, holds, input references, request keys and job
  ownership: retained for reconciliation. Deletion never replenishes usage.

Use `cleanup_temporary(storage, policy)` and `cleanup_inputs(storage, policy)` to
preview counts. Apply only after approval using `dry_run=False`, with a bounded
batch (default 100, maximum 1000). Re-run batches until no eligible entries remain.
Schedule controlled maintenance hourly to pursue the 24-hour source target;
24 hours is eligibility, not a guaranteed physical deletion time. Cleanup is not
silently installed in the runtime. Existing real customer files were not touched.
The Stage 1 `cleanup_orphans` remains a separate explicit operation with a
24-hour default grace for saved but unregistered bundles; run only after restore
reconciliation. Do not use a blanket directory age-deletion command.

Cleanup uses the private store lock and transactional eligibility checks. It
commits a deletion marker before removing files, then records completion. An
interruption leaves a marker that the next batch safely completes. It excludes
active workers, permits no new claim on a terminal job, and input reads refuse
retired documents. Symlink/unexpected-file errors fail closed. Never manually
delete referenced files to free space. Monitor disk space, database size, input
bytes/bundles, eligible backlog, oldest queue age, stalled workers, failed jobs,
uncertain external outcomes, backup age and cleanup errors. Returned maintenance
counts can be logged; proposal contents and exception bodies must not be logged.

Source deletion does not delete the report, its customer/name metadata, extracted
findings or an older backup. Approve report retention, backup rotation and support
access separately. Operators and independent backup accounts require least
privilege, encryption in transit/at rest and an access audit process. No legal
compliance guarantee is made.

## Application-consistent backup

The implemented Stage 3 procedure requires a migrated version-3 database and one
registered input root. It copies all purchases/submissions/holds, jobs, reports,
request keys, queue states and every non-retired referenced input/manifest. It
excludes temporary/orphan files, environment files, tokens and configuration
secrets. Public customer metadata is not logged; the snapshot itself is private
customer data.

1. Stop inbound service and let workers stop. Do not assume a deployment deadline
   killed a thread. If workers crashed, reconcile interrupted claims with the
   accepted recovery procedure before backup. Active/interrupted workers prevent
   backup; do not clear their flags merely to make a backup succeed.
2. Open the existing `SQLitePurchaseLedger` and `PrivateUploadStore` using the
   approved absolute paths and the SAME configured storage limits. Do not use
   `.initialize()` or `.provision()` as a fallback.
3. Call `create_backup(storage, new_destination, max_bytes=approved_budget)`.
   The destination parent must already be private. Use a new destination each time.
   It acquires the scheduler flock and input lock, blocks database writers with
   BEGIN IMMEDIATE, uses SQLite's backup API and verifies source hashes. No source
   database or customer file is changed. The completion manifest `backup.json` is
   written last after file flushing. A missing marker is an incomplete backup.
   Database/full-disk/copy failures do not publish a complete snapshot.
   The backup inventory is limited to 4 MiB; an oversized inventory fails safely
   before publication. Monitor retained file counts as well as source bytes.
4. Transfer the entire snapshot to an independently durable, private destination,
   with access controls/encryption and authenticated checksums. The local backup
   helper does not provision or perform this transfer. A same-volume copy alone
   is not disaster recovery. File hashes detect corruption, not malicious changes
   by someone who can rewrite the whole backup.
5. Verify the remote copy by restoring it into a new isolated directory and
   validating expected synthetic/report counts. Record backup time, verified
   completion, transfer result and drill result without customer contents.

Set a backup frequency and recovery-point objective before launch. Quiesced backups
require a maintenance window and temporarily stop submissions. This is a known
single-instance compromise; online backups without quiescence are not claimed.

## Restore and reconciliation

Call `restore_backup(snapshot, new_destination, max_bytes=approved_budget)` with
no application process using the destination. Existing destinations are never
overwritten. The procedure checks checksums, schema, SQLite integrity/foreign
keys and every referenced manifest/document; it rebinds the input root to the new
private path. Retired references remain tombstones. A partially failed restore
is not a usable replacement. Keep it isolated for inspection; never point the
service at it. Snapshot databases are already fenced before being copied.

Every restored database starts with `reconciliation_required=1`. Queue startup,
claims, submissions/consumption, notification retries, readiness and cleanup fail closed. Completed
reports can be inspected through authorized access while execution is blocked.
Reconcile the snapshot against purchase/refund events, paid usage and external
execution after its timestamp. In particular, a job that was queued at backup
may have executed afterward. The snapshot alone cannot tell you that. Do not
promise exactly-once OpenAI execution. Unknown outcomes require support review;
never reset consumed entitlements or blindly replay them.

Only after reviewed corrections and verification, call
`confirm_restore_reconciled(storage, entitlements_reconciled=True,
external_outcomes_reconciled=True)` while the service remains stopped. This is an
operator attestation, not an automated proof. It rechecks database and file
integrity before lifting the gate. Then use the existing explicit production
configuration and perform a controlled startup, ownership check and report
retrieval check. There is no public reconciliation endpoint.

## Deadlines and acceptance testing

Retain the existing 24-hour wait deadline, 15-minute processing deadline, two
preparation attempts and 60-second retry delay until measured deployment evidence
supports a change. These are safety bounds, not delivery promises. Synthetic tests
prove transitions and fencing, not realistic provider latency or purchase-service
outage duration. Two attempts can exhaust after a short verification outage;
provide a support reconciliation path before launch. Uncertain external work is
never automatically replayed. A notification failure cannot remove a saved report.

Run bounded synthetic acceptance at 20 simultaneous customers on the proposed
Render configuration before activation. Expect two active jobs and 18 saved
waiting jobs, progression without browser connection, unique consumption and
cross-customer isolation. Measure peak memory, CPU, queue age, disk usage and
request latency with text, image and scanned-PDF workloads. Terminate the process
before/after external marking; verify safe retries versus uncertain outcomes,
late-worker fencing and no slot replacement for still-running threads. Exercise
full disk, corrupt/missing inputs, unavailable purchase verification, failed
notifications, restore blocking and authenticated reconnection. Use mocks for
external services until a separately authorized, tightly bounded live test.

The intermittent Stage 2 commissioning 503 and notification timeout remain
unexplained unless a recurrence supplies evidence. Test fixtures now block the
SDK before network, isolate OCR cache and stop workers before restoring global
patches. Queue test timeouts include sanitized state and stack locations. Do not
raise timeouts or weaken assertions to conceal a recurrence.
