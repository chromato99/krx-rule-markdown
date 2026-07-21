from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import ctypes
import fcntl
import json
import os
import shutil
import socket
import tempfile
from typing import Any, Callable, TypeVar


class WriterLockError(RuntimeError):
    pass


class CorpusMutationError(RuntimeError):
    pass


AT_FDCWD = -100
RENAME_EXCHANGE = 2
T = TypeVar("T")
MAX_STAGED_FILE_BYTES = 64 * 1024 * 1024
MAX_STAGED_CORPUS_BYTES = 2 * 1024 * 1024 * 1024
MAX_STAGED_ENTRIES = 100_000


@dataclass
class WriterLock(AbstractContextManager["WriterLock"]):
    data_dir: Path
    operation: str
    path: Path | None = None
    acquired: bool = False
    fd: int | None = None

    def __enter__(self) -> "WriterLock":
        self.data_dir = Path(self.data_dir)
        self.path = self.data_dir.parent / f".{self.data_dir.name}.writer.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "operation": self.operation,
        }
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                owner = lock_owner(self.path)
                raise WriterLockError(
                    f"corpus writer is already active at {self.path}"
                    + (f" ({owner})" if owner else "")
                ) from exc
            except OSError as exc:
                raise WriterLockError(f"filesystem advisory lock failed at {self.path}: {exc}") from exc
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        fsync_directory(self.path.parent)
        self.fd = fd
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.acquired and self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
            self.fd = None
            self.acquired = False


def lock_owner(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    operation = str(payload.get("operation") or "unknown")
    pid = str(payload.get("pid") or "?")
    host = str(payload.get("host") or "?")
    return f"operation={operation} pid={pid} host={host}"


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_run_report(
    data_dir: Path,
    operation: str,
    records: list[dict[str, Any]],
    result: int | str,
    *,
    error: str = "",
    finished_at: str = "",
) -> None:
    """Write operational provenance outside the immutable corpus generation."""

    normalized_records = []
    for record in records:
        normalized = dict(record)
        if not normalized.get("operation"):
            normalized["operation"] = operation
        normalized_records.append(normalized)
    if isinstance(result, str):
        result_label = result
    else:
        result_label = "ok" if result == 0 else "failed"
    if not finished_at:
        finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report_dir = Path(data_dir).parent / ".krx-rule-runs"
    atomic_write_json(
        report_dir / "latest.json",
        {
            "operation": operation,
            "finished_at": finished_at,
            "result": result_label,
            "documents": normalized_records,
            **({"error": error} if error else {}),
        },
    )


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_exchange_paths(left: Path, right: Path) -> None:
    """Atomically exchange two paths on Linux, or fail without mutating them."""

    left = Path(left)
    right = Path(right)
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise WriterLockError("atomic directory exchange requires Linux renameat2")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(left),
        AT_FDCWD,
        os.fsencode(right),
        RENAME_EXCHANGE,
    )
    if result != 0:
        errno = ctypes.get_errno()
        raise WriterLockError(f"atomic directory exchange failed: {os.strerror(errno)}")
    fsync_directory(left.parent)


def create_staged_corpus(data_dir: Path) -> Path:
    data_dir = Path(data_dir)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{data_dir.name}.staging-", dir=data_dir.parent))
    try:
        if data_dir.exists():
            validate_corpus_copy_source(data_dir)
            # Preserve links defensively even though the no-follow preflight
            # rejects them. Following a raced link here could otherwise import
            # bytes from outside the corpus before staged validation runs.
            shutil.copytree(
                data_dir,
                staging,
                dirs_exist_ok=True,
                copy_function=shutil.copy2,
                symlinks=True,
            )
            reject_tree_symlinks(staging)
        return staging
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_corpus_copy_source(root: Path) -> None:
    """Reject links/special files and bound a staging copy without following links."""

    root = Path(root)
    if root.is_symlink():
        raise CorpusMutationError(f"active corpus root must not be a symlink: {root}")
    total_bytes = 0
    entry_count = 0
    try:
        for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for name in [*directory_names, *file_names]:
                path = current_path / name
                info = path.lstat()
                entry_count += 1
                if entry_count > MAX_STAGED_ENTRIES:
                    raise CorpusMutationError(
                        f"active corpus exceeds {MAX_STAGED_ENTRIES} filesystem entries"
                    )
                if path.is_symlink():
                    raise CorpusMutationError(f"active corpus contains a symlink: {path}")
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise CorpusMutationError(f"active corpus contains a special file: {path}")
                if info.st_size > MAX_STAGED_FILE_BYTES:
                    raise CorpusMutationError(
                        f"active corpus file exceeds {MAX_STAGED_FILE_BYTES} bytes: {path}"
                    )
                total_bytes += info.st_size
                if total_bytes > MAX_STAGED_CORPUS_BYTES:
                    raise CorpusMutationError(
                        f"active corpus exceeds {MAX_STAGED_CORPUS_BYTES} total bytes"
                    )
    except OSError as exc:
        raise CorpusMutationError(f"active corpus preflight failed: {exc}") from exc


def reject_tree_symlinks(root: Path) -> None:
    """Defense-in-depth check for links introduced by a copy race."""

    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in [*directory_names, *file_names]:
            path = current_path / name
            if path.is_symlink():
                raise CorpusMutationError(f"staged corpus contains a symlink: {path}")


def publish_staged_corpus(data_dir: Path, staging: Path) -> None:
    data_dir = Path(data_dir)
    if not data_dir.exists():
        os.replace(staging, data_dir)
        fsync_directory(data_dir.parent)
        return
    atomic_exchange_paths(data_dir, staging)
    # staging now names the previous complete release. An interruption before
    # cleanup leaves only an orphan; the active path already names the new one.
    shutil.rmtree(staging)


def mutate_staged_corpus(
    data_dir: Path,
    operation_name: str,
    operation: Callable[[Path], T],
) -> T:
    """Run one corpus mutation against a sibling generation and publish it.

    The validator import is deliberately local: validation depends on the
    repository helpers for atomic I/O.
    """

    from .validate import validate_data

    data_dir = Path(data_dir)
    with WriterLock(data_dir, operation_name):
        staging: Path | None = create_staged_corpus(data_dir)
        try:
            result = operation(staging)
            errors = validate_data(staging, release_mode=True)
            if errors:
                preview = "\n".join(errors[:20])
                suffix = f"\n... and {len(errors) - 20} more" if len(errors) > 20 else ""
                raise CorpusMutationError(
                    f"staged corpus failed validation with {len(errors)} error(s)\n{preview}{suffix}"
                )
            publish_staged_corpus(data_dir, staging)
            staging = None
            return result
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging)
