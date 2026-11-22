# Closed Render deployment preparation

Prepared against accepted commit `43bed4a`, without deploying or activating
customer access. This entry point is closed even after storage is provisioned.
No trusted Wix verifier is included. No environment variable can supply one.

## Build and compatibility boundary

Target: Linux x86_64, CPython **3.13.18**, one CPU and 2 GB RAM. The Render
Blueprint uses the supported `standard` plan alias, one instance, one Uvicorn
process, a 10 GB disk at `/var/data`, Oregon, manual deployments, maintenance
mode enabled, and `/health/live`. Region and spend still need owner approval.

`render.yaml` uses JSON syntax, which is valid YAML. This permits regression
validation with the Python standard library without a new runtime dependency.
It was validated against https://render.com/schema/render.yaml.json. Current
field documentation: https://render.com/docs/blueprint-spec and
https://render.com/docs/python-version. A local schema pass is not a Render deploy.

`requirements.lock.txt` preserves all 33 accepted installed application and
transitive package versions. SHA-256 hashes identify the published release
artifacts. `requirements-build.lock.txt` separately pins pip, setuptools, wheel
and packaging; it does not upgrade the application packages. The original
`requirements.txt` remains unchanged for the existing development environment.

Public PyPI metadata was checked for Python 3.13.18/Linux: all selected runtime
versions declare compatible Python requirements and the dependency constraints
agree. Hash-verified Linux wheels were downloaded for 32 packages. The existing
`google-search-results==2.4.2` is source-only; its hash-verified source built into
a pure-Python wheel with the pinned build tools on macOS/Python 3.12.14.
An offline target-platform pip resolution also resolved all 33 runtime packages
without conflicts; this does not execute their Linux code.

