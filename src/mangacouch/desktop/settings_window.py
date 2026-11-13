"""The settings window, which is also the first-run setup. It runs in its own process (`desktop --settings`)
so Tk and the tray each own a main thread; exit code 0 means the settings were saved."""

from __future__ import annotations

import importlib
import sqlite3
import sys
import tkinter as tk
from contextlib import closing
from pathlib import Path
from tkinter import filedialog, font, ttk
from typing import Any

from pydantic import ValidationError

from .config import (
    CANCELLED,
    DATABASE_NAME,
    SAVED,
    DesktopConfig,
    default_library_directory,
    find_state_directory,
    library_root,
)
from .i18n import t

HINT_COLOR = "gray50"


def passcode_configured(database: Path) -> bool:
    """Read-only, as it runs while a path is typed: opening the library would upgrade an older one."""
    from ..database import database_uri

    try:
        with closing(sqlite3.connect(database_uri(database.absolute(), "ro"), uri=True, timeout=1)) as db:
            return db.execute("SELECT 1 FROM auth_settings").fetchone() is not None
    except Exception:  # an unreadable library is reported by serve; ask for nothing here
        return True


def create_library(state_dir: Path, books_dir: Path, passcode: str | None) -> None:
    """Initialize a new library (when there is none) and set its passcode (when given)."""
    from ..archives import Limits
    from ..auth import Auth
    from ..lifecycle import initialize_library, open_library

    database = state_dir / DATABASE_NAME
    if not database.is_file():
        state_dir.mkdir(parents=True, exist_ok=True)
        books_dir.mkdir(parents=True, exist_ok=True)
        initialize_library(database, books_dir, Limits())
    if passcode is not None:
        Auth(open_library(database)).initialize(passcode)


def parse_hosts(text: str) -> list[str]:
    return [host.strip().lower() for host in text.replace("\n", ",").split(",") if host.strip()]


def validation_message(error: ValidationError) -> str:
    return "; ".join(str(item["msg"]).removeprefix("Value error, ") for item in error.errors())


