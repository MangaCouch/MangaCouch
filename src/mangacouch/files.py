from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from .database import Library
from .limits import check_cancel

ROOT_MARKER = ".mangacouch-root.json"
STAGING = ".mangacouch-staging"
CHUNK_SIZE = 1024 * 1024
SPACE_MARGIN = 8 * 1024 * 1024


_thread = threading.local()


def set_thread_cancel(cancelled: Callable[[], bool] | None) -> None:
    _thread.cancelled = cancelled


def thread_cancelled() -> Callable[[], bool] | None:
    return getattr(_thread, "cancelled", None)


def require_free_space(path: Path, additional_bytes: int) -> None:
    if shutil.disk_usage(path).free < additional_bytes + SPACE_MARGIN:
        raise OSError(errno.ENOSPC, "Insufficient disk space")


class RootUnavailable(OSError):
    pass


class ContentChanged(OSError):
    def __init__(self, message: str, *, observed: bool = False):
        super().__init__(message)
        self.observed = observed


def sync_directory(path: Path) -> None:
    """Make renames and new names in `path` durable. Some network and FUSE filesystems cannot
    sync a directory; like SQLite, carry on there: they persist names on their own terms."""
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        except OSError as error:
            if error.errno not in UNSUPPORTED | {errno.EBADF, errno.EACCES}:
                raise
        finally:
            os.close(fd)


def sync_file(path: Path) -> None:
    """Flush an already written file to disk. Windows flushes only through a handle with write
    access (a read-only one fails with EBADF); a file Windows will not open for writing (read-only
    attribute) was written by someone else, so carry on without it."""
    try:
        with path.open("r+b" if os.name == "nt" else "rb") as stream:
            os.fsync(stream.fileno())
    except PermissionError:
        if os.name != "nt":
            raise


UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}
"""What a filesystem answers to an operation it does not implement (FUSE, virtiofs, network shares)."""
LINK_UNSUPPORTED = UNSUPPORTED | {errno.EXDEV, errno.EPERM, errno.EACCES, errno.EMLINK}


