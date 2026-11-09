"""Output path safety checks shared by the GUI and export engine."""
import hashlib
import json
import os
import errno
import shutil
import uuid
import tempfile
import time
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .cache import acquire_file_lease, LeaseBusy, LeaseUnavailable


@contextmanager
def _export_lock_coordinator():
    # One stable inode protects creation/removal of transient output locks.
    # Never delete this coordinator: a new inode would split ownership.
    suffix = str(os.getuid()) if hasattr(os, 'getuid') else 'user'
    root = Path(tempfile.gettempdir()) / ('slide-extractor-export-locks-' + suffix)
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise LeaseUnavailable('Unsafe export lock coordinator directory')
    deadline = time.monotonic() + 5
    while True:
        lease = acquire_file_lease(root / 'coordinator.lock')
        try:
            lease.__enter__()
            break
        except LeaseBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    try:
        yield
    finally:
        lease.__exit__(None, None, None)


@contextmanager
def export_file_lease(path):
    """Remove the output lock without allowing open/unlink inode races."""
    path = Path(path)
    lease = acquire_file_lease(path)
    with _export_lock_coordinator():
        handle = lease.__enter__()
        identity = os.fstat(handle.fileno())
    try:
        yield handle
    finally:
        try:
            with _export_lock_coordinator():
                # Close before unlink for Windows; coordinator excludes all
                # upgraded export/recovery contenders throughout this gap.
                lease.__exit__(None, None, None)
                try:
                    current = path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (not path.is_symlink() and current.st_nlink == 1 and
                            (current.st_dev, current.st_ino) ==
                            (identity.st_dev, identity.st_ino)):
                        path.unlink()
        finally:
            # Also release ownership if coordinator acquisition failed.
            if not handle.closed:
                lease.__exit__(None, None, None)


def output_lock_path(path):
    """One canonical PDF resource lock shared by both export modes."""
    path = Path(path)
    key = os.path.normcase(str(path.resolve()))
    if sys.platform == "darwin":
        # Default macOS volumes alias case and Unicode normalization.
        # Conservative extra serialization is safe on case-sensitive volumes.
        key = unicodedata.normalize("NFC", key).casefold()
    return path.parent.resolve() / ("." + hashlib.sha256(key.encode()).hexdigest() + ".lock")


def activity_lock_path(journal_path):
    return Path(journal_path).with_suffix(".active.lock")


class ExportPublishedError(RuntimeError):
    """The output was published; a subsequent confirmation failed."""
    committed = True

    def __init__(self, outputs, detail):
        self.outputs = tuple(Path(path) for path in outputs)
        super().__init__("PDF saved: " + ", ".join(map(str, self.outputs)) +
                         ". Final confirmation failed: " + str(detail) +
                         ". Check the saved file before replacing it again.")


class UnsupportedExportFilesystem(ValueError):
    """Paired export cannot preserve its exclusive publication guarantee."""


class OutputCollision(ValueError):
    """An output would overwrite a source or alias an unsafe target."""


class OutputOverwriteRequired(ValueError):
    """Replacing existing outputs requires explicit approval for both paths."""


@dataclass(frozen=True)
class ExportSnapshot:
    paths: tuple[Path, Path]
    states: tuple[tuple | None, tuple | None]


def sync_directory(path: Path) -> bool:
    """Sync a directory where supported; propagate genuine permission/I/O errors."""
    # Windows CRT cannot open directories for fsync. File data is still
    # fsynced; the transaction records reduced directory durability.
    if os.name == "nt":
        return False
    unsupported = {errno.EINVAL, errno.EBADF}
    unsupported.update(x for x in (getattr(errno, 'ENOTSUP', None),
                                  getattr(errno, 'EOPNOTSUPP', None)) if x)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in unsupported or (exc.errno == errno.EACCES and getattr(exc, 'winerror', None) == 5):
            return False
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno in unsupported:
                return False
            raise
    finally:
        os.close(fd)
    return True


