from __future__ import annotations

import contextlib
import json
import os
import plistlib
import socket
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from helpers import free_port
from pydantic import ValidationError

from mangacouch.archives import Limits
from mangacouch.auth import Auth
from mangacouch.database import Library
from mangacouch.desktop import acquire_instance_lock, autostart
from mangacouch.desktop.config import (
    DesktopConfig,
    find_state_directory,
    load_config,
    save_config,
)
from mangacouch.desktop.i18n import STRINGS, t
from mangacouch.desktop.icons import logo, tray_icon
from mangacouch.desktop.menu import Entry, after_settings, describe, menu_entries
from mangacouch.desktop.service import RotatingLog, Service, State, Status, failure_detail
from mangacouch.lifecycle import initialize_library


@pytest.fixture(autouse=True)
def desktop_home(tmp_path, monkeypatch):
    home = tmp_path / "desktop-home"
    monkeypatch.setenv("MANGACOUCH_DESKTOP_HOME", str(home))
    for name in ("MANGACOUCH_DB", "MANGACOUCH_ROOT", "MANGACOUCH_PORT", "MANGACOUCH_HOST"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def state_dir(tmp_path):
    root = tmp_path / "books"
    root.mkdir()
    (tmp_path / "state").mkdir()
    library = initialize_library(tmp_path / "state" / "library.sqlite", root, Limits())
    Auth(library).initialize("desktop-fixture-passcode")
    return library.path.parent


def option_values(arguments: list[str], option: str) -> list[str]:
    return [arguments[index + 1] for index, value in enumerate(arguments) if value == option]


def test_local_configuration_serves_on_loopback(tmp_path):
    config = DesktopConfig(state_dir=str(tmp_path), port=9123, cache_gib=3, upload_gib=1)
    arguments = config.serve_arguments(lan_address=None)
    assert arguments[:3] == ["--db", str(tmp_path / "library.sqlite"), "serve"]
    assert option_values(arguments, "--host") == ["127.0.0.1"]
    assert option_values(arguments, "--origin") == ["http://localhost:9123"]
    assert option_values(arguments, "--allowed-host") == ["localhost", "127.0.0.1"]
    assert option_values(arguments, "--cache-bytes") == [str(3 * 1024**3)]
    assert option_values(arguments, "--upload-bytes") == [str(1024**3)]
    assert "--stop-on-stdin-eof" in arguments
    assert config.local_url == "http://127.0.0.1:9123"


def test_lan_configuration_uses_the_network_address(tmp_path):
    config = DesktopConfig(state_dir=str(tmp_path), lan=True, extra_hosts=["nas.home.arpa"])
    arguments = config.serve_arguments(lan_address="192.168.1.20")
    assert option_values(arguments, "--host") == ["0.0.0.0"]
    assert option_values(arguments, "--origin") == ["http://192.168.1.20:8780"]
    hosts = option_values(arguments, "--allowed-host")
    assert {"localhost", "127.0.0.1", "0.0.0.0", "nas.home.arpa"} <= set(hosts)
    # The origin falls back to loopback when no network address is known.
    assert config.origin(None) == "http://localhost:8780"


def test_public_origin_overrides_the_derived_one(tmp_path):
    config = DesktopConfig(state_dir=str(tmp_path), lan=True, public_origin="https://manga.example")
    assert option_values(config.serve_arguments("10.0.0.2"), "--origin") == ["https://manga.example"]
    assert config.local_url == "https://manga.example"


@pytest.mark.parametrize(
    "values",
    [
        {"public_origin": "https://manga.example/path"},
        {"extra_hosts": ["Not A Host"]},
        {"port": 80},
        {"cache_gib": 0},
    ],
)
def test_invalid_server_options_are_rejected(values):
    with pytest.raises(ValidationError):
        DesktopConfig.model_validate(values)


def test_configuration_round_trip_and_defaults(tmp_path, desktop_home):
    assert load_config() == DesktopConfig()
    config = DesktopConfig(state_dir=str(tmp_path / "数据"), port=9000, launch_at_login=True)
    save_config(config)
    assert load_config() == config
    (desktop_home / "desktop.json").write_text("{not json", encoding="utf-8")
    assert load_config() == DesktopConfig()


def test_choosing_the_parent_folder_finds_the_state_folder(state_dir):
    assert find_state_directory(state_dir.parent) == state_dir
    assert find_state_directory(state_dir) == state_dir


def test_failure_detail_prefers_the_cli_error(tmp_path):
    log = tmp_path / "server.log"
    log.write_text('starting\n{"error": "InvalidInput", "detail": "Run auth-init"}\n', encoding="utf-8")
    assert failure_detail(log) == "Run auth-init"
    log.write_text("ERROR: [Errno 48] address already in use\n\n", encoding="utf-8")
    assert failure_detail(log) == "ERROR: [Errno 48] address already in use"
    assert failure_detail(tmp_path / "missing.log") == ""


class Watched(Service):
    """A Service whose status changes are recorded and can be awaited."""

    def __init__(self):
        self.changes: list[Status] = []
        self.changed = threading.Condition()
        super().__init__(on_change=self.record)

    def record(self, status: Status) -> None:
        with self.changed:
            self.changes.append(status)
            self.changed.notify_all()

    def wait_for(self, states: set[State], timeout: float = 90) -> Status:
        with self.changed:
            if not self.changed.wait_for(lambda: self.status.state in states, timeout):
                raise AssertionError(f"Service stayed {self.status}")
        return self.status


def test_service_starts_reports_running_and_stops_gracefully(state_dir, desktop_home):
    service = Watched()
    config = DesktopConfig(state_dir=str(state_dir))
    for attempt in range(3):
        config = DesktopConfig(state_dir=str(state_dir), port=free_port())
        service.start(config)
        status = service.wait_for({State.RUNNING, State.FAILED})
        if status.state == State.RUNNING:
            break
        service.stop()
        assert attempt < 2 and "port_in_use" in status.detail, status
        service.changes.clear()
    try:
        response = httpx.get(f"http://127.0.0.1:{config.port}/", headers={"Host": f"localhost:{config.port}"})
        assert response.status_code == 200
    finally:
        service.stop()
    assert service.status == Status(State.STOPPED)
    assert [status.state for status in service.changes] == [
        State.STARTING,
        State.RUNNING,
        State.STOPPING,
        State.STOPPED,
    ]
    # Settings passed on the command line were saved by serve, as with a manual start.
    with Library(state_dir / "library.sqlite").connection() as db:
        saved = json.loads(db.execute("SELECT settings_json FROM http_config").fetchone()[0])
    assert saved["public_origin"] == f"http://localhost:{config.port}"


def test_service_reports_why_it_did_not_start(tmp_path, state_dir):
    service = Service()
    service.start(DesktopConfig(state_dir=str(tmp_path / "nowhere")))
    assert service.status == Status(State.LIBRARY_MISSING, str(tmp_path / "nowhere" / "library.sqlite"))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        service.start(DesktopConfig(state_dir=str(state_dir), port=port))
        assert service.status == Status(State.STARTING, f"port_in_use:{port}")
    service.stop()
    assert service.status.state == State.STOPPED


def test_service_surfaces_a_failed_start(tmp_path, monkeypatch):
    from mangacouch.desktop import service as service_module

    # The server stops before binding; a port taken meanwhile must not change the outcome.
    monkeypatch.setattr(service_module, "port_in_use", lambda host, port: False)
    root = tmp_path / "books"
    root.mkdir()
    (tmp_path / "state").mkdir()
    library = initialize_library(tmp_path / "state" / "library.sqlite", root, Limits())
    service = Watched()
    service.start(DesktopConfig(state_dir=str(library.path.parent), port=free_port()))
    status = service.wait_for({State.FAILED, State.STOPPED})
    assert status.state == State.FAILED and "auth-init" in status.detail


def test_server_exits_when_stdin_closes(state_dir):
    from helpers import start_test_server

    process, port, response = start_test_server(
        lambda port: [
            sys.executable,
            "-m",
            "mangacouch",
            *DesktopConfig(state_dir=str(state_dir), port=port).serve_arguments(None),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        headers=lambda port: {"Host": f"localhost:{port}"},
    )
    try:
        assert response.status_code == 401
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(30) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stderr is not None:
            process.stderr.close()


def test_only_one_tray_holds_the_lock():
    first = acquire_instance_lock()
    assert first is not None
    try:
        # A second process is refused while the first holds it.
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from mangacouch.desktop import acquire_instance_lock as a; print(a() is None)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert probe.stdout.strip() == "True", probe.stderr
    finally:
        first.close()
    second = acquire_instance_lock()
    assert second is not None
    second.close()


def test_login_items(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Mänga Couch" / "mangacouch.exe"))
    command = autostart.launch_command()
    assert command == [sys.executable]
    agent = plistlib.loads(autostart.launch_agent(command))
    assert agent["ProgramArguments"] == command and agent["RunAtLoad"] is True
    assert autostart.windows_command(command) == f'"{sys.executable}"'


def test_login_item_from_source_runs_the_desktop_command(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert autostart.launch_command()[-3:] == ["-m", "mangacouch", "desktop"]


def test_xdg_exec_quotes_arguments_per_the_desktop_entry_spec():
    assert autostart.desktop_exec(["/opt/mangacouch/mangacouch"]) == "/opt/mangacouch/mangacouch"
    assert autostart.desktop_exec(["/opt/Mänga Couch/mangacouch"]) == '"/opt/Mänga Couch/mangacouch"'
    # Exec quoting escapes " ` $ \ with a backslash; the string value then doubles every backslash.
    command = [r'/home/a\b$c/py"thon', "-m", "mangacouch", "desktop", "100%"]
    assert autostart.desktop_exec(command) == r'"/home/a\\\\b\\$c/py\\"thon" -m mangacouch desktop 100%%'


def test_xdg_autostart_entry_is_written_and_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    # A Linux path on every test platform: Windows separators would be escaped in Exec.
    monkeypatch.setattr(sys, "executable", "/opt/Manga Couch/mangacouch")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tmp_path / "config" / "autostart" / "mangacouch.desktop"
    assert autostart.autostart_path() == path
    autostart.apply(True)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[Desktop Entry]"
    assert 'Exec="/opt/Manga Couch/mangacouch"' in lines
    assert {"Type=Application", "X-GNOME-Autostart-enabled=true"} <= set(lines)
    assert [entry.name for entry in path.parent.iterdir()] == ["mangacouch.desktop"]
    autostart.apply(False)
    assert not path.exists()
    autostart.apply(False)  # removing an absent entry is not an error


def test_xdg_autostart_defaults_to_the_home_config_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for value in ("", "relative/config"):
        monkeypatch.setenv("XDG_CONFIG_HOME", value)
        assert autostart.autostart_path() == tmp_path / ".config" / "autostart" / "mangacouch.desktop"


@pytest.mark.skipif(sys.platform != "darwin", reason="LaunchAgents are macOS only")
def test_launch_agent_is_written_and_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    autostart.apply(True)
    assert plistlib.loads(autostart.launch_agent_path().read_bytes())["Label"] == autostart.LABEL
    autostart.apply(False)
    assert not autostart.launch_agent_path().exists()


def test_strings_exist_in_every_language():
    assert STRINGS["en"].keys() == STRINGS["zh"].keys()


def test_icons_render_every_state():
    assert logo(256).size == (256, 256)
    for state in ("running", "starting", "stopped", "failed"):
        for template in (True, False):
            image = tray_icon(state, template=template)
            assert image.mode == "RGBA" and image.getbbox() is not None


def test_settings_window_creates_the_library(tmp_path):
    pytest.importorskip("tkinter")
    from mangacouch.desktop.settings_window import create_library, library_root, passcode_configured

    state, books = tmp_path / "state", tmp_path / "books"
    create_library(state, books, "first-passcode")
    assert library_root(state) == books.absolute()
    assert passcode_configured(state / "library.sqlite")
    # An existing library is reused, not initialized again.
    create_library(state, tmp_path / "elsewhere", None)
    assert not (tmp_path / "elsewhere").exists()


def test_desktop_command_rejects_unknown_arguments():
    result = subprocess.run(
        [sys.executable, "-m", "mangacouch", "desktop", "--bogus"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 2 and "usage: mangacouch desktop" in result.stderr


# Tray menu and follow-up decisions --------------------------------------------------------------------


def entries_for(status: Status, *, configured: bool = True, login: bool = False, log: bool = True):
    entries = menu_entries(status, configured=configured, launch_at_login=login, log_exists=log)
    return {entry.action: entry for entry in entries if entry.action}, entries


def test_menu_while_running_on_the_lan():
    actions, entries = entries_for(Status(State.RUNNING, lan_url="http://192.168.1.5:8780"))
    assert entries[0] == Entry(t("status", state=t("state.running")), enabled=False)
    assert actions["open"].enabled and actions["open"].default
    assert actions["open_lan"].text == t("menu.lan", url="http://192.168.1.5:8780")
    assert actions["stop"].enabled and "start" not in actions
    assert actions["restart"].enabled and actions["books"].enabled
    assert [entry.default for entry in entries].count(True) == 1
    assert entries[-1].action == "quit"


def test_menu_while_unresponsive_can_stop_and_restart():
    actions, entries = entries_for(Status(State.STARTING, "not_responding"))
    assert entries[1] == Entry(t("detail.not_responding"), enabled=False)
    assert not actions["open"].enabled
    assert actions["stop"].enabled and actions["restart"].enabled
    assert "open_lan" not in actions


def test_menu_after_a_failed_start_offers_start_and_the_reason():
    actions, entries = entries_for(Status(State.FAILED, "port_in_use:8780"))
    assert entries[0].text == t("status", state=t("state.failed"))
    assert entries[1] == Entry(t("detail.port_in_use", port="8780"), enabled=False)
    assert actions["start"].enabled and not actions["open"].enabled and "stop" not in actions


def test_menu_before_setup_only_offers_settings():
    actions, entries = entries_for(Status(State.STOPPED), configured=False, log=False)
    assert entries[0].text == t("status", state=t("state.unconfigured"))
    assert not any(actions[name].enabled for name in ("open", "start", "restart", "books", "logs"))
    assert actions["settings"].enabled and actions["quit"].enabled


def test_menu_shows_the_login_item_state():
    assert entries_for(Status(State.STOPPED), login=True)[0]["login"].checked is True
    assert entries_for(Status(State.STOPPED), login=False)[0]["login"].checked is False


def test_long_failure_details_are_shortened():
    detail = describe(Status(State.FAILED, "x" * 200))
    assert len(detail) == 90 and detail.endswith("…")
    assert describe(Status(State.LIBRARY_MISSING, "/missing")) == t("detail.library_missing", path="/missing")


@pytest.mark.parametrize(
    ("was_configured", "change", "state", "expected"),
    [
        (False, {}, State.STOPPED, "start"),
        (False, {"port": 9000}, State.RUNNING, "restart"),
        (True, {"port": 9000}, State.LIBRARY_MISSING, "restart"),
        (True, {"port": 9000}, State.RUNNING, "restart"),
        (True, {"lan": True}, State.STARTING, "restart"),
        (True, {"extra_hosts": ["nas.home.arpa"]}, State.FAILED, "restart"),
        (True, {"port": 9000}, State.STOPPED, "none"),
        (True, {"open_browser": False, "launch_at_login": True}, State.RUNNING, "none"),
        (True, {}, State.RUNNING, "none"),
    ],
)
def test_what_happens_after_saving_settings(tmp_path, was_configured, change, state, expected):
    previous = DesktopConfig(state_dir=str(tmp_path))
    current = previous.model_copy(update=change)
    assert after_settings(previous, current, was_configured=was_configured, state=state) == expected


# Settings window --------------------------------------------------------------------------------------


@pytest.fixture
def settings_window():
    """Builds the real Tk window, withdrawn so it never appears or takes focus."""
    tkinter = pytest.importorskip("tkinter")
    from mangacouch.desktop.settings_window import SettingsWindow

    windows = []

    def make(config: DesktopConfig) -> SettingsWindow:
        try:
            window = SettingsWindow(config)
        except tkinter.TclError as error:  # no display, as on headless Linux
            pytest.skip(f"Tk cannot open a window: {error}")
        window.root.withdraw()
        windows.append(window)
        return window

    yield make
    for window in windows:
        with contextlib.suppress(tkinter.TclError):
            window.root.destroy()


def shown(widgets) -> bool:
    managers = {widget.winfo_manager() for widget in widgets}
    assert len(managers) == 1
    return managers == {"grid"}


def test_setup_window_validates_then_creates_the_library(tmp_path, settings_window):
    from mangacouch.desktop.settings_window import SAVED

    window = settings_window(DesktopConfig())
    assert window.setup and shown(window.books_rows) and shown(window.passcode_rows)
    window.state_dir.set(str(tmp_path / "library" / "state"))
    window.books_dir.set(str(tmp_path / "library" / "books"))
    window.port.set("9123")

    window.save()
    assert window.error.get() == t("error.passcode_length")
    window.passcode.set("first-passcode")
    window.passcode_repeat.set("other-passcode")
    window.save()
    assert window.error.get() == t("error.passcode_match")
    window.books_dir.set(str(tmp_path / "library"))  # would hold the data folder
    window.passcode_repeat.set("first-passcode")
    window.save()
    assert window.error.get() == t("error.same_dir")
    assert not (tmp_path / "library" / "state").exists() and load_config() == DesktopConfig()

    window.books_dir.set(str(tmp_path / "library" / "books"))
    window.save()
    assert window.result == SAVED and window.error.get() == ""
    saved = load_config()
    assert saved.state_dir == str(tmp_path / "library" / "state") and saved.port == 9123
    database = tmp_path / "library" / "state" / "library.sqlite"
    assert Auth(Library(database)).configured()


def test_settings_for_an_existing_library_hide_setup_fields(state_dir, settings_window):
    from mangacouch.desktop.settings_window import SAVED

    window = settings_window(DesktopConfig(state_dir=str(state_dir)))
    assert not window.setup
    assert not shown(window.books_rows) and not shown(window.passcode_rows)
    assert window.library_note.get() == t("settings.existing", root=state_dir.parent / "books")

    window.port.set("eighty")
    window.save()
    assert window.error.get() == t("error.number", field=t("settings.port"))
    window.port.set("9124")
    window.hosts.set("nas.home.arpa, Bad Host")
    window.save()
    assert window.error.get().startswith(t("error.invalid", detail=""))
    assert load_config() == DesktopConfig()

    window.hosts.set("NAS.home.arpa,  media.local")
    window.lan.set(True)
    window.login.set(True)
    window.save()
    assert window.result == SAVED
    saved = load_config()
    assert (saved.port, saved.lan, saved.launch_at_login) == (9124, True, True)
    assert saved.extra_hosts == ["nas.home.arpa", "media.local"]


def test_settings_ask_for_a_passcode_when_the_library_has_none(tmp_path, settings_window):
    (tmp_path / "state").mkdir()
    (tmp_path / "books").mkdir()
    initialize_library(tmp_path / "state" / "library.sqlite", tmp_path / "books", Limits())
    window = settings_window(DesktopConfig(state_dir=str(tmp_path / "state")))
    assert not shown(window.books_rows) and shown(window.passcode_rows)
    window.save()
    assert window.error.get() == t("error.passcode_length")
    window.passcode.set("late-passcode")
    window.passcode_repeat.set("late-passcode")
    window.save()
    assert window.error.get() == ""
    assert Auth(Library(tmp_path / "state" / "library.sqlite")).configured()


def test_switching_the_data_folder_updates_the_form(tmp_path, state_dir, settings_window):
    window = settings_window(DesktopConfig(state_dir=str(state_dir)))
    window.state_dir.set(str(tmp_path / "fresh"))
    assert shown(window.books_rows) and shown(window.passcode_rows)
    assert window.library_note.get() == t("settings.new")
    window.state_dir.set(str(state_dir))
    assert not shown(window.books_rows)


# Windows login item -----------------------------------------------------------------------------------


class FakeRegistry:
    """The few winreg calls autostart makes, over an in-memory HKEY_CURRENT_USER."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.keys: dict[str, dict[str, tuple[str, int]]] = {}

    @contextlib.contextmanager
    def CreateKeyEx(self, root, path, reserved=0, access=0):
        assert root == self.HKEY_CURRENT_USER
        yield self.keys.setdefault(path, {})

    @contextlib.contextmanager
    def OpenKey(self, root, path, reserved=0, access=0):
        assert root == self.HKEY_CURRENT_USER
        if path not in self.keys:
            raise FileNotFoundError(path)
        yield self.keys[path]

    def SetValueEx(self, key, name, reserved, kind, value):
        key[name] = (value, kind)

    def DeleteValue(self, key, name):
        if name not in key:
            raise FileNotFoundError(name)
        del key[name]


@pytest.fixture
def registry(monkeypatch):
    """Windows login items written to a fake registry, so no real Run key is ever touched."""
    fake = FakeRegistry()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    return fake


def test_run_key_is_written_and_removed(registry):
    registry.keys[autostart.RUN_KEY] = {"Other app": ("other.exe", registry.REG_SZ)}
    autostart.apply(True)
    command = autostart.windows_command(autostart.launch_command())
    assert registry.keys[autostart.RUN_KEY][autostart.RUN_VALUE] == (command, registry.REG_SZ)
    autostart.apply(False)
    assert registry.keys[autostart.RUN_KEY] == {"Other app": ("other.exe", registry.REG_SZ)}
    autostart.apply(False)  # removing an absent value is not an error


def test_run_key_is_created_when_missing(registry):
    # Some fresh profiles (and CI images) have no Run key at all.
    autostart.apply(False)  # removing from a missing key is not an error
    assert registry.keys == {}
    autostart.apply(True)
    assert registry.keys[autostart.RUN_KEY][autostart.RUN_VALUE][1] == registry.REG_SZ


def test_login_item_is_refused_from_a_disk_image_or_a_translocated_copy(monkeypatch):
    assert autostart.transient_location("/private/var/folders/x/AppTranslocation/1/d/MangaCouch.app/m")
    assert not autostart.transient_location("/Applications/MangaCouch.app/Contents/MacOS/mangacouch")
    monkeypatch.setattr(autostart.os, "ST_RDONLY", 1, raising=False)
    monkeypatch.setattr(autostart.os, "statvfs", lambda path: type("S", (), {"f_flag": 1})(), raising=False)
    assert autostart.transient_location("/Volumes/MangaCouch/MangaCouch.app/Contents/MacOS/mangacouch")
    monkeypatch.setattr(autostart.os, "statvfs", lambda path: type("S", (), {"f_flag": 0})(), raising=False)
    assert not autostart.transient_location("/Volumes/External/MangaCouch.app/Contents/MacOS/mangacouch")


def test_failure_detail_names_the_exception_behind_a_failed_startup(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(
        "2026-01-01 00:00:00.000 ERROR   uvicorn.error: Traceback (most recent call last):\n"
        '  File "downloads.py", line 952, in start\n'
        "ValueError: Another download worker is using this library\n"
        "\n"
        "2026-01-01 00:00:00.001 ERROR   uvicorn.error: Application startup failed. Exiting.\n",
        encoding="utf-8",
    )
    assert failure_detail(log) == "Another download worker is using this library"
    log.write_text("sqlite3.OperationalError: database is locked\nlater line\n", encoding="utf-8")
    assert failure_detail(log) == "database is locked"


def test_an_unreadable_desktop_json_is_kept_aside_and_reported(desktop_home):
    from mangacouch.desktop.config import config_path, read_config

    assert read_config() == (DesktopConfig(), None)
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text('{"port": "not a number"', encoding="utf-8")
    config, problem = read_config()
    assert config == DesktopConfig() and problem
    assert not config_path().exists()
    assert (desktop_home / "desktop.json.invalid").read_text(encoding="utf-8") == '{"port": "not a number"'


def test_server_children_keep_only_log_level_and_trusted_proxy_fallback(state_dir, monkeypatch):
    from mangacouch.desktop import service as service_module

    seen = {}

    class Started:
        stdin = stdout = None

        def __init__(self, command, **options):
            seen.update(options["env"])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setenv("MANGACOUCH_MAX_BYTES", "1")
    monkeypatch.setenv("MANGACOUCH_TRUSTED_PROXIES", "10.0.0.1")
    monkeypatch.setenv("MANGACOUCH_LOG_LEVEL", "debug")
    monkeypatch.setattr(service_module.subprocess, "Popen", Started)
    monkeypatch.setattr(service_module, "port_in_use", lambda host, port: False)
    service = Service()
    service.start(DesktopConfig(state_dir=str(state_dir), port=free_port()))
    service.stop()
    assert seen["MANGACOUCH_LOG_LEVEL"] == "debug"
    assert not {"MANGACOUCH_MAX_BYTES", "MANGACOUCH_DESKTOP_HOME"} & set(seen)
    assert seen["MANGACOUCH_TRUSTED_PROXIES"] == "10.0.0.1"


def test_the_settings_window_checks_for_a_passcode_without_opening_the_library(state_dir, tmp_path):
    pytest.importorskip("tkinter")
    from mangacouch.desktop.settings_window import passcode_configured

    database = state_dir / "library.sqlite"
    before = database.stat().st_mtime_ns
    assert passcode_configured(database)
    assert database.stat().st_mtime_ns == before
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"not a database")
    assert passcode_configured(garbage)  # unreadable: ask for nothing, serve reports it


def test_the_server_log_rotates_by_size_on_whole_lines(tmp_path):
    path = tmp_path / "logs" / "server.log"
    path.parent.mkdir()
    path.write_bytes(b"previous run\n")
    log = RotatingLog(path, max_bytes=60, backups=2)
    assert (tmp_path / "logs" / "server.log.1").read_bytes() == b"previous run\n"
    lines = [f"line {index:02d} of the server log\n".encode() for index in range(6)]
    for line in lines:
        log.write(line)
    log.close()
    log.write(b"after close\n")  # dropped, not raised
    files = [path.with_name(name) for name in ("server.log.2", "server.log.1", "server.log")]
    # Two 26-byte lines fit in 60 bytes; the previous run's log was the oldest and is gone.
    assert [file.read_bytes() for file in files] == [b"".join(lines[at : at + 2]) for at in (0, 2, 4)]
    assert not path.with_name("server.log.3").exists()


def test_the_server_log_keeps_writing_when_a_rotation_is_refused(tmp_path, monkeypatch):
    from mangacouch.desktop import service as service_module

    path = tmp_path / "server.log"
    log = RotatingLog(path, max_bytes=40, backups=1)
    real_replace = os.replace

    def refused(source, target):  # Windows, while another program holds the file open
        raise PermissionError(13, "in use", str(source))

    monkeypatch.setattr(service_module.os, "replace", refused)
    log.write(b"a" * 30 + b"\n")
    log.write(b"b" * 30 + b"\n")
    assert path.read_bytes() == b"a" * 30 + b"\n" + b"b" * 30 + b"\n"
    monkeypatch.setattr(service_module.os, "replace", real_replace)
    log.write(b"c" * 30 + b"\n")  # retried a quarter of max_bytes later
    log.close()
    assert path.read_bytes() == b"c" * 30 + b"\n"
    assert path.with_name("server.log.1").read_bytes() == b"a" * 30 + b"\n" + b"b" * 30 + b"\n"


def test_service_rotates_the_server_output_while_it_runs(state_dir, desktop_home, monkeypatch):
    from mangacouch.desktop import service as service_module

    ready = threading.Event()
    write = RotatingLog.write

    def observed_write(self, data):
        write(self, data)
        if data.strip() == b"ready":
            ready.set()

    monkeypatch.setattr(RotatingLog, "write", observed_write)
    # A stand-in server: writes 200 numbered lines to stdout and stderr, then one more as it stops.
    script = (
        "import sys\n"
        "for index in range(200):\n"
        "    (sys.stdout if index % 2 else sys.stderr).write(f'line {index:03d}\\n')\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n"
        "print('stopped cleanly', flush=True)\n"
    )
    monkeypatch.setattr(service_module, "cli_command", lambda: [sys.executable, "-c", script])
    monkeypatch.setattr(service_module, "port_in_use", lambda host, port: False)  # the script binds nothing
    monkeypatch.setattr(service_module, "LOG_MAX_BYTES", 300)
    monkeypatch.setattr(service_module, "LOG_BACKUPS", 50)
    log = desktop_home / "logs" / "server.log"
    service = Watched()
    try:
        service.start(DesktopConfig(state_dir=str(state_dir), port=free_port()))
        assert ready.wait(30), "Server output did not reach the log"
    finally:
        service.stop()
    assert service.status == Status(State.STOPPED)
    files = sorted(log.parent.glob("server.log.*"), key=lambda file: -int(file.suffix[1:]))
    assert len(files) > 3 and all(file.stat().st_size <= 300 for file in [*files, log])
    lines = b"".join(file.read_bytes() for file in [*files, log]).decode().splitlines()
    assert sorted(lines[:200]) == [f"line {index:03d}" for index in range(200)]
    assert lines[200:] == ["ready", "stopped cleanly"]


def test_settings_retry_after_config_write_failure_keeps_initialized_auth(
    tmp_path, settings_window, monkeypatch
):
    from mangacouch.desktop import config as config_module
    from mangacouch.desktop.settings_window import SAVED

    window = settings_window(DesktopConfig())
    state = tmp_path / "retry-state"
    window.state_dir.set(str(state))
    window.books_dir.set(str(tmp_path / "retry-books"))
    window.passcode.set("first-passcode")
    window.passcode_repeat.set("first-passcode")
    save = config_module.save_config
    monkeypatch.setattr(
        config_module, "save_config", lambda value: (_ for _ in ()).throw(OSError("disk full"))
    )
    window.save()
    assert "disk full" in window.error.get()
    assert Auth(Library(state / "library.sqlite")).configured()
    monkeypatch.setattr(config_module, "save_config", save)
    window.save()
    assert window.result == SAVED and window.error.get() == ""
    assert load_config().state_dir == str(state)


def test_log_reopens_after_rotation_open_failure(tmp_path, monkeypatch):
    path = tmp_path / "server.log"
    log = RotatingLog(path, max_bytes=4)
    log.write(b"old\n")
    original = Path.open
    blocked = True

    def opening(self, mode="r", *args, **kwargs):
        if self == path and mode == "wb" and blocked:
            raise OSError("temporary sharing lock")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opening)
    log.write(b"loses this line\n")
    blocked = False
    log.write(b"new\n")
    log.close()
    assert path.read_bytes() == b"new\n"
    assert path.with_name("server.log.1").read_bytes() == b"old\n"
    log.write(b"after close")
    assert path.read_bytes() == b"new\n"


def test_relative_xdg_config_home_uses_same_base_as_autostart(tmp_path, monkeypatch):
    from mangacouch.desktop import config as config_module

    monkeypatch.setattr(config_module.sys, "platform", "linux")
    monkeypatch.delenv("MANGACOUCH_DESKTOP_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    assert (
        config_module.app_directory().parent
        == autostart.autostart_path().parent.parent
        == Path.home() / ".config"
    )


def test_start_retries_a_recently_occupied_port_and_stop_cancels_pending_retry(state_dir, monkeypatch):
    from mangacouch.desktop import service as module

    timers = []

    class Timer:
        def __init__(self, interval, callback):
            self.callback, self.cancelled = callback, False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(module.threading, "Timer", Timer)
    monkeypatch.setattr(module, "port_in_use", lambda *_: True)
    service = Service()
    config = DesktopConfig(state_dir=str(state_dir))
    service.start(config)
    assert service.status.state == State.STARTING
    timers[-1].callback()
    assert len(timers) == 2
    pending = timers[-1]
    service.stop()
    pending.callback()
    assert pending.cancelled and len(timers) == 2 and service.status.state == State.STOPPED
    monkeypatch.setattr(module, "START_RETRY_TIMEOUT", 0)
    service.start(config)
    assert service.status.state == State.FAILED


def test_server_loads_numpy_before_stdin_watch(state_dir):
    from helpers import python

    script = """
import sys
from mangacouch import __main__ as cli

def watch(stop):
    assert "numpy" in sys.modules, "NumPy must load before the blocking stdin reader"

cli.stop_on_stdin_eof = watch
sys.argv = ["mangacouch", *sys.argv[1:]]
raise SystemExit(cli.main())
"""
    result = python("-c", script, *DesktopConfig(state_dir=str(state_dir)).serve_arguments(None), "--check")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["startup_ready"]


def test_stdin_close_cancels_upgrade_before_server_start(tmp_path):
    import sqlite3
    import subprocess
    from contextlib import closing

    database = tmp_path / "old.sqlite"
    root = tmp_path / "books"
    root.mkdir()
    from mangacouch.database import Library

    Library.initialize(database)
    script = """
import sys,time
from mangacouch import database, migrate, schema
from mangacouch.__main__ import main

migrate.SCHEMA_VERSION = database.SCHEMA_VERSION = schema.SCHEMA_VERSION = 2

def slow(context):
    context.db.execute("CREATE TABLE cancellation_probe(value)")
    print("IN_UPGRADE", flush=True)
    while True:
        context.checkpoint()
        time.sleep(.01)
migrate.STEPS[1] = slow
sys.argv = ["mangacouch", *sys.argv[1:]]
raise SystemExit(main())
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            "--db",
            str(database),
            "--root",
            str(root),
            "serve",
            "--origin",
            "http://localhost:8780",
            "--stop-on-stdin-eof",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    ready = threading.Event()
    output = []

    def read():
        assert process.stdout
        for line in process.stdout:
            output.append(line)
            if b"IN_UPGRADE" in line:
                ready.set()

    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert ready.wait(15), b"".join(output)
        assert process.stdin
        process.stdin.close()
        assert process.wait(5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        reader.join(5)
        assert process.stdout
        process.stdout.close()
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='cancellation_probe'").fetchall()


def test_windows_shutdown_waits_for_cleanup_and_handles_cancelled_session(monkeypatch):
    from concurrent.futures import Future
    from types import SimpleNamespace

    from mangacouch.desktop import shutdown

    calls = []

    class Native:
        def __init__(self, name):
            self.name = name

        def __call__(self, *args):
            calls.append((self.name, args))
            return True

    native = SimpleNamespace(
        ShutdownBlockReasonCreate=Native("block"), ShutdownBlockReasonDestroy=Native("unblock")
    )
    monkeypatch.setattr(shutdown.ctypes, "WinDLL", lambda *a, **k: native, raising=False)
    icon = SimpleNamespace(_hwnd=123, _message_handlers={})

    def quit():
        calls.append(("cleanup", ()))
        future = Future()
        future.set_result(None)
        return future

    shutdown.windows_hooks(icon, quit)
    assert icon._message_handlers[0x11](0, 0) == 1
    # The reason Windows shows is in the tray's language, like its other text.
    assert calls[0] == ("block", (123, t("shutdown.reason")))
    icon._message_handlers[0x16](0, 0)
    assert [call[0] for call in calls] == ["block", "unblock"]
    icon._message_handlers[0x11](0, 0)
    icon._message_handlers[0x16](1, 0)
    assert [call[0] for call in calls] == ["block", "unblock", "block", "cleanup", "unblock"]


def test_serve_turns_windows_session_end_into_a_graceful_stop(monkeypatch):
    import ctypes
    from ctypes import wintypes  # noqa: F401  # loaded before the platform is faked
    from types import SimpleNamespace

    from mangacouch import __main__ as cli

    registered = []

    def register(callback, add):
        registered.append((callback, add))
        return True

    kernel32 = SimpleNamespace(SetConsoleCtrlHandler=register)
    prototypes = []

    def winfunctype(restype, *argtypes):
        prototypes.append((restype, argtypes))
        return lambda function: function

    monkeypatch.setattr(ctypes, "WINFUNCTYPE", winfunctype, raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **options: kernel32, raising=False)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(cli, "_CONSOLE_HANDLERS", [])
    finished = threading.Event()
    stops = []

    def stop() -> None:
        stops.append(True)
        finished.set()

    callback = cli.stop_on_windows_session_end(stop, finished)
    assert callback is not None and registered == [(callback, True)]
    assert [callback] == cli._CONSOLE_HANDLERS  # kept alive while Windows may call it
    assert prototypes == [(wintypes.BOOL, (wintypes.DWORD,))]
    assert kernel32.SetConsoleCtrlHandler.argtypes[1] is wintypes.BOOL
    # Ctrl+C and Ctrl+Break are left to Python; close, logoff and shutdown stop the server
    # gracefully and hold the process until it has finished.
    assert callback(0) is False and callback(1) is False and not stops
    for event in (2, 5, 6):
        finished.clear()
        assert callback(event) is True and finished.is_set()
    assert len(stops) == 3


def test_serve_registers_no_console_handler_off_windows(monkeypatch):
    import ctypes
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from mangacouch import __main__ as cli

    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux"))
    windll = MagicMock()
    monkeypatch.setattr(ctypes, "WinDLL", windll, raising=False)
    assert cli.stop_on_windows_session_end(lambda: None, threading.Event()) is None
    windll.assert_not_called()


@pytest.mark.skipif(sys.platform != "darwin", reason="AppKit delegate requires macOS")
def test_macos_termination_defers_reply_until_cleanup_finishes():
    import importlib
    from types import SimpleNamespace

    from mangacouch.desktop.shutdown import macos_delegate

    appkit = importlib.import_module("AppKit")

    callbacks, replies = [], []

    def quit(*, completed):
        callbacks.append(completed)

    delegate = macos_delegate(quit)
    application = SimpleNamespace(replyToApplicationShouldTerminate_=replies.append)
    assert delegate.applicationShouldTerminate_(application) == appkit.NSTerminateLater
    assert replies == []
    callbacks.pop()()
    assert replies == [True]
    assert macos_delegate(quit) is not delegate


@pytest.mark.parametrize("blocker", ["port", "library_lock"])
def test_service_recovers_when_previous_instance_releases_its_resource(state_dir, monkeypatch, blocker):
    from mangacouch.desktop import service as module
    from mangacouch.files import FileLock

    monkeypatch.setattr(module, "START_RETRY_SECONDS", 0.05)
    port = free_port()
    resource = socket.socket() if blocker == "port" else FileLock(state_dir / "library.sqlite.downloads.lock")
    if isinstance(resource, socket.socket):
        resource.bind(("127.0.0.1", port))
        resource.listen()
    service = Watched()
    try:
        service.start(DesktopConfig(state_dir=str(state_dir), port=port))
        with service.changed:
            assert service.changed.wait_for(lambda: bool(service.status.detail), 15)
        assert service.status.state == State.STARTING
        resource.close()
        assert service.wait_for({State.RUNNING, State.FAILED}, 15).state == State.RUNNING
    finally:
        resource.close()
        service.stop()


def test_start_retries_append_to_one_log_and_keep_the_previous_run(state_dir, monkeypatch, desktop_home):
    from mangacouch.desktop import service as module
    from mangacouch.files import FileLock

    monkeypatch.setattr(module, "START_RETRY_SECONDS", 0.05)
    log = module.log_path()
    log.parent.mkdir(parents=True)
    log.write_text("previous run\n", encoding="utf-8")
    attempts: list[bool] = []
    enough = threading.Event()

    class Counted(module.RotatingLog):
        def __init__(self, *args, **kwargs):
            attempts.append(kwargs.get("fresh", True))
            if len(attempts) >= 4:
                enough.set()
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "RotatingLog", Counted)
    lock = FileLock(state_dir / "library.sqlite.downloads.lock")
    service = Watched()
    try:
        service.start(DesktopConfig(state_dir=str(state_dir), port=free_port()))
        # Four attempts would have rotated "previous run" past the three kept backups.
        assert enough.wait(20), (attempts, service.changes, log.read_text())
        lock.close()
        assert service.wait_for({State.RUNNING, State.FAILED}, 30).state == State.RUNNING
    finally:
        lock.close()
        service.stop()
    assert attempts[0] is True and not any(attempts[1:])
    assert log.with_name("server.log.1").read_text(encoding="utf-8") == "previous run\n"
    assert not log.with_name("server.log.2").exists()
    current = log.read_text(encoding="utf-8")
    assert current.count(module.RETRY_MARKER) == len(attempts) - 1
    assert "Another download worker is using this library" in current


def test_failure_detail_ignores_errors_before_the_last_retry(tmp_path):
    from mangacouch.desktop.service import RETRY_MARKER

    log = tmp_path / "server.log"
    log.write_text(
        f"ValueError: Another download worker is using this library\n{RETRY_MARKER}\nstarting\n",
        encoding="utf-8",
    )
    assert failure_detail(log) == "starting"
    log.write_text(f"OSError: earlier\n{RETRY_MARKER}\nValueError: first\n{RETRY_MARKER}\n", encoding="utf-8")
    assert failure_detail(log) == ""