def _rename_exclusive(source: Path, target: Path) -> int:
    """The kernel's no-replace rename; 0 on success, else the errno."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    else:
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(target), 1)  # AT_FDCWD, RENAME_NOREPLACE
    rename.restype = ctypes.c_int
    return ctypes.get_errno() if rename(*args) else 0


def _rename_new(source: Path, target: Path, *, claimed: Callable[[Path], None] | None = None) -> None:
    """Atomically publish a complete file without replacing an existing destination.

    Filesystems without a no-replace rename (virtiofs and other FUSE mounts, NFS, SMB) get a hard
    link instead, which also fails on an existing target. Without hard links either, the target
    name is claimed with an exclusive create and bytes are copied through that descriptor.
    A caller may persist the claim identity before copying so interrupted partial files can be cleaned."""
    if os.name == "nt":
        os.rename(source, target)
        return
    code = _rename_exclusive(source, target)
    if not code:
        return
    if code not in UNSUPPORTED:
        raise OSError(code, os.strerror(code), str(target))
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as error:
        if error.errno not in LINK_UNSUPPORTED:
            raise
        _replace_claim(source, target, claimed=claimed)
        return
    os.unlink(source)


def _replace_claim(source: Path, target: Path, *, claimed: Callable[[Path], None] | None = None) -> None:
    """Copy into an exclusively opened destination without replacing any directory entry.

    A failed copy leaves an unreferenced partial file for import repair; never unlink a name
    another process could have replaced. The original source remains until the copy succeeds.
    """
    before = live_stamp(source)
    mode = stat.S_IMODE(source.stat().st_mode)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
        claim = os.fstat(output.fileno())
        if claimed is not None:
            claimed(target)
        while chunk := incoming.read(CHUNK_SIZE):
            check_cancel(thread_cancelled())
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
        current = target.stat()
        if (current.st_dev, current.st_ino) != (claim.st_dev, claim.st_ino):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
        if live_stamp(source) != before:
            raise ContentChanged("Source changed during publication")
    source.unlink()


def set_aside(path: Path, suffix: str = ".changed") -> Path:
    """Rename `path` to `<name><suffix>`, or `<name>.<n><suffix>` when taken; nothing is replaced."""
    for number in range(1, 1000):
        target = path.with_name(path.name + (suffix if number == 1 else f".{number}{suffix}"))
        try:
            _rename_new(path, target)
        except FileExistsError:
            continue
        sync_directory(path.parent)
        return target
    raise FileExistsError(errno.EEXIST, "No free name to set the file aside", str(path))


def atomic_write(path: Path, data: bytes, *, mode: int | None = None, replace: bool = True) -> None:
    """Publish `data` at `path` complete or not at all, durably once this returns.

    The file gets `mode`, or else the permissions any new file gets under the process umask.
    With replace=False an existing file is never overwritten (FileExistsError).
    """
    temporary = path.with_name(f".mc-{uuid.uuid4().hex[:12]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            if mode is not None and os.name != "nt":
                os.fchmod(stream.fileno(), mode)  # exactly `mode`, whatever the umask
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            _rename_new(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    sync_directory(path.parent)


class FileLock:
    """An exclusive lock on `path` (created when missing), held until close().

    The lock belongs to this handle: threads of one process exclude each other like processes do.
    Raises BlockingIOError when `blocking` is False and another handle holds the lock.
    """

    def __init__(self, path: Path, *, blocking: bool = True, cancelled: Callable[[], bool] | None = None):
        self._handle = path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                if self._handle.seek(0, os.SEEK_END) == 0:
                    self._handle.write(b"\0")  # msvcrt locks a byte range, which must exist
                    self._handle.flush()
                self._handle.seek(0)
                while True:
                    check_cancel(cancelled)
                    try:
                        msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise
                        if not blocking:
                            raise BlockingIOError(errno.EAGAIN, "Lock is already held") from error
                        time.sleep(0.1)
            else:
                import fcntl

                while True:
                    check_cancel(cancelled)
                    try:
                        fcntl.flock(
                            self._handle.fileno(),
                            fcntl.LOCK_EX | (0 if blocking and cancelled is None else fcntl.LOCK_NB),
                        )
                        break
                    except BlockingIOError:
                        if not blocking:
                            raise
                        time.sleep(0.1)
        except BaseException:
            self._handle.close()
            raise

    def close(self) -> None:
        if self._handle.closed:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()

    def __enter__(self) -> FileLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def publish_file(source: Path, target: Path, *, claimed: Callable[[Path], None] | None = None) -> None:
    """Link when possible; otherwise publish a verified copy, keeping the source.

    The copy gets the source's permission bits, as a link would share them: callers choose
    them on the source (an archive follows the umask; keys, databases and snapshots are 0600).
    `claimed(path)` receives each file whose identity the target is about to take, before the
    target name can appear, so a caller can later recognise a target it published itself.
    """
    check_cancel(thread_cancelled())
    before = live_stamp(source)
    mode = stat.S_IMODE(source.lstat().st_mode)
    try:
        if claimed is not None:
            claimed(source)
        os.link(source, target, follow_symlinks=False)
        return
    except OSError as error:
        if error.errno not in LINK_UNSUPPORTED:
            raise
    require_free_space(target.parent, before[2])
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".mangacouch-publish-")
    temporary = Path(name)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as output, source.open("rb") as input:
            while chunk := input.read(CHUNK_SIZE):
                check_cancel(thread_cancelled())
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if live_stamp(source) != before or hash_file(temporary) != digest.hexdigest():
            raise ContentChanged("Source changed or copy verification failed during publication")
        if os.name != "nt":  # Windows only has a read-only flag, which must not block cleanup.
            os.chmod(temporary, mode)
        if claimed is not None:
            claimed(temporary)
        _rename_new(temporary, target, claimed=claimed)
    finally:
        temporary.unlink(missing_ok=True)


def live_stamp(path: Path) -> tuple[int, int, int, int]:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise OSError("Expected a regular file, not a symbolic link or special file")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def file_stamp(path: Path) -> tuple[int, int]:
    value = live_stamp(path)
    return value[2], value[3]


def hash_file(path: Path, cancelled: Callable[[], bool] | None = None) -> str:
    cancelled = cancelled or thread_cancelled()
    check_cancel(cancelled)
    before = live_stamp(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            check_cancel(cancelled)
            digest.update(chunk)
    if live_stamp(path) != before:
        raise ContentChanged("File changed while being read")
    return digest.hexdigest()


CHECK_TTL = 2.0


def check_root(path: Path, root_id: str, *, writable: bool = False) -> tuple[int, int, int, int]:
    """Verify the root identity; returns the marker's stamp for cheap revalidation."""
    try:
        if path.is_symlink() or path.is_junction() or not path.is_dir():
            raise RootUnavailable("Library root is missing or is a symbolic link")
        marker = path / ROOT_MARKER
        stamp = live_stamp(marker)
        with marker.open("rb") as stream:
            data = json.loads(stream.read(4097))
        if data != {"schema": "mangacouch.root/1", "id": root_id}:
            raise RootUnavailable("Library root identity does not match the database")
        with os.scandir(path) as entries:
            next(entries, None)
        if writable:
            with tempfile.TemporaryFile(dir=path):
                pass
        return stamp
    except RootUnavailable:
        raise
    except (OSError, ValueError) as error:
        raise RootUnavailable("Library root unavailable; repair the path, mount or permissions") from error


