"""Explicit private maintenance, never invoked by app startup or a web route.

Source cleanup is safe with running workers. Backup/restore reconciliation needs
the service stopped and the SAME scheduler flock. All destinations are new,
private directories; no existing backup/customer data is overwritten or removed.
See QUEUE_OPERATIONS.md for operator prerequisites and reconciliation limits.
"""
from contextlib import contextmanager, closing
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from durable_uploads import PrivateUploadStore, _directory, _private, _read, _json, _ID, _FILE
from purchase_security import SQLitePurchaseLedger
from sqlite_migrations import check_version, require_reconciled

_BACKUP_MANIFEST_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class RetentionPolicy:
    temporary_seconds: int = 3600
    completed_input_seconds: int = 86400
    failed_input_seconds: int = 7 * 86400
    # Deliberately unsupported until the owner approves a report access policy.
    report_days: None = None

    def __post_init__(self):
        for value in (self.temporary_seconds, self.completed_input_seconds, self.failed_input_seconds):
            if type(value) is not int or value < 3600:
                raise ValueError('Retention requires at least one hour')
        if self.report_days is not None:
            raise ValueError('Report deletion is not enabled; owner approval and a separate rollout are required')


def _stage3(db):
    if check_version(db) != 3:
        raise ValueError('Explicit Stage 3 migration required')


def _remove_bundle(root, bundle):
    if not _ID.fullmatch(bundle):
        raise ValueError('Invalid bundle reference')
    try:
        directory = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    except FileNotFoundError:
        return  # Interrupted cleanup may already have removed this directory.
    try:
        _private(os.fstat(directory), directory=True)
        names = os.listdir(directory)
        for name in names:
            if not _FILE.fullmatch(name):
                raise ValueError('Unexpected stored document')
            _private(os.stat(name, dir_fd=directory, follow_symlinks=False))
        for name in names:
            os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
    os.rmdir(bundle, dir_fd=root)
    os.fsync(root)


def cleanup_inputs(storage, policy=RetentionPolicy(), *, dry_run=True, limit=100):
    """Bounded explicit cleanup. Default is preview; returns counts, no identities.

    A committed tombstone precedes deletion. Retrying after interruption is safe.
    Job/input/hold/idempotency rows remain. No consumed quota is replenished.
    Uncertain outcomes, active holds, retryable jobs and live workers are kept.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Choose a batch of 1 to 1000')
    result = {'eligible': 0, 'deleted': 0}
    with storage._locked() as root:
        with storage.records._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _stage3(db)
            require_reconciled(db)
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT i.job_id,i.bundle_id FROM review_inputs i
                JOIN review_jobs j USING(job_id) JOIN review_queue q USING(job_id)
                LEFT JOIN review_holds h USING(job_id) LEFT JOIN input_retention r USING(job_id)
                WHERE i.root_id=? AND q.worker_active=0 AND q.phase IN ('finished','failed')
                AND j.retryable=0 AND COALESCE(h.state,'released')!='active'
                AND (r.state='deleting' OR (r.job_id IS NULL AND
                    ((j.state='completed' AND COALESCE(q.completed_at,j.updated)<=?) OR (j.state='failed' AND j.updated<=?))))
                ORDER BY j.updated LIMIT ?""", (storage.root_id,
                    time.time() - policy.completed_input_seconds,
                    time.time() - policy.failed_input_seconds, limit)).fetchall()
            result['eligible'] = len(rows)
            if dry_run:
                return result
            for row in rows:
                db.execute("INSERT OR IGNORE INTO input_retention VALUES (?,'deleting',?,NULL)",
                           (row['job_id'], time.time()))
        # No scheduler can claim terminal jobs; tombstones also forbid loading.
        for row in rows:
            _remove_bundle(root, row['bundle_id'])
            with storage.records._connect() as db:
                db.execute("UPDATE input_retention SET state='deleted',deleted=? WHERE job_id=?",
                           (time.time(), row['job_id']))
            result['deleted'] += 1
    return result


