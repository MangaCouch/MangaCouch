"""Windows content stamps: Python's st_ctime is creation time, not change time."""

from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from functools import cache
from pathlib import Path


class FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_int64),
        ("access_time", ctypes.c_int64),
        ("write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("attributes", ctypes.c_uint32),
    ]


@cache
def _kernel32():
    api = ctypes.WinDLL("kernel32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue]
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def file_stamp(path: Path) -> tuple[int, int, int, int, int]:
    import msvcrt

    api = _kernel32()
    # READ_ATTRIBUTES; share read/write/delete; OPEN_EXISTING. Open the reparse point
    # itself, including directories, so neither symlinks nor junctions get followed.
    handle = api.CreateFileW(str(path), 0x80, 0x7, None, 3, 0x00200000 | 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # pyright: ignore[reportAttributeAccessIssue]
    fd = None
    try:
        info = FileBasicInfo()
        if not api.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())  # pyright: ignore[reportAttributeAccessIssue]
        if info.attributes & (0x400 | 0x10):  # REPARSE_POINT | DIRECTORY
            raise OSError("Expected a regular file, not a symbolic link or special file")
        # fstat and ChangeTime must describe the same opened file, even during a rename.
        fd = msvcrt.open_osfhandle(  # pyright: ignore[reportAttributeAccessIssue]
            handle,
            os.O_RDONLY | os.O_NOINHERIT,  # pyright: ignore[reportAttributeAccessIssue]
        )
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode):
            raise OSError("Expected a regular file, not a symbolic link or special file")
        changed_ns = (info.change_time - 116444736000000000) * 100
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, changed_ns
    finally:
        if fd is None:
            api.CloseHandle(handle)
        else:
            os.close(fd)  # open_osfhandle transferred ownership of the native handle.