class LibraryRoot:
    def __init__(self, library: Library, path: Path):
        self.library = library
        self.path = path.absolute()
        with library.connection() as db:
            row = db.execute("SELECT root_id FROM library_root WHERE singleton=1").fetchone()
        if row is None:
            raise RootUnavailable("Library has no bound file root; initialize it explicitly")
        self.root_id: str = row["root_id"]
        self._verified: tuple[float, tuple[int, int, int, int]] | None = None

    @classmethod
    def bind(cls, library: Library, path: Path) -> LibraryRoot:
        path = path.absolute()
        if not path.is_dir() or path.is_symlink():
            raise RootUnavailable("Initialize an existing ordinary directory")
        root_id = str(uuid.uuid4())
        marker = path / ROOT_MARKER
        owned = None
        try:
            with library.transaction() as db:
                if (
                    db.execute("SELECT 1 FROM library_root").fetchone()
                    or db.execute("SELECT 1 FROM book").fetchone()
                ):
                    raise ValueError("Root binding requires a new, empty database")
                with marker.open("xb") as stream:
                    info = os.fstat(stream.fileno())
                    owned = (info.st_dev, info.st_ino)
                    stream.write(json.dumps({"schema": "mangacouch.root/1", "id": root_id}).encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_directory(path)
                db.execute("INSERT INTO library_root(singleton,root_id) VALUES(1,?)", (root_id,))
                check_root(path, root_id, writable=True)
        except BaseException:
            if owned is not None and marker.exists():
                info = marker.lstat()
                if (info.st_dev, info.st_ino) == owned:
                    marker.unlink()
                    sync_directory(path)
            raise
        root = cls(library, path)
        return root

    def check(self, *, writable: bool = False) -> None:
        """Raise RootUnavailable unless the bound root is present.

        A successful full check is reused for CHECK_TTL seconds as long as the
        marker file still has the same stamp (one lstat). The writable probe
        (a temporary file) only runs when writable=True, i.e. for real file
        mutations; failures are never cached.
        """
        verified = self._verified
        if not writable and verified is not None and time.monotonic() - verified[0] < CHECK_TTL:
            try:
                if live_stamp(self.path / ROOT_MARKER) == verified[1]:
                    return
            except OSError:
                pass
        self._verified = None
        stamp = check_root(self.path, self.root_id, writable=writable)
        self._verified = (time.monotonic(), stamp)

    def resolve(self, relative: str) -> Path:
        if (
            not relative
            or PurePosixPath(relative).is_absolute()
            or PureWindowsPath(relative).drive
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError("Expected a safe relative library path")
        current = self.path
        for part in relative.split("/"):
            current /= part
            if current.is_symlink() or current.is_junction():
                raise OSError("Library path contains a symbolic link")
        return current

    @contextmanager
    def mutation(
        self,
        *,
        lock_name: Literal[
            ".mangacouch-operation.lock", ".mangacouch-scan.lock"
        ] = ".mangacouch-operation.lock",
        blocking: bool = True,
        cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[None]:
        cancelled = cancelled or thread_cancelled()
        self.check(writable=True)
        with FileLock(self.resolve(lock_name), blocking=blocking, cancelled=cancelled):
            check_cancel(cancelled)
            self.check()
            yield
