"""Native session termination hooks for the tray launcher."""

from __future__ import annotations

import ctypes
import importlib
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from .i18n import t


def windows_hooks(icon: Any, quit: Callable[..., Any]) -> None:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue]
    create = user32.ShutdownBlockReasonCreate
    create.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    create.restype = wintypes.BOOL
    destroy = user32.ShutdownBlockReasonDestroy
    destroy.argtypes = [wintypes.HWND]
    destroy.restype = wintypes.BOOL

    reason = t("shutdown.reason")

    def query(_wparam, _lparam):
        create(icon._hwnd, reason)
        return 1

    def end(wparam, _lparam):
        try:
            if wparam:
                # Once WM_ENDSESSION returns Windows may terminate this process immediately.
                quit().result()
        finally:
            destroy(icon._hwnd)
        return 0

    icon._message_handlers[0x0011] = query  # WM_QUERYENDSESSION
    icon._message_handlers[0x0016] = end  # WM_ENDSESSION; false means shutdown was cancelled


@lru_cache(maxsize=1)
def _macos_delegate_type() -> Any:
    foundation = importlib.import_module("Foundation")
    appkit = importlib.import_module("AppKit")
    objc = importlib.import_module("objc")

    class MangaCouchTerminationDelegate(  # pyright: ignore[reportGeneralTypeIssues]
        foundation.NSObject,
        protocols=[objc.protocolNamed("NSApplicationDelegate")],  # pyright: ignore[reportCallIssue]
    ):
        def applicationShouldTerminate_(self, application):
            self.on_quit(completed=lambda: application.replyToApplicationShouldTerminate_(True))
            return appkit.NSTerminateLater

    return MangaCouchTerminationDelegate


def macos_delegate(quit: Callable[..., Any]) -> Any:
    delegate = _macos_delegate_type().alloc().init()
    delegate.on_quit = quit
    return delegate
