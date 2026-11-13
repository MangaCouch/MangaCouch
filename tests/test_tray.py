"""The tray icon: each menu entry runs its action, and server changes update the icon.

pystray is replaced by a recording fake, so these tests need no display, menu bar or notification area
on any platform. Background threads and the action executor run inline, so an action has finished by
the time the test looks at its effect.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import types
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mangacouch import desktop
from mangacouch.desktop.config import DesktopConfig, load_config, save_config
from mangacouch.desktop.i18n import t
from mangacouch.desktop.service import State, Status, cli_command, log_path

pytest.importorskip("tkinter")  # the tray imports the settings window module, which imports Tk
pytest.importorskip("PIL")


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, factory):
        self.factory = factory


class FakeMenuItem:
    def __init__(self, text, action, *, enabled=True, default=False, checked=None):
        self.text, self.action, self.enabled, self.default, self.checked = (
            text,
            action,
            enabled,
            default,
            checked,
        )


class FakeIcon:
    def __init__(self, name, *, icon, title, menu):
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.visible = self.stopped = False
        self.menu_updates = 0
        self.notifications: list[tuple[str, str]] = []
        self.refuse_notifications = False
        self._status_item = MagicMock()

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, message, title):
        if self.refuse_notifications:
            raise RuntimeError("notifications are off")
        self.notifications.append((message, title))

    def run(self, setup):
        setup(self)

    def stop(self):
        self.stopped = True


class FakeService:
    def __init__(self, on_change):
        self.on_change = on_change
        self.status = Status(State.STOPPED)
        self.calls: list[tuple] = []

    def start(self, config):
        self.calls.append(("start", config))

    def stop(self):
        self.calls.append(("stop",))

    def restart(self, config):
        self.calls.append(("restart", config))

    def change(self, status: Status) -> None:
        self.status = status
        self.on_change(status)


class InlineThread:
    def __init__(self, target, args=(), name=None, daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class FakeTimer:
    def __init__(self, interval, function):
        self.interval, self.function, self.daemon = interval, function, False

    def start(self):
        pass

    def cancel(self):
        pass


class InlineExecutor:
    def __init__(self, **options):
        pass

    def submit(self, function, *args):
        future = Future()
        future.set_result(function(*args))
        return future

    def shutdown(self, **options):
        pass


class Settings:
    """The settings window process: `saves` is the configuration it saves, or None for Cancel."""

    def __init__(self):
        self.saves: DesktopConfig | None = None
        self.commands: list[tuple[list[str], int]] = []
        self.terminated = False

    def __call__(self, command, creationflags=0):
        self.commands.append((command, creationflags))
        return self

    def wait(self, timeout=None) -> int:
        if self.saves is None:
            return 3
        save_config(self.saves)
        return 0

    def terminate(self):
        self.terminated = True


class Desktop:
    """The tray module with its toolkit and operating system seams recorded."""

    def __init__(self, module, tmp_path: Path, monkeypatch):
        self.module, self.tmp_path = module, tmp_path
        self.opened: list[str] = []
        self.launched: list[list[str]] = []
        self.autostart: list[bool] = []
        self.settings = Settings()
        self.books = tmp_path / "books"
        self.books.mkdir()

        def popen(command, creationflags=0):
            if command[-2:] == ["desktop", "--settings"]:
                return self.settings(command, creationflags)
            self.launched.append(command)
            return None

        monkeypatch.setattr(module, "Service", FakeService)
        monkeypatch.setattr(
            module, "threading", SimpleNamespace(Thread=InlineThread, Lock=threading.Lock, Timer=FakeTimer)
        )
        monkeypatch.setattr(module, "ThreadPoolExecutor", InlineExecutor)
        monkeypatch.setattr(module, "webbrowser", SimpleNamespace(open=self.opened.append))
        monkeypatch.setattr(module, "autostart", SimpleNamespace(apply=self.autostart.append))
        monkeypatch.setattr(
            module,
            "subprocess",
            SimpleNamespace(
                Popen=popen, CREATE_NO_WINDOW=0x08000000, TimeoutExpired=subprocess.TimeoutExpired
            ),
        )
        monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(module, "library_root", lambda state_dir: self.books)
        monkeypatch.setattr(module, "MACOS", False)

    def configured(self, **options: Any) -> DesktopConfig:
        state = self.tmp_path / "state"
        state.mkdir(exist_ok=True)
        config = DesktopConfig(state_dir=str(state), **options)
        assert config.database is not None
        config.database.touch()
        save_config(config)
        return config

    def tray(self):
        tray = self.module.Tray()
        return tray, tray.service, tray.icon


def items(icon) -> dict[str, FakeMenuItem]:
    return {item.text: item for item in icon.menu.factory() if item is not FakeMenu.SEPARATOR}


def click(icon, text: str) -> None:
    item = items(icon)[text]
    assert item.enabled, text
    item.action()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGACOUCH_DESKTOP_HOME", str(tmp_path / "desktop-home"))
    toolkit = types.ModuleType("pystray")
    toolkit.__dict__.update(Icon=FakeIcon, Menu=FakeMenu, MenuItem=FakeMenuItem)
    monkeypatch.setitem(sys.modules, "pystray", toolkit)
    monkeypatch.delitem(sys.modules, "mangacouch.desktop.tray", raising=False)
    module = importlib.import_module("mangacouch.desktop.tray")
    from mangacouch.desktop import shutdown

    monkeypatch.setattr(shutdown, "macos_delegate", MagicMock())
    monkeypatch.setattr(shutdown, "windows_hooks", MagicMock())
    yield Desktop(module, tmp_path, monkeypatch)
    if getattr(desktop, "tray", None) is module:
        delattr(desktop, "tray")


def test_menu_entries_run_their_actions_while_running(fake):
    config = fake.configured(lan=True)
    tray, service, icon = fake.tray()
    lan = "http://192.168.1.5:8780"
    service.status = Status(State.RUNNING, lan_url=lan)
    log_path().parent.mkdir(parents=True)
    log_path().write_bytes(b"server log")
    entries = list(icon.menu.factory())
    assert entries.count(FakeMenu.SEPARATOR) == 4
    status = entries[0]
    assert status.text == t("status", state=t("state.running")) and status.action is None
    assert not status.enabled
    assert [item.text for item in entries if getattr(item, "default", False)] == [t("menu.open")]

    click(icon, t("menu.open"))
    click(icon, t("menu.lan", url=lan))
    assert fake.opened == [config.local_url, lan]
    click(icon, t("menu.stop"))
    click(icon, t("menu.restart"))
    assert service.calls == [("stop",), ("restart", config)]
    click(icon, t("menu.books"))
    click(icon, t("menu.logs"))
    assert fake.launched == [["xdg-open", str(fake.books)], ["xdg-open", str(log_path())]]


def test_start_entry_starts_a_stopped_server(fake):
    config = fake.configured()
    tray, service, icon = fake.tray()
    assert t("menu.stop") not in items(icon)
    click(icon, t("menu.start"))
    assert service.calls == [("start", config)]


def test_entries_whose_target_is_gone_do_nothing(fake):
    fake.configured()
    tray, service, icon = fake.tray()
    service.status = Status(State.RUNNING, lan_url="http://192.168.1.5:8780")
    menu = items(icon)
    service.status = Status(State.RUNNING)
    menu[t("menu.lan", url="http://192.168.1.5:8780")].action()
    fake.books.rmdir()
    menu[t("menu.books")].action()
    menu[t("menu.logs")].action()  # no server log was written yet
    assert fake.opened == [] and fake.launched == []


def test_launch_at_login_entry_toggles_the_saved_setting(fake):
    fake.configured()
    tray, _, icon = fake.tray()
    login = items(icon)[t("menu.login")]
    assert login.checked is not None and login.checked(login) is False
    login.action()
    assert load_config().launch_at_login and fake.autostart == [True]
    refreshed = items(icon)[t("menu.login")]
    assert refreshed.checked is not None and refreshed.checked(refreshed) is True
    refreshed.action()
    assert not load_config().launch_at_login and fake.autostart == [True, False]


def test_quit_closes_the_settings_window_stops_the_server_and_removes_the_icon(fake):
    fake.configured()
    tray, service, icon = fake.tray()
    tray.settings_process = fake.settings
    click(icon, t("menu.quit"))
    assert tray.quitting and fake.settings.terminated
    assert service.calls == [("stop",)] and icon.stopped


def test_first_setup_starts_the_server_and_opens_the_browser_once_running(fake):
    tray, service, icon = fake.tray()
    assert not tray.configured
    assert not items(icon)[t("menu.start")].enabled
    state = fake.tmp_path / "state"
    state.mkdir()
    (state / "library.sqlite").touch()
    fake.settings.saves = DesktopConfig(state_dir=str(state), launch_at_login=True)
    click(icon, t("menu.settings"))
    assert fake.settings.commands == [([*cli_command(), "desktop", "--settings"], 0)]
    assert service.calls == [("start", fake.settings.saves)] and fake.autostart == [True]
    service.change(Status(State.STARTING))
    assert fake.opened == []
    service.change(Status(State.RUNNING))
    service.change(Status(State.RUNNING, lan_url="http://192.168.1.5:8780"))
    assert fake.opened == [fake.settings.saves.local_url]


def test_changed_server_settings_restart_a_running_server(fake):
    fake.configured()
    tray, service, icon = fake.tray()
    service.status = Status(State.RUNNING)
    fake.settings.saves = tray.config.model_copy(update={"port": 9123})
    click(icon, t("menu.settings"))
    assert service.calls == [("restart", fake.settings.saves)] and tray.config.port == 9123
    assert fake.opened == [] and fake.autostart == []


def test_cancelled_unchanged_or_already_open_settings_change_nothing(fake):
    config = fake.configured()
    tray, service, icon = fake.tray()
    click(icon, t("menu.settings"))  # cancelled
    assert tray.config == config and icon.menu_updates == 0
    fake.settings.saves = config
    click(icon, t("menu.settings"))  # saved as it was: only the menu is refreshed
    assert icon.menu_updates == 1
    with tray.settings_open:
        click(icon, t("menu.settings"))
    assert len(fake.settings.commands) == 2 and service.calls == []


def test_server_changes_update_the_icon_and_a_failure_is_notified_once(fake):
    fake.configured()
    tray, service, icon = fake.tray()
    service.change(Status(State.STOPPING))
    assert icon.title == t("status", state=t("state.stopping")) and icon.menu_updates == 1
    service.change(Status(State.FAILED, "port_in_use:8780"))
    assert icon.title == t("status", state=t("state.failed"))
    assert icon.notifications == [
        (t("notify.failed", detail=t("detail.port_in_use", port="8780")), "MangaCouch")
    ]
    # A failed start no longer opens the browser once the server finally runs.
    service.change(Status(State.RUNNING))
    assert fake.opened == []
    icon.refuse_notifications = True
    service.change(Status(State.LIBRARY_MISSING, "/missing"))  # best effort: no error escapes
    tray.quitting = True
    icon.refuse_notifications = False
    service.change(Status(State.FAILED, "not_responding"))
    assert len(icon.notifications) == 1


def test_running_the_tray_starts_a_configured_library_and_stops_the_server_on_exit(fake):
    fake.configured(launch_at_login=True, open_browser=False)
    tray, service, icon = fake.tray()
    tray.run()
    assert icon.visible and icon.menu_updates == 1 and fake.autostart == [True]
    assert service.calls == [("start", tray.config), ("stop",)] and tray.quitting


def test_running_the_tray_without_a_library_opens_the_setup_window(fake):
    tray, service, icon = fake.tray()
    tray.run()
    assert icon.visible and len(fake.settings.commands) == 1
    assert service.calls == [("stop",)] and fake.autostart == []


def test_macos_menu_bar_runs_on_the_main_thread_with_a_template_image(fake, monkeypatch):
    fake.configured()
    modules: dict[str, MagicMock] = {}

    def objc(name: str) -> MagicMock:
        return modules.setdefault(name, MagicMock(name=name))

    objc("PyObjCTools.AppHelper").callAfter.side_effect = lambda function: function()
    monkeypatch.setattr(fake.module, "objc", objc)
    monkeypatch.setattr(fake.module, "MACOS", True)
    tray, service, icon = fake.tray()
    tray.run()
    appkit = modules["AppKit"]
    appkit.NSApplication.sharedApplication.return_value.setActivationPolicy_.assert_called_once_with(
        appkit.NSApplicationActivationPolicyAccessory
    )
    picture = appkit.NSImage.alloc.return_value.initWithData_.return_value
    picture.setSize_.assert_called_once_with((18, 18))
    picture.setTemplate_.assert_called_once_with(True)
    icon._status_item.button.return_value.setImage_.assert_called_once_with(picture)
    data = modules["Foundation"].NSData.dataWithBytes_length_.call_args.args
    assert data[0].startswith(b"\x89PNG") and data[1] == len(data[0])
    assert icon.visible and service.calls == [("start", tray.config), ("stop",)]


# A failing login item never holds back the server ------------------------------------------------------


def failing_login(fake, monkeypatch, failures: dict[bool, Exception]) -> None:
    def apply(enabled: bool) -> None:
        fake.autostart.append(enabled)
        if enabled in failures:
            raise failures[enabled]

    monkeypatch.setattr(fake.module, "autostart", SimpleNamespace(apply=apply))


def test_existing_library_starts_when_the_login_item_cannot_be_registered(fake, monkeypatch):
    failing_login(fake, monkeypatch, {True: OSError("permission denied")})
    fake.configured(launch_at_login=True, open_browser=False)
    tray, service, icon = fake.tray()
    tray.run()
    assert service.calls == [("start", tray.config), ("stop",)] and fake.autostart == [True]
    assert icon.notifications == [(t("notify.login_failed", detail="permission denied"), "MangaCouch")]
    assert load_config().launch_at_login  # kept: the next start tries again


def test_first_setup_starts_and_turns_the_login_item_back_off_when_it_fails(fake, monkeypatch):
    failing_login(fake, monkeypatch, {True: OSError("permission denied")})
    tray, service, icon = fake.tray()
    state = fake.tmp_path / "state"
    state.mkdir()
    (state / "library.sqlite").touch()
    fake.settings.saves = DesktopConfig(state_dir=str(state), launch_at_login=True)
    click(icon, t("menu.settings"))
    assert service.calls == [("start", fake.settings.saves)]
    assert icon.notifications == [(t("notify.login_failed", detail="permission denied"), "MangaCouch")]
    assert not tray.config.launch_at_login and not load_config().launch_at_login
    assert load_config().state_dir == str(state)
    login = items(icon)[t("menu.login")]
    assert login.checked is not None and login.checked(login) is False


def test_saved_settings_restart_even_when_the_login_item_cannot_be_removed(fake, monkeypatch):
    failing_login(fake, monkeypatch, {False: OSError("key is locked")})
    fake.configured(launch_at_login=True)
    tray, service, icon = fake.tray()
    service.status = Status(State.RUNNING)
    fake.settings.saves = tray.config.model_copy(update={"port": 9123, "launch_at_login": False})
    click(icon, t("menu.settings"))
    assert service.calls == [("restart", fake.settings.saves)]
    assert icon.notifications == [(t("notify.login_remove_failed", detail="key is locked"), "MangaCouch")]
    assert tray.config.launch_at_login and load_config().launch_at_login
    assert load_config().port == 9123


def test_menu_toggle_is_undone_when_the_login_item_fails(fake, monkeypatch):
    failing_login(fake, monkeypatch, {True: OSError("permission denied")})
    fake.configured()
    tray, _, icon = fake.tray()
    click(icon, t("menu.login"))
    assert fake.autostart == [True]
    assert not tray.config.launch_at_login and not load_config().launch_at_login
    assert icon.notifications == [(t("notify.login_failed", detail="permission denied"), "MangaCouch")]


def test_unreadable_launcher_settings_are_kept_aside_and_reported(fake):
    from mangacouch.desktop.config import config_path

    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("{broken", encoding="utf-8")
    tray, _, icon = fake.tray()
    tray.run()
    assert tray.config == DesktopConfig() and len(fake.settings.commands) == 1
    assert len(icon.notifications) == 1
    assert icon.notifications[0][0].startswith(t("notify.config_invalid", detail="").strip())
    assert config_path().with_name("desktop.json.invalid").read_text(encoding="utf-8") == "{broken"


def test_linux_menu_toggle_writes_and_removes_the_xdg_autostart_entry(fake, monkeypatch):
    from mangacouch.desktop import autostart

    monkeypatch.setattr(fake.module, "autostart", autostart)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake.tmp_path / "xdg"))
    entry = fake.tmp_path / "xdg" / "autostart" / "mangacouch.desktop"
    fake.configured()
    tray, _, icon = fake.tray()
    click(icon, t("menu.login"))
    assert tray.config.launch_at_login and "X-GNOME-Autostart-enabled=true" in entry.read_text()
    click(icon, t("menu.login"))
    assert not tray.config.launch_at_login and not entry.exists()
    (fake.tmp_path / "xdg" / "autostart").rmdir()
    (fake.tmp_path / "xdg" / "autostart").write_text("")  # not a folder: the entry cannot be written
    click(icon, t("menu.login"))
    assert not tray.config.launch_at_login and not load_config().launch_at_login
    [(message, _)] = icon.notifications
    assert message.startswith(t("notify.login_failed", detail=""))


def test_saving_after_database_disappears_restarts_the_active_server(fake):
    config = fake.configured()
    tray, service, _ = fake.tray()
    service.status = Status(State.RUNNING)
    config.database.unlink()
    fake.settings.saves = config.model_copy(update={"port": 9123})
    tray._run_settings()
    assert service.calls == [("restart", fake.settings.saves)]


def test_repeated_quit_shares_cleanup_and_runs_termination_reply(fake):
    fake.configured()
    tray, service, icon = fake.tray()
    tray.settings_process = fake.settings
    replied = []
    first = tray._quit(completed=lambda: replied.append(True))
    second = tray._quit(completed=lambda: replied.append(True))
    assert first is second and first.done()
    assert replied == [True, True] and service.calls == [("stop",)]
    assert fake.settings.terminated and icon.stopped


def test_windows_tray_and_server_start_when_session_end_hooks_fail(fake, monkeypatch, caplog):
    from mangacouch.desktop import shutdown

    monkeypatch.setattr(fake.module, "sys", SimpleNamespace(platform="win32"))
    hooks = MagicMock(side_effect=OSError("no user32"))
    monkeypatch.setattr(shutdown, "windows_hooks", hooks)
    fake.configured(open_browser=False)
    tray, service, icon = fake.tray()
    tray.run()
    hooks.assert_called_once()
    assert icon.visible and service.calls == [("start", tray.config), ("stop",)]
    assert "session end hooks" in caplog.text
