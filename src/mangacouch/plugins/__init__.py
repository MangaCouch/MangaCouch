"""The typed plugin system (§5.4) and the ML extension points (§5.5)."""

from __future__ import annotations

from .base import (
    DownloadContext,
    DownloadPlugin,
    DownloadResult,
    LoginContext,
    LoginPlugin,
    MetadataContext,
    MetadataPlugin,
    MetadataResult,
    PageProcessContext,
    PageProcessHook,
    PageProcessResult,
    PluginError,
    PluginInfo,
    PluginParam,
    PluginType,
    ScriptContext,
    ScriptPlugin,
    ScriptResult,
)
from .registry import PluginRegistry

__all__ = [
    "DownloadContext",
    "DownloadPlugin",
    "DownloadResult",
    "LoginContext",
    "LoginPlugin",
    "MetadataContext",
    "MetadataPlugin",
    "MetadataResult",
    "PageProcessContext",
    "PageProcessHook",
    "PageProcessResult",
    "PluginError",
    "PluginInfo",
    "PluginParam",
    "PluginRegistry",
    "PluginType",
    "ScriptContext",
    "ScriptPlugin",
    "ScriptResult",
]