Compatibility is not security clearance. The downloaded PyPI version metadata
flags advisories for preserved versions of anyio, certifi, idna, Pillow,
pillow-heif, PyMuPDF, pypdf, python-dotenv, python-multipart, requests, Starlette
and urllib3, and for the pinned pip/setuptools/wheel build tools. Examples of
the version-specific records are
[Pillow](https://pypi.org/pypi/Pillow/11.3.0/json),
[Starlette](https://pypi.org/pypi/starlette/0.49.3/json) and
[pip](https://pypi.org/pypi/pip/25.3/json).
Applicability and patched-version regression testing were not established in
this deployment task. No accepted application package was silently upgraded.
Keep maintenance mode and customer access closed; review and test applicable
security updates before accepting untrusted customer traffic. The hashes prove
artifact identity, not absence of vulnerabilities.

UNVERIFIED: executing these packages on Linux/Python 3.13.18, native HEIC/PDF
libraries under Linux's 512 MiB parser address-space limit, Render's actual
mount/ownership and process termination behavior, and whole-service memory at
20 uploads. Neither Mac tests nor wheel availability proves these properties.
The Linux wheels require the matching x86_64/glibc platform; verify the native
Render image rather than copying this Mac virtual environment.

Build in a fresh environment, never activate the tracked legacy `source/` tree:

```sh
python -m pip install --require-hashes -r requirements-build.lock.txt
python -m pip install --no-build-isolation --require-hashes -r requirements.lock.txt
python -m pip check
```

`--no-build-isolation` makes the source-only package use the pinned build tools
instead of fetching an unpinned build environment. Hashes include target Linux
wheels, current Mac Python 3.9 wheels and source archives. This pins dependency
inputs, not byte-for-byte identical compiled binaries on every platform.

For the full offline suite, install Node.js on the validation host and put it on
PATH. Node is a test dependency, not part of the running Python service:

```sh
python -B run_offline_tests.py
node --test test_quote_upload_ui.js
```

The runner requires Node, blocks external networking and `.env`/customer-upload
reads, supplies dummy credentials, writes private temporary results, and kills
the test process group after 600 seconds. Missing Node or skipped tests fail the
run. A clean Git checkout still needs the accepted but currently untracked
`test_photo_readability.py` and `test_fixtures/mobile_photo_readability_20261118.json`
resolved before claiming the exact local baseline is reproducible from GitHub.
This task does not add unrelated fixtures or stage files.

## Configuration contract

| Variable | Default / Blueprint value | Meaning |
| --- | --- | --- |
| `PYTHON_VERSION` | `3.13.18` | Render build runtime, also recorded in `.python-version` |
| `PORT` | Render-supplied | Uvicorn port; do not expose a separate origin port |
| `PYTHONUNBUFFERED` | `1` in Blueprint | Timely sanitized operational output |
| `PYTHON_DOTENV_DISABLED` | `1` | Prevent `.env` discovery; the deployment factory also enforces this |
| `OPENAI_API_KEY` | `closed-deployment-placeholder` | Non-secret import placeholder, not a usable credential |
| `EMAIL_APP_PASSWORD` | Empty | No email sending |
| `CYT_DISK_MOUNT` | `/var/data` | Must be a real, nonsymlink persistent mount in production |
| `CYT_DATA_DIR` | `/var/data/cyt` | Private direct child of the mount |
| `CYT_INPUT_MAX_BYTES` | `3000000000` | Aggregate stored-input budget |
| `CYT_FREE_RESERVE_BYTES` | `1000000000` | Free-space floor enforced on input writes |
| `CYT_INPUT_MAX_BUNDLES` | `10000` | Input bundle count bound |
| `CYT_QUEUE_ENABLED` | `false` | Exact `true`/`false`; true without an injected trusted verifier refuses startup |

Database and input paths are derived from the private data directory:
`ledger.sqlite3`, `inputs/`, and `ledger.sqlite3.queue.lock`. Completed HTML/JSON
remain inside the database. No PDF spool or public upload directory is added.
Never store bearer tokens or environment secrets on this disk as queued metadata.
Do not set SDK endpoint overrides or reuse development secrets for staging.

Mount and data paths reject traversal, symlink ancestors and application-tree
locations. Data and ledger must be on the same volume. The private root must be
service-owned with no group/other access. The adapter checks database ownership,
private permissions, schema and input-root binding. Documents retain existing
0400 permissions and hash verification; database/locks use 0600, directories 0700.
Upload limits do not bound report database growth: monitor disk space, database
size and backup footprint separately. No automatic deletion is configured.

## Initial provisioning and verification

Run these only after separately approving and creating the closed Render service.
Use its normal disk-backed Shell/SSH session, not a build, pre-deploy hook, cron
service or ephemeral one-off instance (those do not have this disk).

```sh
python -m deployment_ops closed-check
python -m deployment_ops init
python -m deployment_ops verify
python -m deployment_ops retention-preview
```

`closed-check` runs in-process HTTP checks with no provider calls: liveness 200,
readiness 503, unauthenticated upload/report access 401, and no scheduler started.
It does not prove Render routing, TLS or resource limits.

`init` requires the mount to exist, creates the private data root exclusively,
creates a fresh ledger, applies migrations 1/2/3, provisions inputs, and verifies
integrity/foreign keys. Existing roots/databases are never replaced. Partial
initialization is deliberately left in place and closed for operator inspection;
do not delete it or run `init` repeatedly to hide an error.

Normal startup never invokes initialization or migration. Missing/unusable
storage can coexist with the CLOSED health server so the operator can repair
configuration; no entitlement service is installed, readiness is 503 and all
customer operations remain denied. Queue-enabled startup instead refuses to
start when its dependencies are absent or invalid.

For a genuinely existing older ledger with its registered input store, close
intake, stop the scheduler, take the appropriate pre-upgrade backup, then run:

```sh
python -m deployment_ops migrate --confirm-closed
python -m deployment_ops verify
```

Migrations are explicit, additive and preserve records. Unknown versions,
integrity/foreign-key errors, live scheduler ownership, active/interrupted work,
or unreconciled restores are rejected. `verify` checks every non-retired source
hash and emits only schema/integrity status and aggregate job counts, not names,
document contents, credentials or paths. It may wait for operator-level locks;
it is not the HTTP health probe. A restored database can verify successfully
while correctly reporting `reconciliation_required: true`.

## Startup, shutdown and queue activation boundary

The Blueprint's exact start command is:

```sh
sh -c 'umask 077; exec python -m uvicorn deployment_app:create_app --factory --host 0.0.0.0 --port "$PORT" --workers 1 --lifespan on --no-access-log --no-proxy-headers --timeout-graceful-shutdown 20'
```

The factory reuses the existing routes, intake middleware, error handlers,
report/PDF controls and static mount. Public development API docs are omitted.
It never imports a verifier by environment-supplied module name and contains no
test-proof or payment bypass. A future reviewed Wix integration must explicitly
pass a verifier implementing both `verify` and `revalidate`, with bounded
transport timeouts, and enable the queue. The factory then assembles the same
ledger/report store, private storage, EntitlementService, QueueRuntime and
`process_saved_submission`. Supplying a verifier with the queue disabled is
rejected to prevent exposing the legacy non-durable submission path.

The existing lifespan starts one scheduler and two managed analysis threads,
holds the exclusive scheduler lock, and stops them on shutdown. No reload,
additional Uvicorn processes, autoscaling or external queue workers are allowed.
For a local supervised process, use SIGTERM to its known PID (or Ctrl-C in its
foreground terminal); never use a broad process-kill command. Render owns the
service process lifecycle. Its disk-backed services have a fixed 30-second
shutdown allowance and deployment downtime. Uvicorn's 20-second request drain
and the existing five-second queue stop are bounded, not a promise that a long
external call finishes. A remaining worker requires process termination;
recovery fences uncertain external work instead of automatically replaying it.

`/health/live` does no storage/provider work. `/health/ready` remains a separate
meaningful dependency assessment. It reads a SQLite snapshot instead of competing
for the scheduler's writer lock, bounds each SQLite lock wait to 50 ms, and never
waits for the upload lock. Sustained contention produces a prompt private 503.
Render must continue using **liveness** for restart decisions. Monitor readiness
over a sensible window instead of restarting on a single busy result. Readiness
never calls Wix/OpenAI or creates storage. Closed deployments intentionally
remain unready for paid traffic even with a healthy database.

## Backup preparation and execution

No schedule or transfer is installed. First keep Render maintenance mode enabled
and `CYT_QUEUE_ENABLED=false`. The current deployed factory has no verifier and
does not start a scheduler. Verify that `active_workers` is zero. The backup
helper independently refuses a live scheduler lock or interrupted active work.
Do not clear flags, usage or reservations to force a backup to succeed.

For future active queues: close intake, drain work, then perform a controlled
restart into the closed configuration before taking a backup. Maintenance mode
alone does NOT stop the scheduler. If work is stuck/uncertain, use reviewed
reconciliation rather than automatic replay or a forced snapshot.

Example commands for an approved disk, using a NEW unique snapshot name:

```sh
python -m deployment_ops verify
mkdir -m 700 /var/data/cyt/backup-staging
python -m deployment_ops backup --confirm-closed --destination /var/data/cyt/backup-staging/approved-snapshot-001 --max-bytes 4000000000
```

Create the staging parent once; if it exists, verify ownership/mode rather than
blindly chmodding or deleting it. Reserve space for a complete database/input
copy. `backup.json` is the completion marker written last. Failed partial copies
are not complete backups and remain for explicit operator handling. This command
calls the accepted Stage 3 application-consistent helper. It never transfers
data, sends email, changes live source records or clears restore gates.

A same-disk snapshot is not disaster recovery. The independent encrypted copy,
backup frequency, alerting and recovery-point target still require approval.
Render disk snapshots do not replace this application-level consistency and
entitlement reconciliation procedure. See `QUEUE_OPERATIONS.md`.

## Synthetic restore drill (temporary data only)

The explicit `--synthetic` option only permits canonical child directories under
the system temporary directory. It bypasses the physical-mount check, not private
permissions, schema, hashes, ownership, authentication or restore fencing. It
does not exist as a production environment switch or HTTP option.

```sh
CYT_DRILL=$(python -c 'import tempfile; from pathlib import Path; print(Path(tempfile.mkdtemp(prefix="cyt-render-drill-")).resolve())')
python -m deployment_ops init --synthetic --disk-mount "$CYT_DRILL" --data-dir "$CYT_DRILL/cyt"
python -m deployment_ops backup --synthetic --disk-mount "$CYT_DRILL" --data-dir "$CYT_DRILL/cyt" --confirm-closed --destination "$CYT_DRILL/snapshot" --max-bytes 4000000000
python -m deployment_ops restore --synthetic --disk-mount "$CYT_DRILL" --data-dir "$CYT_DRILL/cyt" --confirm-closed --snapshot "$CYT_DRILL/snapshot" --destination "$CYT_DRILL/restored" --max-bytes 4000000000
python -m deployment_ops verify --synthetic --disk-mount "$CYT_DRILL" --data-dir "$CYT_DRILL/restored"
```

This simple command drill restores an empty synthetic ledger. The offline queue
tests additionally restore synthetic queued/completed jobs and verify reports,
ownership and usage. Restores never overwrite existing directories and remain
fenced with `reconciliation_required=true`. There is deliberately no CLI option
to clear that gate. The existing explicit operator reconciliation API may only
be used after approved reconciliation of lost post-snapshot activity.

## Remaining acceptance gates

1. Resolve the accepted local-only test/fixture before claiming a full clean-clone
   test baseline; do not accidentally upload unrelated backups or proposal files.
2. Run the full suite with Node on Linux/Python 3.13.18 using the locked packages.
3. Approve Oregon, cost, independent backup destination/frequency, retention and
   operator responsibilities. Review historical secret revocation separately.
4. Authorize the first closed Render deployment; verify TLS, maintenance mode,
   actual mount identity/permissions, restart persistence and SIGTERM behavior.
5. Run the 20-submission synthetic text/photo/scanned-PDF test on one CPU/2 GB;
   measure memory, CPU, HTTP latency, lock contention and health responsiveness.
   Test isolated full-disk faults, corrupted inputs, service restart, report
   ownership, restore fencing, and provider failures using mocks.
6. Keep Wix verification, live provider credentials, queue activation and paid
   customer access unavailable until separately implemented and accepted.
7. Triage the dependency advisories above and validate required updates before
   customer access; this preserved-version lock is not a security certification.

## Local validation record

On macOS/Python 3.9.6, the final complete offline run passed **1,406 tests**:
the accepted 1,379 plus 27 deployment regressions, with zero failures, errors,
skips or recorded prohibited network/private-file attempts. The focused
deployment/readiness/Stage 2 run passed 90 tests. All 11 Node UI tests also passed
separately. Python syntax, file whitespace, `git diff --check`, published Render
Blueprint schema validation and target-platform dependency resolution passed.

An earlier full run exposed the readiness probe competing for SQLite's writer
lock. The snapshot read and bounded probes above corrected that interaction;
existing test assertions were preserved. An earlier focused run also saw
`test_process_crash_before_external_recovers_automatically` miss its expected
child-process exit (88 instead of 42). Five isolated repeats, the subsequent
focused run and final full suite passed without altering its timeout or
assertions. The isolated crash-test failure's cause remains unconfirmed; these
passes do not prove timing-dependent failures impossible.

These results validate local preparation, not Linux/Render runtime behavior or
permission to activate customers. All changes remain uncommitted for review.

No automatic retention, completed-report deletion, external backup transfers,
Wix calls, production emails, payments, provider requests or deployment were
performed by this preparation work.