def cleanup_temporary(storage, policy=RetentionPolicy(), *, dry_run=True, limit=100):
    """One-hour default applies ONLY to incomplete .tmp bundles, not saved inputs."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Choose a batch of 1 to 1000')
    result = {'eligible': 0, 'deleted': 0}
    with storage._locked() as root:
        with storage.records._connect() as db:
            _stage3(db)
            require_reconciled(db)
            refs = {row[0] for row in db.execute('SELECT bundle_id FROM review_inputs WHERE root_id=?', (storage.root_id,))}
        storage._usage(root)  # Fail closed on unexpected entries/symlinks.
        for name in sorted(os.listdir(root)):
            if not name.startswith('.tmp-') or name[5:] in refs:
                continue
            if os.stat(name, dir_fd=root, follow_symlinks=False).st_mtime > time.time() - policy.temporary_seconds:
                continue
            result['eligible'] += 1
            if not dry_run:
                directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                try:
                    for item in os.listdir(directory):
                        os.unlink(item, dir_fd=directory)
                    os.fsync(directory)
                finally:
                    os.close(directory)
                os.rmdir(name, dir_fd=root)
                os.fsync(root)
                result['deleted'] += 1
            if result['eligible'] == limit:
                break
    return result


@contextmanager
def stopped_queue(ledger):
    """Exclusive same-host scheduler lock; caller MUST stop inbound service too."""
    path = ledger.path
    if path != path.resolve() or path.stat().st_nlink != 1:
        raise ValueError('Canonical private ledger required')
    parent = _directory(path.parent)
    lock = None
    try:
        _private(os.fstat(parent), directory=True)
        lock = os.open(path.name + '.queue.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        _private(os.fstat(lock))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if lock is not None:
            os.close(lock)
        os.close(parent)


def _new_directory(path):
    path = Path(path)
    repo = Path(__file__).resolve().parent
    if path == repo or repo in path.parents:
        raise ValueError('Maintenance destinations must be outside the application repository')
    parent = _directory(path.parent)
    try:
        _private(os.fstat(parent), directory=True)
        os.mkdir(path.name, 0o700, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)
    return path


def _write(directory, name, data):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, 'wb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.fsync(directory)


def _copy(source, name, target, budget):
    """Stream bounded private files; never follow links or overwrite."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(fd, 'rb') as file:
        _private(os.fstat(file.fileno()))
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target)
        with os.fdopen(out, 'wb') as copy:
            while data := file.read(256 * 1024):
                budget[0] -= len(data)
                if budget[0] < 0:
                    raise ValueError('Maintenance byte budget exceeded')
                copy.write(data)
                digest.update(data)
                size += len(data)
            copy.flush()
            os.fsync(copy.fileno())
    os.fsync(target)
    return {'size': size, 'sha256': digest.hexdigest()}


def _references(db, root_id):
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute("""SELECT i.* FROM review_inputs i
        WHERE i.root_id=? AND NOT EXISTS(SELECT 1 FROM input_retention r WHERE r.job_id=i.job_id)""", (root_id,))]


def _check_database(db):
    _stage3(db)
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Database integrity validation failed')
    if db.execute('SELECT COUNT(*) FROM queue_storage').fetchone()[0] != 1:
        raise ValueError('Maintenance currently requires one private input root')
    if db.execute('SELECT 1 FROM review_queue WHERE worker_active=1 LIMIT 1').fetchone() or db.execute(
            "SELECT 1 FROM review_jobs WHERE state='processing' LIMIT 1").fetchone():
        raise ValueError('Active or interrupted workers must be reconciled before backup')


