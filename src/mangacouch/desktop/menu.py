"""What the tray shows and decides, without the tray toolkit, so it can be tested on any platform."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import DesktopConfig
from .i18n import t
from .service import State, Status

# Changing these needs a server restart; the rest only affect the launcher.
SERVER_FIELDS = (
    "state_dir",
    "port",
    "lan",
    "public_origin",
    "extra_hosts",
    "trusted_proxies",
    "cache_gib",
    "upload_gib",
)

Action = Literal["open", "open_lan", "start", "stop", "restart", "settings", "books", "logs", "login", "quit"]


@dataclass(frozen=True)
class Entry:
    text: str
    action: Action | None = None
    """None for an informational line or a separator."""
    enabled: bool = True
    default: bool = False
    """The action of a left click on Windows; shown in bold."""
    checked: bool | None = None


SEPARATOR = Entry("-")


def describe(status: Status) -> str:
    """A short, translated reason for the current state."""
    if status.state == State.LIBRARY_MISSING:
        return t("detail.library_missing", path=status.detail)
    detail = status.detail
    if detail.startswith("port_in_use:"):
        return t("detail.port_in_use", port=detail.partition(":")[2])
    if detail == "not_responding":
        return t(f"detail.{detail}")
    return detail if len(detail) <= 90 else detail[:89] + "…"


def state_label(status: Status, *, configured: bool) -> str:
    return (
        t(f"state.{status.state.value}")
        if configured or status.state == State.LIBRARY_MISSING
        else t("state.unconfigured")
    )


def menu_entries(status: Status, *, configured: bool, launch_at_login: bool, log_exists: bool) -> list[Entry]:
    running = status.state == State.RUNNING
    busy = status.state == State.STOPPING
    active = status.state in {State.STARTING, State.RUNNING}
    entries = [Entry(t("status", state=state_label(status, configured=configured)), enabled=False)]
    if status.detail and status.state in {State.FAILED, State.STARTING, State.LIBRARY_MISSING}:
        entries.append(Entry(describe(status), enabled=False))
    entries += [SEPARATOR, Entry(t("menu.open"), "open", enabled=running, default=True)]
    if status.lan_url:
        entries.append(Entry(t("menu.lan", url=status.lan_url), "open_lan", enabled=running))
    entries.append(SEPARATOR)
    if active:
        entries.append(Entry(t("menu.stop"), "stop", enabled=not busy))
    else:
        entries.append(Entry(t("menu.start"), "start", enabled=configured and not busy))
    entries += [
        Entry(t("menu.restart"), "restart", enabled=configured and not busy),
        SEPARATOR,
        Entry(t("menu.reselect" if status.state == State.LIBRARY_MISSING else "menu.settings"), "settings"),
        Entry(t("menu.books"), "books", enabled=configured),
        Entry(t("menu.logs"), "logs", enabled=log_exists),
        Entry(t("menu.login"), "login", checked=launch_at_login),
        SEPARATOR,
        Entry(t("menu.quit"), "quit"),
    ]
    return entries


def after_settings(
    previous: DesktopConfig, current: DesktopConfig, *, was_configured: bool, state: State
) -> Literal["start", "restart", "none"]:
    """What the tray does once the settings window saved: the first setup starts the server, a changed
    server option restarts it unless the user had stopped it."""
    changed = any(getattr(previous, name) != getattr(current, name) for name in SERVER_FIELDS)
    if changed and state in {State.STARTING, State.RUNNING, State.FAILED, State.LIBRARY_MISSING}:
        return "restart"
    if not was_configured and state == State.STOPPED:
        return "start"
    return "none"
