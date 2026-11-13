"""Stamp the checked-out build identity into wheels and source distributions."""

import runpy
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version != "editable":
            root = Path(self.root)
            module = runpy.run_path(str(root / "src" / "mangacouch" / "version.py"))
            module["write_build_version"](root)
