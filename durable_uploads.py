"""Private durable inputs, reused by the opt-in Stage 2 customer queue.

Provision an explicit persistent directory once, then open it without creation.
All writers/cleanup use its advisory lock; filesystem finalization precedes the
single database registration transaction. An ambiguous failure leaves an orphan
for explicit cleanup, never deletes a possibly registered customer's documents.
Raw originals may contain EXIF/customer data: this entire store is private data.

Operator contract (no deployment or live-data migration is performed here):
* Upgrade an existing ledger explicitly with sqlite_migrations.migrate_stage1;
  new SQLitePurchaseLedger.initialize databases start at version 1.
* Provision this store once on a private persistent POSIX filesystem outside
  every web-served directory. All processes must share its database, root and
  limits. Open with PrivateUploadStore on restart; never provision as fallback.
* Keep database and input files together in a coordinated backup/restore plan.
  Independent rollback of one side requires reconciliation before cleanup.
* cleanup_orphans is explicit maintenance with a 24-hour default grace period.
  It does not expire referenced inputs. A retention/deletion workflow and disk
  monitoring must be configured before customer queue rollout.
* Local format validation is not OCR readability acceptance. Later processing
  must retain the existing photo-retake safeguards before paid HVAC analysis.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import BinaryIO
import uuid

from fastapi import HTTPException
from starlette.datastructures import FormData, Headers, UploadFile
from document_intake import (IMAGE_FORMATS, MAX_FILE_BYTES, MAX_UPLOAD_BODY,
    MAX_PDF_PAGES, MAX_VISUAL_PAGES_PER_QUOTE, MAX_PREPARED_BYTES,
    MAX_TEXT_CHARS, MAX_TOTAL_TEXT_CHARS, prepare_document, quote_groups,
    validate_customer, validate_filename, validate_text)
from purchase_security import QUOTE_LIMITS
from queue_inputs import SQLiteQueuedInputs
from sqlite_migrations import require_reconciled

_ID = re.compile(r"[0-9a-f]{32}")
_BUNDLE = re.compile(r"(?:\.tmp-)?[0-9a-f]{32}")
_FILE = re.compile(r"(?:\d{4}\.bin|manifest\.json)")
_CHUNK = 64 * 1024
_MANIFEST_LIMIT = 64 * 1024


class StoredInputError(Exception):
    """Confirmed missing/corrupted inputs, distinct from unavailable storage."""


@dataclass(frozen=True)
class UploadSource:
    filename: str
    content_type: str
    stream: BinaryIO


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _private(info, directory=False):
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not valid_type or info.st_mode & 0o077 or info.st_uid != os.getuid()
            or (not directory and info.st_nlink != 1)):
        raise OSError("Unsafe storage object")


def _directory(path):
    """Walk from / using directory FDs, rejecting every symlink and '..'."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise OSError("Explicit private absolute path required")
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, 'rb') as file:
        _private(os.fstat(file.fileno()))
        if os.fstat(file.fileno()).st_size > limit:
            raise OSError("Stored file exceeds limit")
        data = file.read(limit + 1)
        if len(data) > limit:
            raise OSError("Stored file exceeds limit")
        return data