def _state(path: Path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OutputCollision(f"Unsafe output target: {path}")
    if not path.exists():
        return None
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return stat.st_dev, stat.st_ino, digest.digest()


@dataclass(frozen=True)
class PdfSnapshot:
    path: Path
    state: tuple | None


def snapshot_pdf(destination: Path) -> PdfSnapshot:
    path = Path(destination).absolute().with_suffix('.pdf')
    return PdfSnapshot(path, _state(path))


def preflight_export(source: Path, destination: Path, *,
                     overwrite: bool = False) -> tuple[Path, Path]:
    pdf = Path(destination).with_suffix(".pdf")
    targets = (pdf, pdf.with_suffix(".json"))
    source = Path(source).resolve()
    for target in targets:
        # Check the leaf before resolve() hides a symlink (including dangling ones).
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise OutputCollision(f"Unsafe output target: {target}")
    targets = tuple(target.resolve() for target in targets)

    def aliases(left, right):
        return left == right or (
            left.exists() and right.exists() and left.samefile(right)
        )

    if any(aliases(source, target) for target in targets):
        raise OutputCollision("PDF and JSON outputs must differ from the source")
    if aliases(*targets):
        raise OutputCollision("PDF and JSON outputs must be distinct files")
    if not overwrite and any(target.exists() for target in targets):
        raise OutputOverwriteRequired(
            "Approve replacing the output pair:\n" + "\n".join(map(str, targets))
        )
    return targets


def snapshot_export(source: Path, destination: Path, *, overwrite=False) -> ExportSnapshot:
    paths = preflight_export(source, destination, overwrite=overwrite)
    states = tuple(_state(path) for path in paths)
    # A file may appear after preflight but before its state is captured.
    if not overwrite and any(state is not None for state in states):
        raise OutputOverwriteRequired(
            "Approve replacing the output pair:\n" + "\n".join(map(str, paths))
        )
    return ExportSnapshot(paths, states)


def validate_export_snapshot(source: Path, snapshot: ExportSnapshot) -> None:
    # Re-run collision checks on the exact paths captured for consent.
    preflight_export(source, snapshot.paths[0], overwrite=True)
    if tuple(_state(path) for path in snapshot.paths) != snapshot.states:
        raise OutputOverwriteRequired("Output paths changed during export; approve the current files")


class ExportTransaction:
    """Prepare and publish a PDF/JSON pair with a durable local journal."""

    def __init__(self, source, snapshot, validator=None, *, publication_guard=None):
        self.source = Path(source).resolve()
        self.snapshot = snapshot
        self.paths = snapshot.paths
        self.validator = validator
        self.publication_guard = publication_guard
        self.export_id = str(uuid.uuid4())
        parent = self.paths[0].parent.resolve()
        if self.paths[1].parent.resolve() != parent:
            raise OutputCollision("Output pair must share a parent")
        self.stage = parent / f".slide-export-{self.export_id}"
        self.stage.mkdir(mode=0o700)
        stage_stat = self.stage.stat(follow_symlinks=False)
        self._stage_identity = (stage_stat.st_dev, stage_stat.st_ino)
        self.journal_path = parent / f".slide-export-{self.export_id}.journal.json"
        self.lock_path = output_lock_path(self.paths[0])
        self.activity_path = activity_lock_path(self.journal_path)
        self._activity_lease = None
        self._filesystem_checked = False
        self._source_state = _state(self.source)
        self._prepared = False
        self._committed = False
        self._publication_started = False
        self._directory_sync_supported = True
        self._journal_identity = None
        self._last_journal = None

    def _begin_activity(self):
        if self._activity_lease is None:
            lease = export_file_lease(self.activity_path)
            lease.__enter__()
            self._activity_lease = lease

    def _end_activity(self):
        lease, self._activity_lease = self._activity_lease, None
        if lease is not None:
            lease.__exit__(None, None, None)

    def check_filesystem(self):
        """Reject unsupported paired publication before changing user outputs."""
        if self._filesystem_checked:
            return
        if not self._stage_owned():
            raise OutputCollision("Transaction staging directory changed")
        fd, name = tempfile.mkstemp(prefix=".hardlink-probe-", dir=self.stage)
        os.close(fd)
        probe = Path(name)
        linked = probe.with_suffix(".link")
        try:
            try:
                os.link(probe, linked, follow_symlinks=False)
            except OSError as exc:
                unsupported = {errno.EPERM, errno.EINVAL, errno.ENOSYS}
                unsupported.update(getattr(errno, name) for name in
                                   ("ENOTSUP", "EOPNOTSUPP") if hasattr(errno, name))
                if exc.errno in unsupported or getattr(exc, "winerror", None) in (1, 50):
                    raise UnsupportedExportFilesystem(
                        "This folder does not support safe PDF+JSON publication. "
                        "Use PDF-only export or choose a local folder that supports hard links.") from exc
                raise
            self._filesystem_checked = True
        finally:
            # The linked name is random and removed only when it aliases our probe.
            if linked.exists() and not linked.is_symlink() and linked.samefile(probe):
                linked.unlink()
            probe.unlink()

    def _stage_owned(self):
        try:
            stat = self.stage.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (not self.stage.is_symlink() and
                (stat.st_dev, stat.st_ino) == self._stage_identity)

    def _remove_stage(self):
        if self._stage_owned():
            shutil.rmtree(self.stage, ignore_errors=True)

    def _owned_path(self, path, *, directory=False):
        path = Path(path)
        try:
            stage_stat = self.stage.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise OutputCollision("Transaction staging directory disappeared") from exc
        if (self.stage.is_symlink() or
                (stage_stat.st_dev, stage_stat.st_ino) != self._stage_identity or
                path.parent != self.stage or path.name not in {
                    "0.new", "1.new", "0.old", "1.old", "journal.tmp"}):
            raise OutputCollision("Transaction path escaped its staging directory")
        if path.is_symlink():
            raise OutputCollision("Transaction path must not be a symlink")
        if path.exists() and (not path.is_file() or directory):
            raise OutputCollision("Unsafe transaction path")
        return path

    def _journal(self, phase):
        if not self._stage_owned():
            raise OutputCollision("Transaction staging directory changed")
        self._sync_dir(self.journal_path.parent)
        record = {
            "version": 1, "export_id": self.export_id,
            "targets": [p.name for p in self.paths],
            "staged": [f"{i}.new" for i in range(2)],
            "backups": [f"{i}.old" for i in range(2)],
            "old_exists": [v is not None for v in self.snapshot.states],
            "old_hashes": [v[2].hex() if v else None for v in self.snapshot.states],
            "new_hashes": self.new_hashes, "phase": phase,
        }
        record["directory_sync_supported"] = self._directory_sync_supported
        temp = self.journal_path.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        if self._journal_identity is None:
            if self.journal_path.exists() or self.journal_path.is_symlink():
                raise OutputCollision("Journal path already exists")
        else:
            if self.journal_path.is_symlink():
                raise OutputCollision("Journal path must not be a symlink")
            stat = self.journal_path.stat()
            if (stat.st_dev, stat.st_ino) != self._journal_identity:
                raise OutputCollision("Journal identity changed")
            try:
                current = json.loads(self.journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise OutputCollision("Journal content changed") from exc
            if current != self._last_journal:
                raise OutputCollision("Journal content changed")
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
        if self._journal_identity is None:
            os.link(temp, self.journal_path, follow_symlinks=False)
            temp.unlink()
        else:
            os.replace(temp, self.journal_path)
        stat = self.journal_path.stat()
        self._journal_identity = (stat.st_dev, stat.st_ino)
        self._last_journal = record
        self._sync_dir(self.journal_path.parent)

    def _sync_dir(self, path):
        if not sync_directory(path):
            self._directory_sync_supported = False

    def prepare(self, pdf_bytes, json_bytes, *, fail_after=None):
        self.new_hashes = [hashlib.sha256(data).hexdigest() for data in (pdf_bytes, json_bytes)]
        try:
            self._begin_activity()
            self.check_filesystem()
            for index, data in enumerate((pdf_bytes, json_bytes)):
                staged = self._owned_path(self.stage / f"{index}.new")
                fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                if fail_after == index + 1:
                    raise OSError("injected staging failure")
            if self.validator:
                self.validator(self.stage / "0.new", self.stage / "1.new")
            self._sync_dir(self.stage)
            self._journal("PREPARED")
            self._prepared = True
        except BaseException:
            self._remove_stage()
            self._end_activity()
            raise

    def commit(self):
        try:
            return self._commit()
        finally:
            self._end_activity()

    def _commit(self):
        if not self._prepared:
            raise RuntimeError("prepare must complete before commit")
        with export_file_lease(self.lock_path):
            validate_export_snapshot(self.source, self.snapshot)
            if _state(self.source) != self._source_state:
                raise OutputOverwriteRequired("Source changed during export")
            # Atomic publication begins only after protected, potentially slow
            # path/source checks. Callers may reject a changed project or cancel
            # here; after this point complete the durable commit/rollback.
            if self.publication_guard is not None:self.publication_guard()
            backups = [self.stage / f"{i}.old" for i in range(2)]
            moved = []
            published = set()
            try:
                self._publication_started = True
                self._journal("COMMITTING")
                for i, target in enumerate(self.paths):
                    if _state(target) != self.snapshot.states[i]:
                        raise OutputOverwriteRequired("Output changed before backup")
                    if self.snapshot.states[i] is not None:
                        self._owned_path(backups[i])
                        if backups[i].exists():
                            raise OutputCollision("Backup destination already exists")
                        os.replace(target, backups[i]); moved.append(i)
                        self._sync_dir(target.parent); self._sync_dir(backups[i].parent)
                        self._journal("COMMITTING")
                for i, target in enumerate(self.paths):
                    staged = self._owned_path(self.stage / f"{i}.new")
                    if target.is_symlink() or target.exists():
                        raise OutputCollision("Unknown output blocks publish")
                    if hashlib.sha256(staged.read_bytes()).hexdigest() != self.new_hashes[i]:
                        raise OutputCollision("Staged output changed")
                    # link() creates the target exclusively, so a racing external
                    # writer cannot have its file replaced between check and publish.
                    os.link(staged, target, follow_symlinks=False)
                    published.add(i)
                    staged.unlink()
                    self._sync_dir(staged.parent); self._sync_dir(target.parent)
                    self._journal("COMMITTING")
                if self.validator:
                    self.validator(*self.paths)
                if [hashlib.sha256(p.read_bytes()).hexdigest() for p in self.paths] != self.new_hashes:
                    raise OSError("Published output hash mismatch")
                self._journal("COMMITTED")
                self._committed = True
            except BaseException:
                # Verify the entire rollback set before the first destructive operation.
                for i, target in enumerate(self.paths):
                    if target.is_symlink():
                        raise OutputCollision("Unknown output blocks rollback")
                    if target.exists():
                        try:
                            digest = hashlib.sha256(target.read_bytes()).hexdigest()
                        except OSError as exc:
                            raise OutputCollision("Unknown output blocks rollback") from exc
                        old_hash = (self.snapshot.states[i][2].hex()
                                    if self.snapshot.states[i] is not None else None)
                        expected = self.new_hashes[i] if i in published else old_hash
                        if digest != expected:
                            raise OutputCollision("Unknown output blocks rollback")
                for i in moved:
                    backup = self._owned_path(backups[i])
                    if (not backup.exists() or backup.is_symlink() or
                            hashlib.sha256(backup.read_bytes()).hexdigest() != self.snapshot.states[i][2].hex()):
                        raise OutputCollision("Backup ownership could not be verified")
                for i in published:
                    target = self.paths[i]
                    if target.exists():
                        target.unlink()
                for i in reversed(moved):
                    backup = self._owned_path(backups[i])
                    target = self.paths[i]
                    os.replace(backup, target)
                    self._sync_dir(backup.parent); self._sync_dir(target.parent)
                self._sync_dir(self.paths[0].parent)
                raise

    def discard_unpublished(self):
        """Remove only this prepared job when publication never began."""
        if self._committed or self._publication_started or not self._prepared:
            return
        self._begin_activity()
        try:
            if not self._stage_owned() or self.journal_path.is_symlink():
                return
            try:
                stat = self.journal_path.stat()
                record = json.loads(self.journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if ((stat.st_dev, stat.st_ino) != self._journal_identity or
                    record != self._last_journal or record["phase"] != "PREPARED"):
                return
            for item in self.stage.iterdir():
                if (item.name not in {"0.new", "1.new"} or item.is_symlink() or
                        not item.is_file() or
                        hashlib.sha256(item.read_bytes()).hexdigest() !=
                        self.new_hashes[int(item.name[0])]):
                    return
            self._remove_stage()
            if self.stage.exists():
                return
            self.journal_path.unlink()
            self._prepared = False
        finally:
            self._end_activity()

    def close(self):
        """Release ownership only; verified recovery owns stage/journal cleanup."""
        self._end_activity()

# T008 — conservative, idempotent recovery after a process crash.
class RecoveryRequired(RuntimeError):
    """An untrusted or ambiguous on-disk state must be resolved manually."""


def _hex_hash(value):
    import re
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _read_journal(journal_path: Path, parent: Path):
    """Validate every recorded path and every hash before allowing mutations."""
    import re
    if journal_path.is_symlink() or not journal_path.is_file():
        raise RecoveryRequired(f"Unsafe journal: {journal_path}")
    try:
        record = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RecoveryRequired(f"Cannot parse journal: {journal_path}") from exc
    if not isinstance(record, dict):
        raise RecoveryRequired("Invalid journal record")
    if record.get("version") != 1 or record.get("phase") not in {"PREPARED", "COMMITTING", "COMMITTED"}:
        raise RecoveryRequired("Unknown journal version or phase")
    try:
        export_id = str(uuid.UUID(record["export_id"]))
        if export_id != record["export_id"] or journal_path.name != f".slide-export-{export_id}.journal.json":
            raise ValueError("journal export ID mismatch")
        names = record["targets"]
        if (not isinstance(names, list) or len(names) != 2 or
                any(type(n) is not str or Path(n).name != n or "/" in n or "\\" in n
                    for n in names) or
                not names[0].endswith(".pdf") or
                names[1] != names[0][:-4] + ".json"):
            raise ValueError("unsafe target names")
        if record.get("staged") != ["0.new", "1.new"] or record.get("backups") != ["0.old", "1.old"]:
            raise ValueError("invalid stage names")
        old_exists = record["old_exists"]
        old_hashes = record["old_hashes"]
        new_hashes = record["new_hashes"]
        if (type(old_exists) is not list or len(old_exists) != 2 or
                any(type(v) is not bool for v in old_exists) or
                type(old_hashes) is not list or len(old_hashes) != 2 or
                type(new_hashes) is not list or len(new_hashes) != 2 or
                any(not _hex_hash(v) for v in new_hashes) or
                any(not _hex_hash(v) if exists else v is not None
                    for v, exists in zip(old_hashes, old_exists))):
            raise ValueError("invalid journal file hashes")
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryRequired(f"Unsafe journal fields: {journal_path}") from exc
    stage = parent / f".slide-export-{export_id}"
    paths = tuple(parent / n for n in names)
    return record, stage, paths


def _recovery_file_hash(path: Path, *, allow_absent=True):
    """Never follow a leaf symlink or accept nonregular files."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RecoveryRequired(f"Unsafe recovery file: {path}")
    if not path.exists():
        if allow_absent:
            return None
        raise RecoveryRequired(f"Missing recovery file: {path}")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RecoveryRequired(f"Unreadable recovery file: {path}") from exc


def _recover_one(journal_path: Path, parent: Path, *, validator=None):
    # A live PREPARED transaction is not an interrupted transaction.
    # Acquire ownership before reading: writers update journals atomically.
    lease = export_file_lease(activity_lock_path(journal_path))
    try:
        lease.__enter__()
    except LeaseUnavailable as exc:
        raise RecoveryRequired('Unable to inspect export activity lease; check lock path and permissions') from exc
    except LeaseBusy:
        return None
    try:
        if not journal_path.exists() and not journal_path.is_symlink():
            return None  # Another recovery already completed.
        return _recover_one_locked(journal_path, parent, validator=validator)
    finally:
        lease.__exit__(None, None, None)


def _recover_one_locked(journal_path: Path, parent: Path, *, validator=None):
    record, stage, paths = _read_journal(journal_path, parent)
    with export_file_lease(output_lock_path(paths[0])):
        # Read again after the lock: do not use stale data from a racing writer.
        reread, checked_stage, checked_paths = _read_journal(journal_path, parent)
        if reread != record or checked_stage != stage or checked_paths != paths:
            raise RecoveryRequired("Journal changed during recovery")
        stage_exists = stage.exists() or stage.is_symlink()
        if stage_exists:
            if stage.is_symlink() or not stage.is_dir():
                raise RecoveryRequired("Unsafe staging directory")
            allowed = {"0.new", "1.new", "0.old", "1.old"}
            for item in stage.iterdir():
                if item.name not in allowed:
                    raise RecoveryRequired(f"Unknown staged file: {item}")
                index = int(item.name[0])
                expected = (record["new_hashes"] if item.name.endswith(".new")
                            else record["old_hashes"])[index]
                if expected is None or _recovery_file_hash(item) != expected:
                    raise RecoveryRequired(f"Staging bytes have changed: {item}")
        target_hashes = [_recovery_file_hash(p) for p in paths]
        old = record["old_hashes"]
        new = record["new_hashes"]
        if record["phase"] == "COMMITTED":
            if target_hashes != new:
                raise RecoveryRequired("COMMITTED pair changed; keeping journal and backups")
            if validator is not None:
                try:
                    validator(*paths)
                except Exception as exc:
                    raise RecoveryRequired("Committed artifact validator rejected pair") from exc
        else:
            # Verify the complete recovery set before changing any target.
            backups = [stage / f"{i}.old" for i in range(2)]
            backup_hashes = [_recovery_file_hash(b) for b in backups]
            for i, digest in enumerate(target_hashes):
                if digest not in (None, old[i], new[i]):
                    raise RecoveryRequired("Unknown target bytes, not removing")
                if record["phase"] == "PREPARED":
                    if digest != old[i] or backup_hashes[i] is not None:
                        raise RecoveryRequired("PREPARED journal has unexpected target/backup")
                if record["old_exists"][i]:
                    if old[i] is None:
                        raise RecoveryRequired("Old file hash missing")
                    if digest != old[i] and backup_hashes[i] != old[i]:
                        raise RecoveryRequired("Original cannot be restored")
                elif backup_hashes[i] is not None:
                    raise RecoveryRequired("Unexpected backup for originally absent file")
            if record["phase"] == "COMMITTING":
                for i, target in enumerate(paths):
                    digest = _recovery_file_hash(target)
                    if digest == new[i] and digest == old[i]:
                        # Old and new content are indistinguishable. Keeping the
                        # verified target is safe, even before a backup exists.
                        continue
                    if digest == new[i]:
                        # A changed output can be removed only if recoverable.
                        if old[i] is not None and backup_hashes[i] != old[i]:
                            raise RecoveryRequired("Original cannot be restored")
                        target.unlink()
                        digest = None
                    if old[i] is not None and digest is None:
                        backup = backups[i]
                        if _recovery_file_hash(backup) != old[i]:
                            raise RecoveryRequired("Verified backup disappeared")
                        # Hardlink creates exclusively: never replace an unknown file.
                        os.link(backup, target, follow_symlinks=False)
                    if _recovery_file_hash(target) != old[i]:
                        raise RecoveryRequired("Rollback did not restore original target")
            # At this point PREPARED/COMMITTING both have exactly the original pair.
            if [_recovery_file_hash(p) for p in paths] != old:
                raise RecoveryRequired("Rollback pair mismatch")
        # Remove only known private stage entries, then the journal.
        # Re-running after an interrupted cleanup is safe (subset of staged entries).
        if stage_exists:
            for item in list(stage.iterdir()):
                name = item.name
                i = int(name[0])
                expected = (new if name.endswith(".new") else old)[i]
                if _recovery_file_hash(item) != expected:
                    raise RecoveryRequired("Staged file changed during cleanup")
                item.unlink()
            stage.rmdir()
        # The journal itself must still be the same file and have the same content.
        latest, _, _ = _read_journal(journal_path, parent)
        if latest != record:
            raise RecoveryRequired("Journal changed during cleanup")
        journal_path.unlink()
        result = "committed" if record["phase"] == "COMMITTED" else "rolled_back"
        return record["export_id"], result


def recover_export(directory: Path, *, validator=None):
    """Recover only a named parent directory; never scan unrelated user files.

    Returns tuple of (export_id, status) for each journal.
    Live transactions are skipped. Raises RecoveryRequired for ambiguous
    interrupted state and leaves uncertain files intact.
    """
    parent = Path(directory).resolve()
    if parent.is_file():
        parent = parent.parent
    if not parent.is_dir():
        raise RecoveryRequired(f"Recovery parent unavailable: {parent}")
    reports = []
    for journal in sorted(parent.glob(".slide-export-*.journal.json")):
        result = _recover_one(journal, parent, validator=validator)
        if result is not None:
            reports.append(result)
    return tuple(reports)
