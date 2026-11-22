"""Explicit single-volume deployment configuration; never provision on startup.

No dotenv, provider clients, customer documents or credentials are loaded here.
The operator CLI owns initialization and migrations; HTTP startup only opens.
"""
from dataclasses import dataclass
import os
from pathlib import Path

from durable_uploads import PrivateUploadStore, _directory, _private
from purchase_security import SQLitePurchaseLedger
from report_jobs import SQLiteReportStore
from sqlite_migrations import check_version, require_reconciled


class DeploymentError(Exception):
    """Sanitized operator error; never include filesystem or customer values."""


@dataclass(frozen=True)
class DeploymentConfig:
    disk_mount: Path = Path('/var/data')
    data_dir: Path = Path('/var/data/cyt')
    max_bytes: int = 3_000_000_000
    reserve_bytes: int = 1_000_000_000
    max_bundles: int = 10000
    queue_enabled: bool = False
    # False is reserved for explicit temporary synthetic tests/operator drills.
    # There is deliberately no environment switch for bypassing the mount check.
    require_mount: bool = True

    def __post_init__(self):
        repo = Path(__file__).resolve().parent
        for path in (self.disk_mount, self.data_dir):
            if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
                    or path == repo or repo in path.parents or path in repo.parents):
                raise DeploymentError('Choose absolute private storage outside the application tree.')
        if self.data_dir.parent != self.disk_mount:
            raise DeploymentError('The private data directory must be a direct child of the disk mount.')
        for value in (self.max_bytes, self.reserve_bytes, self.max_bundles):
            if type(value) is not int or value <= 0:
                raise DeploymentError('Storage budgets must be positive integers.')
        if type(self.queue_enabled) is not bool or type(self.require_mount) is not bool:
            raise DeploymentError('Deployment switches must be explicit booleans.')

    @classmethod
    def from_env(cls):
        enabled = os.environ.get('CYT_QUEUE_ENABLED', 'false')
        if enabled not in ('true', 'false'):
            raise DeploymentError('CYT_QUEUE_ENABLED must be true or false.')
        try:
            return cls(disk_mount=Path(os.environ.get('CYT_DISK_MOUNT', '/var/data')),
                data_dir=Path(os.environ.get('CYT_DATA_DIR', '/var/data/cyt')),
                max_bytes=int(os.environ.get('CYT_INPUT_MAX_BYTES', '3000000000')),
                reserve_bytes=int(os.environ.get('CYT_FREE_RESERVE_BYTES', '1000000000')),
                max_bundles=int(os.environ.get('CYT_INPUT_MAX_BUNDLES', '10000')),
                queue_enabled=enabled == 'true')
        except ValueError:
            raise DeploymentError('Invalid deployment storage configuration.') from None

    @property
    def ledger_path(self):
        return self.data_dir / 'ledger.sqlite3'

    @property
    def input_path(self):
        return self.data_dir / 'inputs'

    @property
    def limits(self):
        return dict(max_bytes=self.max_bytes, reserve_bytes=self.reserve_bytes, max_bundles=self.max_bundles)

    def check_mount(self):
        fd = _directory(self.disk_mount)  # Reject symlinks throughout the path.
        try:
            if os.fstat(fd).st_mode & 0o022:
                raise DeploymentError('The persistent mount must not be writable by other users.')
            if self.require_mount and not os.path.ismount(self.disk_mount):
                raise DeploymentError('The configured persistent disk is not mounted.')
        finally:
            os.close(fd)

    def check_private_root(self):
        self.check_mount()
        fd = _directory(self.data_dir)
        try:
            _private(os.fstat(fd), directory=True)
            if os.fstat(fd).st_dev != self.disk_mount.stat().st_dev:
                raise DeploymentError('Database and inputs must use the configured disk.')
        finally:
            os.close(fd)


def open_storage(config, *, integrity=False, allow_unreconciled=False):
    """Open existing version-3 storage. Missing/unsafe state is never repaired."""
    config.check_private_root()
    _private(config.ledger_path.lstat())
    ledger = SQLitePurchaseLedger(config.ledger_path)
    with ledger._connect() as db:
        if check_version(db) != 3:
            raise DeploymentError('Explicit migration to schema version 3 is required.')
        if not allow_unreconciled:
            require_reconciled(db)
        if integrity and (db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                          or db.execute('PRAGMA foreign_key_check').fetchall()):
            raise DeploymentError('Database integrity verification failed.')
        if db.execute('SELECT COUNT(*) FROM queue_storage').fetchone()[0] != 1:
            raise DeploymentError('Exactly one registered input store is required.')
    reports = SQLiteReportStore(ledger)
    storage = PrivateUploadStore(config.input_path, ledger, **config.limits)
    if config.input_path.stat().st_dev != config.ledger_path.stat().st_dev:
        raise DeploymentError('Database and inputs must share the persistent disk.')
    return ledger, reports, storage