def create_backup(storage, destination, *, max_bytes):
    """Quiesced, application-consistent snapshot. No unapproved report deletion.

    SQLite writers and input writers/cleanup are excluded throughout. A complete
    marker is written LAST. Partial snapshots are retained for operator handling.
    Caller must transfer this private directory to an independent secure medium.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('Explicit backup byte budget required')
    ledger, budget = storage.records.ledger, [max_bytes]
    if storage.root == Path(destination) or storage.root in Path(destination).parents:
        raise ValueError('Backup destination must be separate from queued inputs')
    with stopped_queue(ledger), storage._locked() as root, ledger._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        require_reconciled(db)
        _check_database(db)
        references = _references(db, storage.root_id)
        destination = _new_directory(destination)
        target = _directory(destination)
        inventory = {}
        try:
            _write(target, 'ledger.sqlite3', b'')
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            # A different read connection avoids backing up a write transaction
            # into itself. BEGIN IMMEDIATE above excludes all concurrent writers.
            with closing(sqlite3.connect(ledger.path.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(destination / 'ledger.sqlite3')) as snapshot:
                    def progress(status, remaining, total):
                        if total * page_size > max_bytes:
                            raise ValueError('Backup database exceeds byte budget')
                    source.backup(snapshot, pages=128, progress=progress)
                    # Every snapshot is itself fenced, even a partially copied
                    # restore or an operator opening the snapshot directly.
                    with snapshot:
                        snapshot.execute('UPDATE restore_control SET reconciliation_required=1 WHERE singleton=1')
            data_fd = os.open('ledger.sqlite3', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=target)
            digest, size = hashlib.sha256(), 0
            with os.fdopen(data_fd, 'rb') as file:
                while chunk := file.read(256 * 1024):
                    digest.update(chunk); size += len(chunk)
                os.fsync(file.fileno())
            budget[0] -= size
            if budget[0] < 0:
                raise ValueError('Backup byte budget exceeded')
            inventory['ledger.sqlite3'] = {'size': size, 'sha256': digest.hexdigest()}
            os.mkdir('inputs', 0o700, dir_fd=target)
            inputs = os.open('inputs', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target)
            try:
                inventory['inputs/store.json'] = _copy(root, 'store.json', inputs, budget)
                for record in references:
                    storage._read_bundle(root, record)  # Verify anchored manifest and every source hash.
                    bundle = record['bundle_id']
                    os.mkdir(bundle, 0o700, dir_fd=inputs)
                    src = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                    dst = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=inputs)
                    try:
                        for name in os.listdir(src):
                            if not _FILE.fullmatch(name):
                                raise ValueError('Unexpected source file')
                            inventory[f'inputs/{bundle}/{name}'] = _copy(src, name, dst, budget)
                    finally:
                        os.close(src); os.close(dst)
                os.fsync(inputs)
            finally:
                os.close(inputs)
            metadata = _json({'format': 1, 'schema': 3, 'root_id': storage.root_id,
                'limits': {'max_bytes': storage.max_bytes, 'reserve_bytes': storage.reserve_bytes, 'max_bundles': storage.max_bundles},
                'files': inventory, 'created': time.time()})
            if len(metadata) > _BACKUP_MANIFEST_LIMIT:
                raise ValueError('Backup inventory exceeds the supported manifest limit')
            _write(target, 'backup.json', metadata)
        finally:
            os.close(target)
    return {'files': len(inventory), 'bytes': max_bytes - budget[0], 'complete': True}


def restore_backup(source, destination, *, max_bytes):
    """New private destination only. Processing stays blocked pending reconciliation."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('Explicit restore byte budget required')
    if Path(source) == Path(destination) or Path(source) in Path(destination).parents:
        raise ValueError('Restore destination must be separate from the snapshot')
    source_fd = _directory(source)
    try:
        _private(os.fstat(source_fd), directory=True)
        metadata = json.loads(_read(source_fd, 'backup.json', _BACKUP_MANIFEST_LIMIT))
        if metadata['format'] != 1 or metadata['schema'] != 3 or not _ID.fullmatch(metadata['root_id']):
            raise ValueError('Unsupported snapshot')
        files = metadata['files']
        if not {'ledger.sqlite3', 'inputs/store.json'} <= files.keys():
            raise ValueError('Incomplete snapshot')
        allowed = re.compile(r'(?:ledger\.sqlite3|inputs/store\.json|inputs/[0-9a-f]{32}/(?:manifest\.json|\d{4}\.bin))')
        if any(not allowed.fullmatch(name) for name in files):
            raise ValueError('Invalid snapshot path')
        if sum(item['size'] for item in files.values()) > max_bytes:
            raise ValueError('Restore byte budget exceeded')
        destination = _new_directory(destination)
        budget = [max_bytes]
        for name in sorted(files, key=lambda name: (name != 'ledger.sqlite3', name)):
            relative = Path(name)
            parent = destination
            for part in relative.parts[:-1]:
                parent = parent / part
                if not parent.exists():
                    _new_directory(parent)
            src = _directory(Path(source) / relative.parent)
            dst = _directory(parent)
            try:
                if _copy(src, relative.name, dst, budget) != files[name]:
                    raise ValueError('Snapshot checksum mismatch')
                if relative.name == 'manifest.json' or relative.suffix == '.bin':
                    os.chmod(relative.name, 0o400, dir_fd=dst, follow_symlinks=False)
            finally:
                os.close(src); os.close(dst)
            if name == 'ledger.sqlite3':
                restored = SQLitePurchaseLedger(destination / name)
                with restored._connect() as db:
                    _check_database(db)
                    db.execute('UPDATE restore_control SET reconciliation_required=1 WHERE singleton=1')
        restored = SQLitePurchaseLedger(destination / 'ledger.sqlite3')
        with restored._connect() as db:
            _check_database(db)
            roots = db.execute('SELECT root_id FROM queue_storage').fetchall()
            if roots[0][0] != metadata['root_id']:
                raise ValueError('Snapshot input identity mismatch')
            db.execute('UPDATE queue_storage SET root_path=? WHERE root_id=?', (str(destination / 'inputs'), metadata['root_id']))
            db.execute('DELETE FROM queue_owner')
        inputs = _directory(destination / 'inputs')
        try:
            _write(inputs, '.lock', b'')
        finally:
            os.close(inputs)
        storage = PrivateUploadStore(destination / 'inputs', restored, **metadata['limits'])
        with storage._locked() as root, restored._connect() as db:
            expected = {'ledger.sqlite3', 'inputs/store.json'}
            for record in _references(db, storage.root_id):
                manifest, data = storage._read_bundle(root, record)
                expected.add(f"inputs/{record['bundle_id']}/manifest.json")
                expected.update(f"inputs/{record['bundle_id']}/{name}" for name in data)
            if set(files) != expected:
                raise ValueError('Snapshot references do not match its files')
        return storage
    finally:
        os.close(source_fd)


def confirm_restore_reconciled(storage, *, entitlements_reconciled=False, external_outcomes_reconciled=False):
    """Operator acknowledgment AFTER comparing snapshot with post-snapshot events.

    Never called automatically. This cannot infer lost purchases/AI executions.
    Required ledger/job corrections must be reviewed separately before approval.
    """
    if entitlements_reconciled is not True or external_outcomes_reconciled is not True:
        raise ValueError('Explicit entitlement and external-outcome reconciliation required')
    with stopped_queue(storage.records.ledger), storage._locked() as root, storage.records._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _check_database(db)
        for record in _references(db, storage.root_id):
            storage._read_bundle(root, record)
        db.execute('UPDATE restore_control SET reconciliation_required=0 WHERE singleton=1')