class SettingsWindow:
    def __init__(self, config: DesktopConfig):
        self.config = config
        self.result = CANCELLED
        self.setup = not (config.database and config.database.is_file())
        self.root = tk.Tk()
        self.root.title(t("settings.setup_title" if self.setup else "settings.title"))
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self._set_icon()

        default = default_library_directory()
        self.state_dir = tk.StringVar(value=config.state_dir or str(default / "state"))
        self.books_dir = tk.StringVar(value=str(default / "books"))
        self.passcode = tk.StringVar()
        self.passcode_repeat = tk.StringVar()
        self.port = tk.StringVar(value=str(config.port))
        self.lan = tk.BooleanVar(value=config.lan)
        self.origin = tk.StringVar(value=config.public_origin)
        self.hosts = tk.StringVar(value=", ".join(config.extra_hosts))
        self.proxies = tk.StringVar(value=", ".join(config.trusted_proxies))
        self.cache = tk.StringVar(value=str(config.cache_gib))
        self.upload = tk.StringVar(value=str(config.upload_gib))
        self.login = tk.BooleanVar(value=config.launch_at_login)
        self.open_browser = tk.BooleanVar(value=config.open_browser)
        self.library_note = tk.StringVar()
        self.error = tk.StringVar()

        self._build()
        self.state_dir.trace_add("write", lambda *_: self._refresh_library())
        self._refresh_library()
        self.root.bind("<Return>", lambda _: self.save())
        self.root.bind("<Escape>", lambda _: self.root.destroy())

    def _set_icon(self) -> None:
        try:
            from PIL import ImageTk

            from .icons import logo

            self._icon = ImageTk.PhotoImage(logo(64))
            self.root.iconphoto(True, self._icon)  # pyright: ignore[reportArgumentType]
        except Exception:  # cosmetic only
            pass

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)
        heading = font.nametofont("TkDefaultFont").copy()
        heading.configure(weight="bold")
        row = 0

        def section(key: str) -> None:
            nonlocal row
            ttk.Label(frame, text=t(key), font=heading).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(12 if row else 0, 4)
            )
            row += 1

        def entry(label: str, variable: tk.Variable, *, width: int = 36, show: str = "") -> ttk.Entry:
            nonlocal row
            ttk.Label(frame, text=t(label)).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
            widget = ttk.Entry(frame, textvariable=variable, width=width, show=show)
            widget.grid(row=row, column=1, sticky="we", pady=2)
            row += 1
            return widget

        def note(text: str | tk.StringVar) -> None:
            nonlocal row
            label = ttk.Label(frame, wraplength=420, foreground=HINT_COLOR)
            if isinstance(text, tk.StringVar):
                label.configure(textvariable=text)
            else:
                label.configure(text=text)
            label.grid(row=row, column=1, columnspan=2, sticky="w")
            row += 1

        def folder(label: str, variable: tk.StringVar) -> tuple[ttk.Entry, ttk.Button]:
            widget = entry(label, variable)
            button = ttk.Button(frame, text=t("settings.browse"), command=lambda: self._choose(variable))
            button.grid(row=row - 1, column=2, padx=(8, 0))
            return widget, button

        def check(label: str, variable: tk.BooleanVar) -> None:
            nonlocal row
            ttk.Checkbutton(frame, text=t(label), variable=variable).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=2
            )
            row += 1

        section("settings.library")
        folder("settings.state_dir", self.state_dir)
        note(t("settings.state_hint"))
        note(self.library_note)
        books_row = row
        folder("settings.books_dir", self.books_dir)
        passcode_row = row
        self.passcode_entry = entry("settings.passcode", self.passcode, show="•")
        entry("settings.passcode_repeat", self.passcode_repeat, show="•")
        note(t("settings.passcode_hint"))
        # Rows only shown when a new library needs them; hidden (not disabled) otherwise.
        self.books_rows = frame.grid_slaves(row=books_row)
        self.passcode_rows = [
            widget for at in range(passcode_row, row) for widget in frame.grid_slaves(row=at)
        ]

        section("settings.network")
        entry("settings.port", self.port, width=8).grid_configure(sticky="w")
        check("settings.lan", self.lan)
        advanced_button = ttk.Button(frame)
        advanced_button.grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1
        advanced_row = row
        entry("settings.proxies", self.proxies)
        note(t("settings.proxies_hint"))
        entry("settings.origin", self.origin)
        note(t("settings.origin_hint"))
        entry("settings.hosts", self.hosts)
        note(t("settings.hosts_hint"))
        advanced_widgets = [widget for at in range(advanced_row, row) for widget in frame.grid_slaves(row=at)]
        advanced_open = False

        def toggle_advanced() -> None:
            nonlocal advanced_open
            advanced_open = not advanced_open
            advanced_button.configure(text=("▾ " if advanced_open else "▸ ") + t("settings.advanced"))
            for widget in advanced_widgets:
                widget.grid() if advanced_open else widget.grid_remove()

        advanced_button.configure(text="▸ " + t("settings.advanced"), command=toggle_advanced)
        for widget in advanced_widgets:
            widget.grid_remove()

        section("settings.storage")
        entry("settings.cache", self.cache, width=8).grid_configure(sticky="w")
        entry("settings.upload", self.upload, width=8).grid_configure(sticky="w")

        section("settings.desktop")
        check("settings.login", self.login)
        check("settings.open_browser", self.open_browser)

        ttk.Label(frame, textvariable=self.error, foreground="#c62828", wraplength=480).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(12, 0)
        )
        row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e", pady=(12, 0))
        if not self.setup:
            ttk.Label(buttons, text=t("settings.restart_note"), foreground=HINT_COLOR).pack(
                side="left", padx=(0, 12)
            )
        ttk.Button(buttons, text=t("settings.cancel"), command=self.root.destroy).pack(side="left")
        ttk.Button(
            buttons,
            text=t("settings.create" if self.setup else "settings.save"),
            command=self.save,
            default="active",
        ).pack(side="left", padx=(8, 0))

    def _choose(self, variable: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(parent=self.root, initialdir=variable.get() or str(Path.home()))
        if chosen:
            path = Path(chosen)
            variable.set(str(find_state_directory(path) if variable is self.state_dir else path))

    def _refresh_library(self) -> None:
        text = self.state_dir.get().strip()
        database = Path(text).expanduser() / DATABASE_NAME if text else None
        exists = database is not None and database.is_file()
        if database is not None and exists:
            self.library_note.set(t("settings.existing", root=library_root(database.parent) or "?"))
            self.needs_passcode = not passcode_configured(database)
        else:
            self.library_note.set(t("settings.new"))
            self.needs_passcode = True
        for widgets, shown in ((self.books_rows, not exists), (self.passcode_rows, self.needs_passcode)):
            for widget in widgets:
                widget.grid() if shown else widget.grid_remove()

    def _integer(self, variable: tk.StringVar, label: str) -> int:
        try:
            return int(variable.get().strip())
        except ValueError:
            raise ValueError(t("error.number", field=t(label))) from None

    def save(self) -> None:
        self.error.set("")
        try:
            text = self.state_dir.get().strip()
            if not text:
                raise ValueError(t("error.state_dir"))
            state_dir = Path(text).expanduser().absolute()
            new_library = not (state_dir / DATABASE_NAME).is_file()
            books_dir = Path(self.books_dir.get().strip()).expanduser().absolute()
            if new_library:
                if not self.books_dir.get().strip():
                    raise ValueError(t("error.books_dir"))
                if state_dir == books_dir or books_dir in state_dir.parents:
                    raise ValueError(t("error.same_dir"))
            passcode = None
            if self.needs_passcode and (new_library or not passcode_configured(state_dir / DATABASE_NAME)):
                passcode = self.passcode.get()
                if not 8 <= len(passcode) <= 256:
                    raise ValueError(t("error.passcode_length"))
                if passcode != self.passcode_repeat.get():
                    raise ValueError(t("error.passcode_match"))
            values = {
                "state_dir": str(state_dir),
                "port": self._integer(self.port, "settings.port"),
                "lan": self.lan.get(),
                "public_origin": self.origin.get().strip().rstrip("/"),
                "extra_hosts": parse_hosts(self.hosts.get()),
                "trusted_proxies": parse_hosts(self.proxies.get()),
                "cache_gib": self._integer(self.cache, "settings.cache"),
                "upload_gib": self._integer(self.upload, "settings.upload"),
                "launch_at_login": self.login.get(),
                "open_browser": self.open_browser.get(),
            }
            try:
                config = DesktopConfig.model_validate(values)
            except ValidationError as error:
                raise ValueError(t("error.invalid", detail=validation_message(error))) from None
        except ValueError as error:
            self.error.set(str(error))
            return
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            if new_library or passcode is not None:
                create_library(state_dir, books_dir, passcode)
        except Exception as error:  # shown in the window; the process must not end on a Tk callback
            self.root.configure(cursor="")
            self.error.set(t("error.failed", detail=error))
            return
        from .config import save_config

        try:
            save_config(config)
        except OSError as error:
            self.root.configure(cursor="")
            self.error.set(t("error.save_failed", detail=error))
            return
        self.result = SAVED
        self.root.destroy()

    def run(self) -> int:
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry(f"+{x}+{y}")
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        if sys.platform == "darwin":
            _activate_macos()
        (self.passcode_entry if self.setup else self.root).focus_set()
        self.root.mainloop()
        return self.result


def _activate_macos() -> None:
    """Bring the window forward; a menu bar app is not activated when it opens a window."""
    try:
        appkit: Any = importlib.import_module("AppKit")
    except ImportError:
        return
    appkit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


def run(config: DesktopConfig) -> int:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    return SettingsWindow(config).run()