class PrivateUploadStore:
    def __init__(self, root, ledger, *, max_bytes, reserve_bytes=64 * 1024 * 1024, max_bundles=10000):
        self.root = Path(root)
        self.records = SQLiteQueuedInputs(ledger)
        if max_bytes <= 0 or reserve_bytes < 0 or max_bundles < 1:
            raise ValueError("Explicit positive storage limits are required")
        self.max_bytes, self.reserve_bytes, self.max_bundles = max_bytes, reserve_bytes, max_bundles
        # No repository/public directory, even if private permissions happen to
        # be set today. The persistent volume is an explicit operator decision.
        repo = Path(__file__).resolve().parent
        if self.root == repo or repo in self.root.parents:
            raise ValueError("Queued inputs must be outside the application repository")
        with self._locked() as fd:
            marker = json.loads(_read(fd, 'store.json', 1024))
            if set(marker) != {'version', 'root_id'} or marker['version'] != 1 or not _ID.fullmatch(marker['root_id']):
                raise OSError("Invalid storage identity")
            self.root_id = marker['root_id']
            self._binding()

    @classmethod
    def provision(cls, root, ledger, *, max_bytes, reserve_bytes=64 * 1024 * 1024, max_bundles=10000):
        """Explicit exclusive provisioning, never an unavailable-volume fallback."""
        root = Path(root)
        repo = Path(__file__).resolve().parent
        if not root.is_absolute() or '..' in root.parts or root == repo or repo in root.parents:
            raise ValueError("Choose a private absolute persistent path outside the repository")
        records = SQLiteQueuedInputs(ledger)
        parent = _directory(root.parent)
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent)
            fd = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                root_id = uuid.uuid4().hex
                for name, data in [('store.json', _json({'version': 1, 'root_id': root_id})), ('.lock', b'')]:
                    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                    with os.fdopen(out, 'wb') as file:
                        file.write(data)
                        file.flush()
                        os.fsync(file.fileno())
                os.fsync(fd)
                os.fsync(parent)
                with records._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('INSERT INTO queue_storage VALUES (?,?,?)', (root_id, str(root), time.time()))
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        return cls(root, ledger, max_bytes=max_bytes, reserve_bytes=reserve_bytes, max_bundles=max_bundles)

    def _binding(self):
        with self.records._connect() as db:
            row = db.execute('SELECT root_path FROM queue_storage WHERE root_id=?', (self.root_id,)).fetchone()
            if not row or row[0] != str(self.root):
                raise OSError("Storage does not belong to this database")

    @contextmanager
    def _locked(self):
        fd = lock = None
        try:
            fd = _directory(self.root)
            _private(os.fstat(fd), directory=True)
            identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            if hasattr(self, '_identity') and identity != self._identity:
                raise OSError("Storage directory changed")
            self._identity = identity
            lock = os.open('.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
            _private(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX)
            if hasattr(self, 'root_id'):
                self._binding()
            yield fd
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, "Private input storage is unavailable. Check saved submission status before retrying.") from None
        finally:
            if lock is not None:
                os.close(lock)
            if fd is not None:
                os.close(fd)

    def _usage(self, root):
        total, bundles = 0, 0
        for name in os.listdir(root):
            if name in {'store.json', '.lock'}:
                continue
            if not _BUNDLE.fullmatch(name):
                raise OSError("Unexpected storage entry")
            bundles += 1
            if bundles > self.max_bundles:
                raise HTTPException(507, "Input storage capacity is exhausted.")
            directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                _private(os.fstat(directory), directory=True)
                names = os.listdir(directory)
                if len(names) > 31:
                    raise OSError("Unexpected storage contents")
                for item in names:
                    if not _FILE.fullmatch(item):
                        raise OSError("Unexpected storage contents")
                    info = os.stat(item, dir_fd=directory, follow_symlinks=False)
                    _private(info)
                    total += info.st_size
            finally:
                os.close(directory)
        return total, bundles

    def _write(self, directory, name, data, budget):
        space = os.fstatvfs(directory)
        if budget[0] + len(data) > self.max_bytes or space.f_bavail * space.f_frsize < len(data) + self.reserve_bytes:
            raise HTTPException(507, "Input storage capacity is exhausted.")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), 0o400)
            os.fsync(file.fileno())
        budget[0] += len(data)

    def save(self, authorization, key, sources, groups, *, customer_name, customer_email, analysis_metadata=None):
        """Validate locally, persist originals, then register hold+job atomically.

        Sources are bounded streams. Groups use the existing upload form schema.
        Explicit metadata fields exclude tokens, API keys, and request headers.
        No OCR/AI call or report generation occurs here.
        """
        self.records.authorize(authorization)
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
            raise HTTPException(400, "A valid submission key is required.")
        name, email = validate_customer(customer_name, customer_email)
        if not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= 30:
            raise HTTPException(400, "Provide a bounded set of proposal pages.")
        form = FormData([('files', UploadFile(source.stream, filename=source.filename,
            headers=Headers({'content-type': source.content_type or ''}))) for source in sources]
            + [('quote_groups', json.dumps(groups))])
        ordered = quote_groups(form, QUOTE_LIMITS[authorization.tier])
        if analysis_metadata is not None:
            allowed = {'file_names', 'contractor_1_name', 'contractor_2_name', 'contractor_3_name', 'city', 'state', 'project_zip'}
            if (not isinstance(analysis_metadata, dict) or set(analysis_metadata) != allowed
                    or not isinstance(analysis_metadata['file_names'], list)
                    or len(analysis_metadata['file_names']) != len(ordered)
                    or any(len(group['pages']) != 1 for group in ordered)
                    or any(value is not None and not isinstance(value, str)
                           for key, value in analysis_metadata.items() if key != 'file_names')):
                raise HTTPException(400, 'Invalid analysis metadata.')
            for filename in analysis_metadata['file_names']:
                validate_filename(filename)
        with self._locked() as root:
            usage, count = self._usage(root)
            if count >= self.max_bundles:
                raise HTTPException(507, "Input storage capacity is exhausted.")
            budget, total, text_total, prepared_total = [usage], 0, 0, 0
            bundle = uuid.uuid4().hex
            temporary = '.tmp-' + bundle
            os.mkdir(temporary, 0o700, dir_fd=root)
            directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            manifest = {'version': 1, 'customer_name': name, 'customer_email': email, 'quotes': []}
            if analysis_metadata is not None:
                manifest['analysis_metadata'] = analysis_metadata
            seen_images = set()
            deadline = time.monotonic() + 180
            try:
                for group in ordered:
                    entries, page_count, visual_count, text_count = [], 0, 0, 0
                    native_text = []
                    for page in group['pages']:
                        if time.monotonic() >= deadline:
                            raise HTTPException(504, "Document preparation timed out. No review was started.")
                        index, rotation = page['file'], page['rotation']
                        source = sources[index]
                        suffix = validate_filename(source.filename, source.content_type)
                        content = bytearray()
                        while True:
                            chunk = source.stream.read(_CHUNK)
                            if not chunk:
                                break
                            if not isinstance(chunk, bytes) or len(chunk) > _CHUNK:
                                raise HTTPException(422, "Invalid document stream.")
                            content.extend(chunk)
                            total += len(chunk)
                            if len(content) > MAX_FILE_BYTES or total > MAX_UPLOAD_BODY - 64 * 1024:
                                raise HTTPException(413, "Document upload size limit exceeded.")
                        data = bytes(content)
                        pages = prepare_document(data, source.filename, source.content_type, rotation)
                        page_count += len(pages)
                        visual_count += sum('image' in page for page in pages)
                        text_count += sum(len(page.get('text', '')) for page in pages)
                        native_text.extend(page.get('text', '') for page in pages)
                        prepared_total += sum(len(page.get('image', '')) for page in pages)
                        if (page_count > MAX_PDF_PAGES or visual_count > MAX_VISUAL_PAGES_PER_QUOTE
                                or text_count > MAX_TEXT_CHARS or prepared_total > MAX_PREPARED_BYTES):
                            raise HTTPException(413, "Combined proposal processing limits exceeded.")
                        sha = _hash(data)
                        if suffix in IMAGE_FORMATS:
                            if sha in seen_images:
                                raise HTTPException(400, "The same photo was attached more than once.")
                            seen_images.add(sha)
                        stored_name = f'{index:04d}.bin'
                        self._write(directory, stored_name, data, budget)
                        entries.append({'file': stored_name, 'filename': source.filename,
                            'content_type': source.content_type, 'rotation': rotation,
                            'size': len(data), 'sha256': sha, 'pages': [page['page'] for page in pages]})
                    joined_text = '\n'.join(native_text)
                    if joined_text.strip():
                        validate_text(joined_text)
                    text_total += text_count
                    if text_total > MAX_TOTAL_TEXT_CHARS:
                        raise HTTPException(413, "Combined proposal text limit exceeded.")
                    manifest['quotes'].append(entries)
                encoded = _json(manifest)
                if len(encoded) > _MANIFEST_LIMIT:
                    raise HTTPException(413, "Document manifest limit exceeded.")
                self._write(directory, 'manifest.json', encoded, budget)
                os.fsync(directory)
            finally:
                os.close(directory)
            os.rename(temporary, bundle, src_dir_fd=root, dst_dir_fd=root)
            os.fsync(root)
            # Digest includes ordered groups and source metadata, never filenames
            # used as filesystem paths. No response/commit ambiguity triggers deletion.
            return self.records.register_inputs(authorization, key, 'inputs-v1:' + _hash(encoded),
                self.root_id, bundle, _hash(encoded), total, len(ordered))

    def load(self, authorization, job_id):
        """Internal replay input: authenticated ownership plus anchored integrity."""
        try:
            return self._load(authorization, job_id)
        except StoredInputError:
            # Only unconsumed, non-running jobs can release capacity. Storage
            # outages/permissions errors are NOT interpreted as invalid uploads.
            try:
                self.records.release(authorization, job_id)
            except HTTPException as error:
                if error.status_code != 409:
                    raise
            raise HTTPException(422, "Saved documents are missing or corrupted. No input can be replayed; submit corrected documents or contact support.") from None

    def _load(self, authorization, job_id):
        with self._locked() as root:
            # Serialize the retirement check with cleanup's deletion marker.
            record = self.records.inputs(authorization, job_id)
            return self._read_bundle(root, record)

    def _read_bundle(self, root, record):
        """Internal integrity check; caller holds storage lock and validates access."""
        if record['root_id'] != self.root_id or not _ID.fullmatch(record['bundle_id']):
            raise OSError("Input reference is invalid")
        try:
            directory = os.open(record['bundle_id'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except FileNotFoundError:
            raise StoredInputError from None
        try:
            _private(os.fstat(directory), directory=True)
            encoded = _read(directory, 'manifest.json', _MANIFEST_LIMIT)
            if _hash(encoded) != record['manifest_hash']:
                raise StoredInputError
            manifest = json.loads(encoded)
            files = {}
            for group in manifest['quotes']:
                for item in group:
                    if not re.fullmatch(r'\d{4}\.bin', item['file']):
                        raise OSError("Invalid input name")
                    data = _read(directory, item['file'], MAX_FILE_BYTES)
                    if len(data) != item['size'] or _hash(data) != item['sha256']:
                        raise StoredInputError
                    files[item['file']] = data
            if sum(map(len, files.values())) != record['byte_count']:
                raise StoredInputError
            return manifest, files
        except FileNotFoundError:
            raise StoredInputError from None
        finally:
            os.close(directory)

    def cleanup_orphans(self, *, older_than_seconds=86400):
        """Explicit maintenance only; never removes ANY database-referenced bundle.

        Age is not a queued-job expiry. Pending, failed, released and completed
        jobs all protect their inputs. Binding and writer lock prevent cleanup
        with an unrelated database or during filesystem/DB registration.
        """
        if older_than_seconds < 3600:
            raise ValueError("Orphan cleanup requires at least one hour of grace")
        removed = 0
        with self._locked() as root:
            self._usage(root)  # Refuse unexpected/symlinked storage contents.
            with self.records._connect() as db:
                require_reconciled(db)
                referenced = {row[0] for row in db.execute('SELECT bundle_id FROM review_inputs WHERE root_id=?', (self.root_id,))}
            for name in os.listdir(root):
                if not _BUNDLE.fullmatch(name) or name.removeprefix('.tmp-') in referenced:
                    continue
                if os.stat(name, dir_fd=root, follow_symlinks=False).st_mtime > time.time() - older_than_seconds:
                    continue
                directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                try:
                    for item in os.listdir(directory):
                        os.unlink(item, dir_fd=directory)
                    os.fsync(directory)
                finally:
                    os.close(directory)
                os.rmdir(name, dir_fd=root)
                removed += 1
            os.fsync(root)
        return removed
