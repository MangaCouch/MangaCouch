"""Strings for the tray and settings window, in the system's interface language (Chinese or English)."""

from __future__ import annotations

import locale
import os
import subprocess
import sys
from functools import cache

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "state.stopped": "Stopped",
        "state.starting": "Starting…",
        "state.running": "Running",
        "state.stopping": "Stopping…",
        "state.failed": "Failed to start",
        "state.unconfigured": "Not set up",
        "status": "MangaCouch — {state}",
        "detail.library_missing": "Library not found: {path}; connect the disk to retry",
        "state.library_missing": "Library not found",
        "menu.reselect": "Choose Library Again…",
        "notify.settings_crashed": "Settings window exited unexpectedly (code {code})",
        "settings.proxies": "Trusted proxies (optional)",
        "settings.proxies_hint": "Comma-separated IP addresses or networks, e.g. 127.0.0.1, ::1",
        "detail.port_in_use": "Port {port} is already in use",
        "detail.not_responding": "Not responding",
        "menu.open": "Open MangaCouch",
        "menu.lan": "LAN: {url}",
        "menu.start": "Start Server",
        "menu.stop": "Stop Server",
        "menu.restart": "Restart Server",
        "menu.settings": "Settings…",
        "menu.books": "Open Books Folder",
        "menu.logs": "Show Server Log",
        "menu.login": "Launch at Login",
        "menu.quit": "Quit MangaCouch",
        "notify.failed": "The server stopped: {detail}",
        "shutdown.reason": "MangaCouch is saving download state",
        "notify.login_failed": "Could not turn on Launch at Login: {detail}",
        "notify.login_remove_failed": "Could not turn off Launch at Login: {detail}",
        "notify.config_invalid": "Unreadable launcher settings were kept as desktop.json.invalid; "
        "using the defaults. {detail}",
        "notify.save_failed": "Could not save the launcher settings: {detail}",
        "notify.settings_failed": "Could not open Settings: {detail}",
        "settings.title": "MangaCouch Settings",
        "settings.setup_title": "Set Up MangaCouch",
        "settings.library": "Library",
        "settings.state_dir": "Data folder",
        "settings.state_hint": "Database and settings. Choose an existing data folder to reuse a library.",
        "settings.books_dir": "Books folder",
        "settings.existing": "Existing library; books in {root}",
        "settings.new": "A new library will be created here",
        "settings.passcode": "Passcode",
        "settings.passcode_repeat": "Repeat passcode",
        "settings.passcode_hint": "8–256 characters; used to sign in to the web app.",
        "settings.network": "Network",
        "settings.advanced": "Advanced",
        "settings.port": "Port",
        "settings.lan": "Allow other devices on the network to connect",
        "settings.origin": "Public URL (optional)",
        "settings.origin_hint": "Leave empty unless clients use another address, such as an HTTPS proxy.",
        "settings.hosts": "Extra host names (optional)",
        "settings.hosts_hint": "Comma-separated names clients may use, e.g. nas.home.arpa",
        "settings.storage": "Storage",
        "settings.cache": "Thumbnail cache limit (GiB)",
        "settings.upload": "Upload size limit (GiB)",
        "settings.desktop": "Desktop",
        "settings.login": "Launch MangaCouch at login",
        "settings.open_browser": "Open the web app when MangaCouch starts",
        "settings.browse": "Choose…",
        "settings.cancel": "Cancel",
        "settings.save": "Save",
        "settings.create": "Create Library",
        "settings.restart_note": "Saving restarts the server.",
        "error.state_dir": "Choose a data folder.",
        "error.books_dir": "Choose a books folder.",
        "error.same_dir": "The data folder must not be inside the books folder.",
        "error.passcode_length": "The passcode must be 8–256 characters.",
        "error.passcode_match": "The passcodes do not match.",
        "error.number": "{field} must be a whole number.",
        "error.invalid": "Check the network settings: {detail}",
        "error.failed": "Could not create the library: {detail}",
        "error.save_failed": "Could not save the settings: {detail}",
    },
    "zh": {
        "state.library_missing": "找不到书库",
        "menu.reselect": "重新选择书库…",
        "notify.settings_crashed": "设置窗口异常退出（退出码 {code}）",
        "settings.proxies": "受信任代理（可选）",
        "settings.proxies_hint": "逗号分隔的 IP 地址或网段，例如 127.0.0.1, ::1",
        "state.stopped": "已停止",
        "state.starting": "正在启动…",
        "state.running": "运行中",
        "state.stopping": "正在停止…",
        "state.failed": "启动失败",
        "state.unconfigured": "尚未设置",
        "status": "MangaCouch — {state}",
        "detail.library_missing": "找不到书库：{path}；请连接磁盘后重试",
        "detail.port_in_use": "端口 {port} 已被占用",
        "detail.not_responding": "无响应",
        "menu.open": "打开 MangaCouch",
        "menu.lan": "局域网：{url}",
        "menu.start": "启动服务",
        "menu.stop": "停止服务",
        "menu.restart": "重启服务",
        "menu.settings": "设置…",
        "menu.books": "打开书库文件夹",
        "menu.logs": "查看服务日志",
        "menu.login": "登录时启动",
        "menu.quit": "退出 MangaCouch",
        "notify.failed": "服务已停止：{detail}",
        "shutdown.reason": "MangaCouch 正在保存下载状态",
        "notify.login_failed": "无法开启“登录时启动”：{detail}",
        "notify.login_remove_failed": "无法关闭“登录时启动”：{detail}",
        "notify.config_invalid": "启动器设置无法读取，已保留为 desktop.json.invalid，"
        "现使用默认设置。{detail}",
        "notify.save_failed": "无法保存启动器设置：{detail}",
        "notify.settings_failed": "无法打开设置：{detail}",
        "settings.title": "MangaCouch 设置",
        "settings.setup_title": "设置 MangaCouch",
        "settings.library": "资料库",
        "settings.state_dir": "数据文件夹",
        "settings.state_hint": "存放数据库和配置。选择已有的数据文件夹即可继续使用原资料库。",
        "settings.books_dir": "书库文件夹",
        "settings.existing": "已有资料库，书库位于 {root}",
        "settings.new": "将在此新建资料库",
        "settings.passcode": "书库口令",
        "settings.passcode_repeat": "再次输入口令",
        "settings.passcode_hint": "8–256 个字符，用于登录网页端。",
        "settings.network": "网络",
        "settings.advanced": "高级",
        "settings.port": "端口",
        "settings.lan": "允许局域网中的其他设备访问",
        "settings.origin": "公开地址（可选）",
        "settings.origin_hint": "留空即可；仅当通过 HTTPS 反向代理等其他地址访问时填写。",
        "settings.hosts": "额外允许的主机名（可选）",
        "settings.hosts_hint": "用逗号分隔，例如 nas.home.arpa",
        "settings.storage": "存储",
        "settings.cache": "缩略图缓存上限（GiB）",
        "settings.upload": "单次上传上限（GiB）",
        "settings.desktop": "桌面",
        "settings.login": "登录时自动启动 MangaCouch",
        "settings.open_browser": "启动 MangaCouch 后打开网页",
        "settings.browse": "选择…",
        "settings.cancel": "取消",
        "settings.save": "保存",
        "settings.create": "创建资料库",
        "settings.restart_note": "保存后将重启服务。",
        "error.state_dir": "请选择数据文件夹。",
        "error.books_dir": "请选择书库文件夹。",
        "error.same_dir": "数据文件夹不能位于书库文件夹之内。",
        "error.passcode_length": "口令需为 8–256 个字符。",
        "error.passcode_match": "两次输入的口令不一致。",
        "error.number": "{field}必须是整数。",
        "error.invalid": "请检查网络设置：{detail}",
        "error.failed": "无法创建资料库：{detail}",
        "error.save_failed": "无法保存设置：{detail}",
    },
}


def _system_languages() -> list[str]:
    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=3
            ).stdout
            return [part.strip(' "\n,()') for part in output.split(",") if part.strip(' "\n,()')]
        except (OSError, subprocess.SubprocessError):
            return []
    if sys.platform == "win32":
        import ctypes

        language = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return ["zh" if language & 0x3FF == 0x04 else "en"]
    return [os.environ.get(name, "") for name in ("LC_ALL", "LC_MESSAGES", "LANG")] + [
        locale.getlocale()[0] or ""
    ]


@cache
def language() -> str:
    override = os.environ.get("MANGACOUCH_DESKTOP_LANGUAGE")
    if override in STRINGS:
        return override
    for candidate in _system_languages():
        if candidate.lower().startswith("zh"):
            return "zh"
        if candidate and candidate not in {"C", "POSIX"}:
            return "en"
    return "en"


def t(key: str, **values: object) -> str:
    return STRINGS[language()][key].format(**values)
