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
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from .archives import check_cancel
from .database import Library

ROOT_MARKER = ".mangacouch-root.json"
STAGING = ".mangacouch-staging"
CHUNK_SIZE = 1024 * 1024
SPACE_MARGIN = 8 * 1024 * 1024


def require_free_space(path: Path, additional_bytes: int) -> None:
    if shutil.disk_usage(path).free < additional_bytes + SPACE_MARGIN:
        raise OSError(errno.ENOSPC, "Insufficient disk space")


class RootUnavailable(OSError):
    pass


class ContentChanged(OSError):
    pass


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _rename_new(source: Path, target: Path) -> None:
    """Atomically publish a complete file without replacing an existing destination."""
    if os.name == "nt":
        os.rename(source, target)
        return
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
    if rename(*args):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def publish_file(source: Path, target: Path) -> None:
    """Link when possible; otherwise publish a verified copy, keeping the source."""
    before = file_stamp(source)
    try:
        os.link(source, target, follow_symlinks=False)
        return
    except OSError as error:
        if error.errno not in {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EINVAL,
            errno.EMLINK,
        }:
            raise
    require_free_space(target.parent, before[2])
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".mangacouch-publish-")
    temporary = Path(name)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as output, source.open("rb") as input:
            while chunk := input.read(CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if file_stamp(source) != before or hash_file(temporary) != digest.hexdigest():
            raise ContentChanged("Source changed or copy verification failed during publication")
        _rename_new(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def file_stamp(path: Path) -> tuple[int, int, int, int]:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise OSError("Expected a regular file, not a symbolic link or special file")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def hash_file(path: Path, cancelled: Callable[[], bool] | None = None) -> str:
    before = file_stamp(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            check_cancel(cancelled)
            digest.update(chunk)
    if file_stamp(path) != before:
        raise ContentChanged("File changed while being read")
    return digest.hexdigest()


CHECK_TTL = 2.0


def _check_root(path: Path, root_id: str, *, writable: bool = False) -> tuple[int, int, int, int]:
    """Verify the root identity; returns the marker's stamp for cheap revalidation."""
    try:
        if path.is_symlink() or path.is_junction() or not path.is_dir():
            raise RootUnavailable("Library root is missing or is a symbolic link")
        marker = path / ROOT_MARKER
        stamp = file_stamp(marker)
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
                _check_root(path, root_id, writable=True)
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
                if file_stamp(self.path / ROOT_MARKER) == verified[1]:
                    return
            except OSError:
                pass
        self._verified = None
        stamp = _check_root(self.path, self.root_id, writable=writable)
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
    ) -> Iterator[None]:
        self.check(writable=True)
        lock_path = self.resolve(lock_name)
        with lock_path.open("a+b") as lock:
            if sys.platform == "win32":
                import msvcrt

                if lock.seek(0, 2) == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                try:
                    while True:
                        try:
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError as error:
                            if not blocking or error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                                raise
                            time.sleep(0.1)
                except OSError as error:
                    if not blocking and error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise BlockingIOError(errno.EAGAIN, "Library lock is already held") from error
                    raise
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                self.check()
                yield
            finally:
                if sys.platform == "win32":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
