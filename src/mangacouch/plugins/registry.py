"""Plugin discovery + registration (§5.4).

Plugins are discovered from the built-in package, a drop-in ``plugins/`` directory, and
``importlib.metadata`` entry points (group ``mangacouch.plugins``). They register by ``namespace``
(uniqueness enforced); download plugins are additionally indexed by a compiled ``url_regex``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import inspect
import logging
import re
from pathlib import Path

from .base import BasePlugin, DownloadPlugin, LoginPlugin, PluginInfo, PluginType

log = logging.getLogger("mangacouch.plugins")

_BUILTIN_MODULES = (
    "mangacouch.plugins.builtin.ehentai_login",
    "mangacouch.plugins.builtin.ehentai_download",
    "mangacouch.plugins.builtin.ehentai_metadata",
    "mangacouch.plugins.builtin.nhentai_metadata",
    "mangacouch.plugins.builtin.hitomi_metadata",
)


class PluginRegistry:
    def __init__(self) -> None:
        self._by_namespace: dict[str, BasePlugin] = {}
        self._info: dict[str, PluginInfo] = {}
        self._download_regex: list[tuple[re.Pattern[str], str]] = []

    # -- registration -------------------------------------------------------------------------

    def register(self, plugin: BasePlugin) -> None:
        info = plugin.plugin_info()
        if info.namespace in self._by_namespace:
            raise ValueError(f"duplicate plugin namespace: {info.namespace!r}")
        self._by_namespace[info.namespace] = plugin
        self._info[info.namespace] = info
        if isinstance(plugin, DownloadPlugin) and info.url_regex:
            self._download_regex.append((re.compile(info.url_regex), info.namespace))
        log.info("registered %s plugin %r", info.type.value, info.namespace)

    def discover(self, extra_dir: Path | None = None) -> None:
        for module_name in _BUILTIN_MODULES:
            self._load_module(module_name)
        self._load_entry_points()
        if extra_dir is not None and extra_dir.is_dir():
            self._load_directory(extra_dir)

    def _load_module(self, module_name: str) -> None:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            log.exception("failed to import plugin module %s", module_name)
            return
        self._register_module(module)

    def _register_module(self, module) -> None:
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BasePlugin)
                and obj.__module__ == module.__name__
                and not inspect.isabstract(obj)
            ):
                try:
                    self.register(obj())
                except Exception:
                    log.exception("failed to register plugin class %s", obj)

    def _load_entry_points(self) -> None:
        try:
            eps = importlib.metadata.entry_points(group="mangacouch.plugins")
        except Exception:  # noqa: BLE001
            return
        for ep in eps:
            try:
                obj = ep.load()
                self.register(obj() if inspect.isclass(obj) else obj)
            except Exception:
                log.exception("failed to load plugin entry point %s", ep.name)

    def _load_directory(self, directory: Path) -> None:
        for file in sorted(directory.glob("*.py")):
            if file.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"mc_plugin_{file.stem}", file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception:
                log.exception("failed to exec plugin file %s", file)
                continue
            self._register_module(module)

    # -- lookup -------------------------------------------------------------------------------

    def get(self, namespace: str) -> BasePlugin | None:
        return self._by_namespace.get(namespace)

    def info(self, namespace: str) -> PluginInfo | None:
        return self._info.get(namespace)

    def all_info(self) -> list[PluginInfo]:
        return list(self._info.values())

    def of_type(self, plugin_type: PluginType) -> list[BasePlugin]:
        return [
            p for ns, p in self._by_namespace.items() if self._info[ns].type is plugin_type
        ]

    def find_download_plugin(self, url: str) -> DownloadPlugin | None:
        for pattern, namespace in self._download_regex:
            if pattern.search(url):
                plugin = self._by_namespace[namespace]
                if isinstance(plugin, DownloadPlugin):
                    return plugin
        # Fall back to asking each download plugin.
        for plugin in self.of_type(PluginType.DOWNLOAD):
            if isinstance(plugin, DownloadPlugin) and plugin.matches(url):
                return plugin
        return None

    def login_plugin(self, namespace: str) -> LoginPlugin | None:
        plugin = self._by_namespace.get(namespace)
        return plugin if isinstance(plugin, LoginPlugin) else None
