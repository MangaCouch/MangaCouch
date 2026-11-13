"""The menu bar (macOS) / notification area (Windows) icon that runs and controls the server."""

from __future__ import annotations

import contextlib
import importlib
import io
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pystray

from . import autostart
from .config import CANCELLED, SAVED, library_root, load_config, read_config, save_config
from .i18n import t
from .icons import tray_icon
from .menu import SEPARATOR, after_settings, describe, menu_entries, state_label
from .service import Service, State, Status, cli_command, log_path

MACOS = sys.platform == "darwin"


def objc(name: str) -> Any:
    """A PyObjC module (macOS only); their members are generated at run time, so they stay untyped."""
    return importlib.import_module(name)


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606  # pyright: ignore[reportAttributeAccessIssue]
    else:
        subprocess.Popen(["open" if MACOS else "xdg-open", str(path)])


class Tray:
    def __init__(self) -> None:
        self.config, self.config_problem = read_config()
        self.service = Service(on_change=self._status_changed)
        # Start, stop and restart run one at a time, off the UI thread.
        self.actions = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mangacouch-action")
        self.settings_open = threading.Lock()
        self.settings_process: subprocess.Popen[bytes] | None = None
        self.open_when_running = self.config.open_browser
        self.quitting = False
        self.shutdown_future: Future | None = None
        self.shutdown_lock = threading.Lock()
        self.termination_delegate: Any = None
        self.missing_timer: threading.Timer | None = None
        self.icon = pystray.Icon(
            "MangaCouch",
            icon=tray_icon("stopped", template=MACOS),
            title="MangaCouch",
            menu=pystray.Menu(self._menu),
        )

    # UI ---------------------------------------------------------------------------------------------

    def _on_ui(self, function: Callable[[], Any]) -> None:
        """AppKit objects may only be touched on the main thread; the Windows backend accepts any."""
        if MACOS:
            objc("PyObjCTools.AppHelper").callAfter(function)
        else:
            function()

    def _refresh(self) -> None:
        status = self.service.status
        state = (
            "stopped"
            if status.state == State.STOPPING
            else "failed"
            if status.state == State.LIBRARY_MISSING
            else status.state.value
        )
        image = tray_icon(state, template=MACOS)
        title = t("status", state=state_label(status, configured=self.configured))

        def apply() -> None:
            self.icon.title = title
            if MACOS:
                self._set_macos_image(image)
            else:
                self.icon.icon = image
            self.icon.update_menu()

        self._on_ui(apply)

    def _set_macos_image(self, image: Any) -> None:
        """A Retina-sharp template image, which macOS tints for light and dark menu bars."""
        buffer = io.BytesIO()
        image.resize((36, 36)).save(buffer, "png")
        data = buffer.getvalue()
        bytes_object = objc("Foundation").NSData.dataWithBytes_length_(data, len(data))
        picture = objc("AppKit").NSImage.alloc().initWithData_(bytes_object)
        picture.setSize_((18, 18))
        picture.setTemplate_(True)
        self.icon._status_item.button().setImage_(picture)  # pyright: ignore[reportAttributeAccessIssue]

    @property
    def configured(self) -> bool:
        return self.config.database is not None and self.config.database.is_file()

    def _menu(self):
        handlers = {
            "open": self._open_web,
            "open_lan": self._open_lan,
            "start": self._start,
            "stop": lambda: self.actions.submit(self.service.stop),
            "restart": self._restart,
            "settings": self._settings,
            "books": self._open_books,
            "logs": self._open_log,
            "login": self._toggle_login,
            "quit": self._quit,
        }
        entries = menu_entries(
            self.service.status,
            configured=self.configured,
            launch_at_login=self.config.launch_at_login,
            log_exists=log_path().exists(),
        )
        for entry in entries:
            if entry is SEPARATOR:
                yield pystray.Menu.SEPARATOR
                continue
            checked = None if entry.checked is None else (lambda _, value=entry.checked: value)
            yield pystray.MenuItem(
                entry.text,
                handlers[entry.action] if entry.action else None,
                enabled=entry.enabled,
                default=entry.default,
                checked=checked,
            )

    # Actions ------------------------------------------------------------------------------------------

    def _status_changed(self, status: Status) -> None:
        # Called with the service lock held: anything slow runs on its own thread.
        if status.state == State.RUNNING and self.open_when_running:
            self.open_when_running = False
            threading.Thread(target=self._open_web, daemon=True).start()
        if status.state == State.FAILED and not self.quitting:
            self.open_when_running = False
            message = t("notify.failed", detail=describe(status))
            threading.Thread(target=self._notify, args=(message,), daemon=True).start()
        self._refresh()

    def _notify(self, message: str) -> None:
        with contextlib.suppress(Exception):  # notifications are best effort
            self.icon.notify(message, "MangaCouch")

    def _open_web(self) -> None:
        webbrowser.open(self.config.local_url)

    def _open_lan(self) -> None:
        if self.service.status.lan_url:
            webbrowser.open(self.service.status.lan_url)

    def _start(self) -> None:
        self.actions.submit(self.service.start, self.config)

    def _restart(self) -> None:
        self.actions.submit(self.service.restart, self.config)

    def _open_books(self) -> None:
        if self.config.database:
            root = library_root(self.config.database.parent)
            if root and root.is_dir():
                open_path(root)

    def _open_log(self) -> None:
        if log_path().exists():
            open_path(log_path())

    def _toggle_login(self) -> None:
        enabled = not self.config.launch_at_login
        changed = self.config.model_copy(update={"launch_at_login": enabled})
        try:
            save_config(changed)
        except OSError as error:
            self._notify(t("notify.save_failed", detail=error))
            return
        self.config = changed
        threading.Thread(target=self._apply_login, args=(enabled, True), daemon=True).start()

    def _apply_login(self, enabled: bool, undo: bool) -> None:
        """Register or remove the login item. A failure is reported, and with `undo` the saved choice
        is turned back, but it never keeps the server from starting or restarting."""
        try:
            autostart.apply(enabled)
        except Exception as error:  # registry, file system or an unsupported location
            if undo and self.config.launch_at_login == enabled:
                self.config = self.config.model_copy(update={"launch_at_login": not enabled})
                with contextlib.suppress(OSError):
                    save_config(self.config)
                self._refresh()
            self._notify(t("notify.login_failed" if enabled else "notify.login_remove_failed", detail=error))

    def _settings(self) -> None:
        threading.Thread(target=self._run_settings, name="mangacouch-settings", daemon=True).start()

    def _run_settings(self) -> None:
        if not self.settings_open.acquire(blocking=False):
            return  # already open
        was_configured = self.configured
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            command = [*cli_command(), "desktop", "--settings"]
            self.settings_process = subprocess.Popen(command, creationflags=flags)
            code = self.settings_process.wait()
        except OSError as error:
            self._notify(t("notify.settings_failed", detail=error))
            return
        finally:
            self.settings_process = None
            self.settings_open.release()
        if self.quitting:
            return
        if code != SAVED:
            if code != CANCELLED:
                self._notify(t("notify.settings_crashed", code=code))
            return
        previous, self.config = self.config, load_config()
        follow_up = after_settings(
            previous, self.config, was_configured=was_configured, state=self.service.status.state
        )
        if follow_up == "start":
            # First setup: start, then open the web app to sign in.
            self.open_when_running = True
            self._start()
        elif follow_up == "restart":
            self._restart()
        else:
            self._refresh()
        if previous.launch_at_login != self.config.launch_at_login:
            self._apply_login(self.config.launch_at_login, undo=True)

    def _finish_shutdown(self) -> None:
        with self.shutdown_lock:
            settings = self.settings_process
            if settings is not None:
                with contextlib.suppress(OSError):
                    settings.terminate()
                    try:
                        settings.wait(5)
                    except subprocess.TimeoutExpired:
                        settings.kill()
                        settings.wait()
                self.settings_process = None
            self.service.stop()

    def _quit(self, *, completed: Callable[[], None] | None = None) -> Future:
        if self.shutdown_future is not None:
            if completed:
                self.shutdown_future.add_done_callback(lambda _: self._on_ui(completed))
            return self.shutdown_future
        self.quitting = True
        if self.missing_timer is not None:
            self.missing_timer.cancel()

        def finish() -> None:
            self._finish_shutdown()

            def close() -> None:
                if completed:
                    completed()
                self.icon.stop()

            self._on_ui(close)

        self.shutdown_future = self.actions.submit(finish)
        return self.shutdown_future

    # Lifecycle ----------------------------------------------------------------------------------------

    def _ready(self, icon: Any) -> None:
        if sys.platform == "win32":
            from .shutdown import windows_hooks

            try:
                windows_hooks(icon, self._quit)
            except Exception:
                # Without them a session end may cut the server short; the tray still works.
                logging.getLogger(__name__).exception("Could not install the session end hooks")
        self._on_ui(lambda: setattr(icon, "visible", True))
        self._refresh()
        if self.config.state_dir:
            self._start()
            self._schedule_missing()
        else:
            self._settings()
        if self.config_problem:
            self._notify(t("notify.config_invalid", detail=self.config_problem))
        if self.config.launch_at_login:
            self._apply_login(True, undo=False)  # records the current executable path if the app moved

    def _schedule_missing(self) -> None:
        if self.quitting:
            return

        def retry() -> None:
            if not self.quitting and self.service.status.state == State.LIBRARY_MISSING:
                self._start()
            self._schedule_missing()

        self.missing_timer = threading.Timer(10, retry)
        self.missing_timer.daemon = True
        self.missing_timer.start()

    def run(self) -> None:
        if MACOS:
            from .shutdown import macos_delegate

            appkit = objc("AppKit")
            self.termination_delegate = macos_delegate(self._quit)
            appkit.NSApplication.sharedApplication().setDelegate_(self.termination_delegate)
            # A menu bar app: no Dock icon or application menu, also when run from source.
            appkit.NSApplication.sharedApplication().setActivationPolicy_(
                appkit.NSApplicationActivationPolicyAccessory
            )
        try:
            self.icon.run(setup=self._ready)
        finally:
            self.quitting = True
            if self.missing_timer is not None:
                self.missing_timer.cancel()
            self.actions.shutdown(wait=True, cancel_futures=True)
            self._finish_shutdown()
