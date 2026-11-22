"""Local operator commands only; never imported by customer HTTP routes."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile

from deployment_storage import DeploymentConfig, DeploymentError, open_storage
from durable_uploads import PrivateUploadStore, _directory
from purchase_security import SQLitePurchaseLedger
from queue_maintenance import (create_backup, restore_backup, stopped_queue,
    cleanup_inputs, cleanup_temporary, _references)
from sqlite_migrations import check_version, migrate_stage1, migrate_stage2, migrate_stage3, require_reconciled


def provision(config):
    """Exclusive fresh root; interrupted provisioning is left closed for inspection."""
    config.check_mount()
    parent = _directory(config.disk_mount)
    try:
        space = os.fstatvfs(parent)
        if (space.f_bavail * space.f_frsize <= config.reserve_bytes
                or space.f_blocks * space.f_frsize < config.max_bytes + config.reserve_bytes):
            raise DeploymentError('Insufficient persistent storage for the configured budget.')
        os.mkdir(config.data_dir.name, 0o700, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)
    config.check_private_root()
    ledger = SQLitePurchaseLedger.initialize(config.ledger_path)
    migrate_stage2(ledger)
    migrate_stage3(ledger)
    PrivateUploadStore.provision(config.input_path, ledger, **config.limits)
    return verify(config)


def migrate(config):
    config.check_private_root()
    ledger = SQLitePurchaseLedger(config.ledger_path)
    with stopped_queue(ledger):
        with ledger._connect() as db:
            require_reconciled(db)
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
                raise DeploymentError('Database integrity verification failed.')
            has_reports = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_jobs'").fetchone()
            if ((check_version(db) >= 2 and db.execute('SELECT 1 FROM review_queue WHERE worker_active=1 LIMIT 1').fetchone())
                    or (has_reports and db.execute("SELECT 1 FROM review_jobs WHERE state='processing' LIMIT 1").fetchone())):
                raise DeploymentError('Interrupted work requires reconciliation before migration.')
        migrate_stage1(ledger)
        migrate_stage2(ledger)
        migrate_stage3(ledger)
    return verify(config)


def verify(config):
    ledger, reports, storage = open_storage(config, integrity=True, allow_unreconciled=True)
    with storage._locked() as root, ledger._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for record in _references(db, storage.root_id):
            storage._read_bundle(root, record)
        counts = dict(db.execute('SELECT state,COUNT(*) FROM review_jobs GROUP BY state'))
        active = db.execute('SELECT COUNT(*) FROM review_queue WHERE worker_active=1').fetchone()[0]
        reconcile = bool(db.execute('SELECT reconciliation_required FROM restore_control WHERE singleton=1').fetchone()[0])
        db.rollback()
    return {'schema': 3, 'integrity': 'ok', 'source_hashes': 'ok', 'job_counts': counts,
            'active_workers': active, 'reconciliation_required': reconcile,
            'queue_environment_enabled': config.queue_enabled}


def closed_check(config):
    if config.queue_enabled:
        raise DeploymentError('Closed validation requires CYT_QUEUE_ENABLED=false.')
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ.setdefault('OPENAI_API_KEY', 'closed-deployment-placeholder')
    from deployment_app import create_app
    from fastapi.testclient import TestClient
    application = create_app(config)
    with TestClient(application) as client:
        statuses = {'live': client.get('/health/live').status_code,
                    'ready': client.get('/health/ready').status_code,
                    'upload': client.post('/upload').status_code,
                    'reports': client.get('/jobs').status_code}
    if statuses != {'live': 200, 'ready': 503, 'upload': 401, 'reports': 401}:
        raise DeploymentError('Closed-service validation failed.')
    return {'closed': True, 'http_statuses': statuses, 'queue_started': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'migrate', 'verify', 'closed-check', 'retention-preview', 'backup', 'restore'])
    parser.add_argument('--disk-mount', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--synthetic', action='store_true', help='Only allow isolated storage beneath the system temporary directory; skip the real mount requirement.')
    parser.add_argument('--confirm-closed', action='store_true', help='Operator confirms inbound traffic and the scheduler are stopped.')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--max-bytes', type=int)
    args = parser.parse_args(argv)
    try:
        config = DeploymentConfig.from_env()
        config = replace(config, disk_mount=args.disk_mount or config.disk_mount,
                         data_dir=args.data_dir or config.data_dir)
        if args.synthetic:
            temp = Path(tempfile.gettempdir()).resolve()
            if temp not in config.disk_mount.parents or config.disk_mount != config.disk_mount.resolve():
                raise DeploymentError('Synthetic storage must be a canonical child of the system temporary directory.')
            config = replace(config, require_mount=False)
        if config.queue_enabled:
            raise DeploymentError('Operator procedures require queue-disabled configuration.')
        if args.command in {'migrate', 'backup', 'restore'} and not args.confirm_closed:
            raise DeploymentError('Confirm closed intake and stopped workers before maintenance.')
        if args.command == 'init':
            result = provision(config)
        elif args.command == 'migrate':
            result = migrate(config)
        elif args.command == 'verify':
            result = verify(config)
        elif args.command == 'closed-check':
            result = closed_check(config)
        elif args.command == 'retention-preview':
            _, _, storage = open_storage(config)
            result = {'inputs': cleanup_inputs(storage), 'temporary': cleanup_temporary(storage), 'dry_run': True}
        elif args.command == 'backup':
            if args.destination is None or args.max_bytes is None:
                raise DeploymentError('An explicit new destination and byte budget are required.')
            _, _, storage = open_storage(config, integrity=True)
            result = create_backup(storage, args.destination, max_bytes=args.max_bytes)
        else:
            if args.snapshot is None or args.destination is None or args.max_bytes is None:
                raise DeploymentError('An explicit snapshot, new destination and byte budget are required.')
            # The helper never overwrites the live database, and leaves the
            # restored copy fenced. No CLI option clears reconciliation.
            config.check_mount()
            if args.destination.parent != config.disk_mount:
                raise DeploymentError('Restore into a new private child of the approved disk.')
            storage = restore_backup(args.snapshot, args.destination, max_bytes=args.max_bytes)
            restored = replace(config, data_dir=args.destination, **{
                'max_bytes': storage.max_bytes, 'reserve_bytes': storage.reserve_bytes, 'max_bundles': storage.max_bundles})
            result = verify(restored)
        print(json.dumps({'ok': True, **result}, sort_keys=True))
        return 0
    except Exception:
        # Exception text can contain customer values/paths. Operator output is
        # intentionally finite; use the documented verification steps to repair.
        print(json.dumps({'ok': False, 'error': 'deployment_operation_failed',
            'action': 'Check mount, private permissions, schema, maintenance state and explicit command arguments. Existing data was not replaced.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
