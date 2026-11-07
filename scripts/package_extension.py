"""Zip the browser extension for a release, stamping the project version into its manifest.

Usage: python scripts/package_extension.py [OUTPUT_DIRECTORY]

pyproject.toml holds the only version. The committed extension/manifest.json carries a development
placeholder; the zip gets `version` (the numeric release part, as Chrome requires) and `version_name`
(the full project version, which can include a pre-release suffix such as 0.2.0rc1).
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
EXTENSION = REPOSITORY / "extension"


def package(output: Path) -> Path:
    with (REPOSITORY / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    numeric = re.match(r"\d+(?:\.\d+){0,3}", version)
    if numeric is None:
        raise SystemExit(f"Project version {version!r} has no numeric release part for the manifest")
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = numeric.group()
    manifest["version_name"] = version
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"mangacouch-extension-{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        for path in sorted(EXTENSION.rglob("*")):
            name = path.relative_to(EXTENSION).as_posix()
            if path.is_dir() or path.name == ".DS_Store" or path.name.endswith(".test.mjs"):
                continue
            if name == "manifest.json":
                stream.writestr(name, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            else:
                stream.write(path, name)
    print(archive)
    return archive


if __name__ == "__main__":
    package(Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve())
