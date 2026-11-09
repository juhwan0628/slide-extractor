"""Small stdlib advisory file lease shared by cache and export code."""
import contextlib
import errno
import os
import stat
import re
from pathlib import Path


class LeaseBusy(RuntimeError):
    """Lease is contended or unavailable; subclasses preserve existing guards."""


class LeaseUnavailable(LeaseBusy):
    """Unsafe lock path or inability to open/validate it, not live ownership."""


@contextlib.contextmanager
def acquire_file_lease(path):
    """Hold a non-blocking OS lock, never following a leaf symlink or hardlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            if path.is_symlink():
                raise LeaseUnavailable(f"Unsafe symlink lock: {path}")
            fd = os.open(path, flags)
        try:
            current = os.fstat(fd)
            observed = path.lstat()
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                    (current.st_dev, current.st_ino) !=
                    (observed.st_dev, observed.st_ino)):
                raise LeaseUnavailable(f"Unsafe lock inode: {path}")
        except BaseException:
            os.close(fd)
            raise
    except OSError as exc:
        raise LeaseUnavailable(f"Unable to acquire lock: {path}") from exc
    handle = os.fdopen(fd, "r+b", buffering=0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if created:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            elif current.st_size == 0:
                raise LeaseUnavailable(f"Unrecognized empty Windows lock file: {path}")
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                failure=(LeaseBusy if exc.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK)
                         else LeaseUnavailable)
                raise failure(str(path)) from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                failure=(LeaseBusy if exc.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK)
                         else LeaseUnavailable)
                raise failure(str(path)) from exc
        locked = True
        yield handle
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()

# M2 T010: Qt-free session ownership, atomic JPEG publishing and safe cleanup.
import json
import shutil
import time
import uuid

SESSION_BUDGET = 4 * 1024 ** 3
ROOT_BUDGET = 8 * 1024 ** 3
FREE_RESERVE = 1024 ** 3
COMPLETE_TTL = 24 * 3600
INCOMPLETE_TTL = 3600


class CacheBudgetExceeded(RuntimeError):
    pass


class CacheCleanupError(OSError):
    """Owned cache remains on disk; another producer must not start."""


class UnsafeCache(RuntimeError):
    pass


def _session_manifest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise UnsafeCache('Unsafe manifest')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise UnsafeCache('Unparseable manifest') from exc
    if (not isinstance(value, dict) or set(value) !=
            {'owner_uuid', 'complete', 'updated_at', 'bytes'} or
            type(value['owner_uuid']) is not str or
            str(uuid.UUID(value['owner_uuid'])) != value['owner_uuid'] or
            type(value['complete']) is not bool or
            type(value['updated_at']) not in (int, float) or
            type(value['bytes']) is not int or value['bytes'] < 0):
        raise UnsafeCache('Invalid cache ownership metadata')
    return value


def _safe_owned_dir(root, directory, expected_id=None):
    root = Path(root).resolve()
    directory = Path(directory)
    if directory.parent != root or directory.is_symlink() or not directory.is_dir():
        raise UnsafeCache('Cache directory escaped root')
    manifest = _session_manifest(directory / 'manifest.json')
    if directory.name != 'session-' + manifest['owner_uuid'] or (expected_id is not None and
            manifest['owner_uuid'] != expected_id):
        raise UnsafeCache('Session directory/owner mismatch')
    for item in directory.iterdir():
        # These are the exact publication temporaries created by write_jpeg
        # and _write_manifest, inside a verified owner session.
        temporary=(item.name=='manifest.tmp' or
                   re.fullmatch(r'[0-9]{8,}\.tmp',item.name) is not None or
                   re.fullmatch(r'skipped_[0-9]{6,}\.tmp',item.name) is not None)
        if (item.is_symlink() or not item.is_file() or
                (item.name not in ('manifest.json','lease.lock') and
                 not item.name.endswith('.jpg') and not temporary) or
                (temporary and item.stat().st_nlink!=1)):
            raise UnsafeCache('Unexpected content in session')
    return manifest


def _remove_owned_locked(root, directory):
    """Budget lock held: delete only validated files; release Windows handle last."""
    with acquire_file_lease(directory / 'lease.lock'):
        _safe_owned_dir(root, directory)
        for item in directory.iterdir():
            if item.name not in ('lease.lock', 'manifest.json'):
                item.unlink()
    # Windows requires releasing the lock handle before unlink. Keep the
    # ownership manifest until that unlink succeeds, so a denial is retryable.
    (directory / 'lease.lock').unlink()
    (directory / 'manifest.json').unlink()
    directory.rmdir()


def _prune_cache_locked(root, *, now=None, expired_only=True, exclude=()):
    clock = time.time() if now is None else now
    candidates = []
    for directory in root.iterdir():
        if directory in exclude or directory.is_symlink() or not directory.name.startswith('session-') or not directory.is_dir():
            continue
        try:
            manifest = _safe_owned_dir(root, directory)
        except (UnsafeCache, ValueError, OSError):
            continue
        ttl = COMPLETE_TTL if manifest['complete'] else INCOMPLETE_TTL
        if not expired_only or clock - manifest['updated_at'] >= ttl:
            candidates.append((manifest['updated_at'], directory))
    removed = []
    for _, directory in sorted(candidates):
        try:
            _remove_owned_locked(root, directory)
            removed.append(directory)
        except (LeaseBusy, UnsafeCache, OSError, ValueError):
            continue
    return tuple(removed)


def prune_cache(root, *, now=None):
    """Remove verified expired sessions; live project and writer pins survive."""
    root = Path(root).resolve()
    if not root.exists():
        return ()
    with _budget_lock(root):
        return _prune_cache_locked(root, now=now)


def discard_cache(root, directory):
    """Best-effort cleanup of one closed owned session, never another writer."""
    root, directory = Path(root).resolve(), Path(directory)
    try:
        with _budget_lock(root):
            _safe_owned_dir(root, directory)
            _remove_owned_locked(root, directory)
        return True
    except (LeaseBusy, UnsafeCache, OSError, ValueError):
        return False


class CachePin:
    """Shared Project lifetime owns the original writer lock without a gap."""
    def __init__(self, lease, owner=None):
        self._owner=owner
        import weakref
        self._finalizer = weakref.finalize(self, lease.__exit__, None, None, None)

    def write_lazy_jpeg(self,index,content,cancel_token=None,*,directory=None):
        if not self._finalizer.alive or self._owner is None:raise ValueError('Closed or unknown cache owner')
        if directory is not None and Path(directory)!=self._owner[1]:raise UnsafeCache('Cache owner directory mismatch')
        return _write_lazy_owned(self,index,content,cancel_token)

    def close(self):
        self._finalizer()
        self._owner=None


# Quotas are reserved in chunks by cooperating writers. The ledger is only
# runtime accounting for reproducible cache files, not a durable output journal.
RESERVATION_CHUNK = 8 * 1024 ** 2

@contextlib.contextmanager
def _budget_lock(root):
    deadline = time.monotonic() + 5
    while True:
        lease = acquire_file_lease(root / '.cache-budget.lock')
        try:
            lease.__enter__()
            break
        except LeaseBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.01)
    try:
        yield
    finally:
        lease.__exit__(None, None, None)


def _read_reservations(root):
    path = root / '.cache-reservations.json'
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise UnsafeCache('Unsafe cache reservation ledger')
    try:
        records = json.loads(path.read_text())
        if not isinstance(records, dict):raise ValueError()
        for owner, ceiling in records.items():
            if str(uuid.UUID(owner)) != owner or type(ceiling) is not int or ceiling < 0:
                raise ValueError()
    except (ValueError, OSError) as exc:
        raise UnsafeCache('Invalid cache reservation ledger') from exc
    return records


def _write_reservations(root, records):
    # Unique O_EXCL temporary file; a crashed previous writer cannot block us.
    temp = root / ('.cache-reservations-' + str(uuid.uuid4()) + '.tmp')
    try:
        with temp.open('x') as f:json.dump(records, f)
        os.replace(temp, root / '.cache-reservations.json')
    finally:
        temp.unlink(missing_ok=True)


def _reclaim_reservations(root, records, current):
    for owner in list(records):
        if owner == current:continue
        directory = root / ('session-' + owner)
        if not directory.exists():
            records.pop(owner)
            continue
        if directory.is_symlink():raise UnsafeCache('Unsafe reservation owner')
        try:
            with acquire_file_lease(directory / 'lease.lock'):
                records.pop(owner)  # Closed/crashed writer; bytes remain counted.
        except LeaseBusy:
            pass


def _root_usage(root):
    total = 0
    jpeg_bytes = {}
    for directory in root.iterdir():
        if not directory.is_dir() or directory.is_symlink():continue
        own = 0
        for file in directory.iterdir():
            if file.is_file() and not file.is_symlink():
                size = file.stat().st_size
                total += size
                if file.name.endswith('.jpg'):own += size
        jpeg_bytes[directory.name] = own
    return total, jpeg_bytes


def _write_lazy_owned(pin,index,content,cancel_token=None):
    """Grow a completed, still-pinned generation under the shared quota lock."""
    if type(index) is not int or index<0 or not isinstance(content,bytes) or not content.startswith(b'\xff\xd8') or not content.endswith(b'\xff\xd9'):
        raise ValueError('Invalid lazy JPEG')
    root,directory,owner,min_free,session_limit,root_limit,auto_prune=pin._owner
    def check():
        if cancel_token is not None:cancel_token.raise_if_cancelled()
        if not pin._finalizer.alive:raise ValueError('Closed cache owner')
    check()
    with _budget_lock(root):
        check();manifest=_safe_owned_dir(root,directory,owner)
        if not manifest['complete']:raise UnsafeCache('Lazy images require completed cache')
        path=directory/f'skipped_{index:06d}.jpg';temp=directory/f'skipped_{index:06d}.tmp'
        if path.exists() or path.is_symlink():raise UnsafeCache('Sample already exists')
        records=_read_reservations(root);_reclaim_reservations(root,records,owner)
        def accounting():
            usage,jpegs=_root_usage(root)
            outstanding=sum(max(0,ceiling-jpegs.get('session-'+key,0)) for key,ceiling in records.items())
            available=min(root_limit-usage-outstanding,shutil.disk_usage(root).free-min_free-outstanding)-256*(len(records)+1)
            return jpegs.get(directory.name,0),available
        own,available=accounting()
        if own+len(content)>session_limit:raise CacheBudgetExceeded('Insufficient session cache budget')
        if available<len(content) and auto_prune:
            _prune_cache_locked(root,expired_only=False,exclude=(directory,))
            _reclaim_reservations(root,records,owner);own,available=accounting()
        if available<len(content):raise CacheBudgetExceeded('Insufficient root cache budget or free disk reserve')
        _write_reservations(root,records)
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        published=False;manifest_created=False
        try:
            with os.fdopen(fd,'wb') as file:file.write(content)
            check();os.replace(temp,path);published=True
            state=dict(manifest,bytes=own+len(content),updated_at=time.time())
            with (directory/'manifest.tmp').open('x',encoding='utf-8') as file:
                manifest_created=True;json.dump(state,file,sort_keys=True);file.flush()
            os.replace(directory/'manifest.tmp',directory/'manifest.json')
        except BaseException:
            if published and not path.is_symlink():path.unlink(missing_ok=True)
            raise
        finally:
            if temp.exists() and not temp.is_symlink():temp.unlink()
            if manifest_created:(directory/'manifest.tmp').unlink(missing_ok=True)
        return path


class CacheSession:
    """One owner retains a lock for the lifetime of a generation."""
    def __init__(self, root, *, min_free_bytes=FREE_RESERVE,
                 session_limit=SESSION_BUDGET, root_limit=ROOT_BUDGET,
                 auto_prune=False, discard_on_error=False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.auto_prune = auto_prune
        self.discard_on_error = discard_on_error
        self._pin = None
        if auto_prune:
            prune_cache(self.root)
        self.owner_uuid = str(uuid.uuid4())
        self.directory = self.root / ('session-' + self.owner_uuid)
        self.directory.mkdir(mode=0o700)
        self._lease = acquire_file_lease(self.directory / 'lease.lock')
        self._lease.__enter__()
        self.closed = False
        self.min_free_bytes = min_free_bytes
        self.session_limit = session_limit
        self.root_limit = root_limit
        self._bytes = 0
        self._reservation = 0
        self._manifest_count = 0
        self._manifest_time = time.monotonic()
        try:
            self._write_manifest(False)
        except BaseException:
            self._lease.__exit__(None, None, None)
            self.closed = True
            raise

    def _write_manifest(self, complete):
        state = {'owner_uuid': self.owner_uuid, 'complete': complete,
                 'updated_at': time.time(), 'bytes': self._bytes}
        temp = self.directory / 'manifest.tmp'
        created = False
        try:
            with temp.open('x', encoding='utf-8') as f:
                created = True
                json.dump(state, f, sort_keys=True)
                f.flush()
            os.replace(temp, self.directory / 'manifest.json')
        finally:
            if created:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass  # Preserve the original publication error.
        self._manifest_count = 0
        self._manifest_time = time.monotonic()

    def write_jpeg(self, index, content):
        if self.closed or type(index) is not int or index < 0 or not isinstance(content, bytes):
            raise ValueError('Invalid cache session write')
        if not content.startswith(b'\xff\xd8') or not content.endswith(b'\xff\xd9'):
            raise ValueError('Expected complete JPEG data')
        name = f'{index:08d}.jpg'
        path = self.directory / name
        if path.exists() or path.is_symlink():
            raise UnsafeCache('Sample already exists')
        if self._bytes + len(content) > self.session_limit:
            raise CacheBudgetExceeded('Insufficient session cache budget')
        if shutil.disk_usage(self.root).free - len(content) < self.min_free_bytes:
            if self.auto_prune:
                with _budget_lock(self.root):
                    _prune_cache_locked(self.root, expired_only=False, exclude=(self.directory,))
            if shutil.disk_usage(self.root).free - len(content) < self.min_free_bytes:
                raise CacheBudgetExceeded('Insufficient free disk reserve; close unused projects or free disk space')
        self._reserve(len(content))
        temp = self.directory / f'{index:08d}.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(content)
            os.replace(temp, path)
            self._bytes += len(content)
            self._manifest_count += 1
            if self._manifest_count >= 32 or time.monotonic() - self._manifest_time >= 2:
                self._write_manifest(False)
        finally:
            if temp.exists() and not temp.is_symlink():
                temp.unlink()
        return path

    def _reserve(self, size):
        if self._bytes + size <= self._reservation:return
        with _budget_lock(self.root):
            records = _read_reservations(self.root)
            _reclaim_reservations(self.root, records, self.owner_uuid)
            usage, jpeg_bytes = _root_usage(self.root)
            outstanding = sum(max(0, ceiling - jpeg_bytes.get('session-' + owner, 0))
                              for owner, ceiling in records.items())
            # Reserve headroom for batched manifest byte-count/heartbeat growth.
            # The scan includes current metadata; future updates must also fit.
            owners = len(records) + (self.owner_uuid not in records)
            available = min(self.root_limit - usage - outstanding,
                            shutil.disk_usage(self.root).free - self.min_free_bytes - outstanding) - 256 * owners
            ceiling = records.get(self.owner_uuid, self._bytes)
            needed = self._bytes + size - ceiling
            if available < needed and self.auto_prune:
                _prune_cache_locked(self.root, expired_only=False, exclude=(self.directory,))
                _reclaim_reservations(self.root, records, self.owner_uuid)
                usage, jpeg_bytes = _root_usage(self.root)
                outstanding = sum(max(0, limit - jpeg_bytes.get('session-' + owner, 0))
                                  for owner, limit in records.items())
                owners = len(records) + (self.owner_uuid not in records)
                available = min(self.root_limit - usage - outstanding,
                                shutil.disk_usage(self.root).free - self.min_free_bytes - outstanding) - 256 * owners
            if available < needed:
                raise CacheBudgetExceeded('Insufficient root cache reservation; close unused projects or free disk space')
            grant = min(max(RESERVATION_CHUNK, needed), available,
                        self.session_limit - ceiling)
            records[self.owner_uuid] = ceiling + grant
            _write_reservations(self.root, records)
            self._reservation = records[self.owner_uuid]

    def complete(self):
        if self.closed:
            raise RuntimeError('Closed cache')
        _safe_owned_dir(self.root, self.directory, self.owner_uuid)
        self._write_manifest(True)

    def retain(self):
        if self.closed:
            raise RuntimeError('Closed cache')
        if self._pin is None:
            self._pin = CachePin(self._lease,(self.root,self.directory,self.owner_uuid,self.min_free_bytes,self.session_limit,self.root_limit,self.auto_prune))
        return self._pin

    def close(self):
        if not self.closed:
            try:
                with _budget_lock(self.root):
                    records = _read_reservations(self.root)
                    records.pop(self.owner_uuid, None)
                    _write_reservations(self.root, records)
            finally:
                if self._pin is None:
                    self._lease.__exit__(None, None, None)
                self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, original, traceback):
        try:
            self.close()
        except (LeaseBusy, UnsafeCache, OSError, ValueError):
            if exc_type is None:
                raise
        if exc_type is not None and self.discard_on_error:
            if self._pin is not None:
                self._pin.close()
            if not discard_cache(self.root, self.directory) and self.directory.exists():
                # Preserve the original failure for existing callers while
                # exposing the cleanup barrier to background successors.
                error = CacheCleanupError(f'Unable to remove sample cache: {self.directory}')
                original.cache_cleanup_error = error
