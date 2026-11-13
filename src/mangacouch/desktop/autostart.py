"""Launch the tray at login: a LaunchAgent on macOS, the per-user Run key on Windows, an XDG
autostart entry elsewhere (Linux desktops)."""

from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .service import cli_command

LABEL = "io.github.mangacouch.desktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MangaCouch"
DESKTOP_FILE = "mangacouch.desktop"
EXEC_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")
"""Characters that make an Exec argument need double quotes (Desktop Entry Specification)."""


def launch_command() -> list[str]:
    command = cli_command()
    if not getattr(sys, "frozen", False):
        command.append("desktop")
        if sys.platform == "win32" and Path(command[0]).name.lower() == "python.exe":
            # The windowless interpreter, so no console window opens at login.
            windowless = Path(command[0]).with_name("pythonw.exe")
            if windowless.is_file():
                command[0] = str(windowless)
    return command


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launch_agent(command: list[str]) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
        }
    )


def windows_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def autostart_path() -> Path:
    """The per-user XDG autostart entry; a relative XDG_CONFIG_HOME is ignored, as the Base
    Directory Specification requires."""
    config = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(config) if os.path.isabs(config) else Path.home() / ".config"
    return base / "autostart" / DESKTOP_FILE


def desktop_exec(command: list[str]) -> str:
    """The Exec value running `command`: arguments quoted per the Desktop Entry Specification, then
    escaped as a string value."""

    def quote(argument: str) -> str:
        argument = argument.replace("%", "%%")
        if argument and not EXEC_RESERVED.intersection(argument):
            return argument
        return '"' + "".join("\\" + c if c in '"`$\\' else c for c in argument) + '"'

    line = " ".join(quote(argument) for argument in command)
    return line.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")


def desktop_entry(command: list[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=MangaCouch\n"
        f"Exec={desktop_exec(command)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def transient_location(executable: str) -> bool:
    """Whether macOS runs the app from a place that is gone after a restart: the mounted disk image
    (read-only) or a translocated copy of an app that was opened from Downloads without moving it."""
    if "/AppTranslocation/" in executable:
        return True
    if not executable.startswith("/Volumes/"):
        return False
    try:
        return bool(os.statvfs(executable).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def apply(enabled: bool) -> None:
    """Register or remove the login item; registering again records the current executable path."""
    if sys.platform == "darwin":
        path = launch_agent_path()
        if enabled and transient_location(launch_command()[0]):
            raise OSError("Move MangaCouch.app to the Applications folder first")
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_bytes(launch_agent(launch_command()))
            os.replace(temporary, path)
        else:
            path.unlink(missing_ok=True)
    elif sys.platform == "win32":
        import winreg

        # A fresh profile may not have the Run key yet, so registering creates it and removing
        # from a missing key is a no-op.
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, windows_command(launch_command()))
        else:
            with (
                contextlib.suppress(FileNotFoundError),
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key,
            ):
                winreg.DeleteValue(key, RUN_VALUE)
    else:
        path = autostart_path()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(desktop_entry(launch_command()), encoding="utf-8")
            os.replace(temporary, path)
        else:
            path.unlink(missing_ok=True)
