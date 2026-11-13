"""Zip the browser extension for a release, stamping the project version into its manifest.

Usage: python scripts/package_extension.py [OUTPUT_DIRECTORY]

pyproject.toml holds the only version. The committed extension/manifest.json carries a development
placeholder; the zip gets `version` (the numeric release part, as Chrome requires) and `version_name`
(the final X.Y.Z project version; pre-release versions are rejected).

Only the files the extension loads are shipped: those the manifest names, the scripts and stylesheets
its pages reference, the modules those scripts import, and each locale's messages.json. Tests, the
README and anything else lying in the directory stay out. Entries are written in a fixed order with a
fixed timestamp and mode, so the same sources always give the same zip.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

REPOSITORY = Path(__file__).resolve().parents[1]
EXTENSION = REPOSITORY / "extension"
# The earliest timestamp a zip entry can hold.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
# <script src="…"> and <link href="…"> in the pages, and `from "./…"` / `import "./…"` in the scripts.
PAGE_REFERENCE = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"'#?]+)["']""")
MODULE_REFERENCE = re.compile(r"""\b(?:from|import)\s*["'](\.{1,2}/[^"']+)["']""")


def _local(base: str, reference: str) -> str | None:
    """The extension-relative path of a reference made from file `base`, or None if it is external."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", reference, re.I) or reference.startswith("/"):
        return None
    parts: list[str] = []
    for part in (PurePosixPath(base).parent / reference).parts:
        if part == "..":
            if not parts:
                raise SystemExit(f"{base} refers outside the extension: {reference}")
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def shipped_files(extension: Path = EXTENSION) -> list[str]:
    """The extension-relative paths that go into the zip, sorted."""
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    pending = [
        manifest["action"]["default_popup"],
        manifest["options_ui"]["page"],
        *manifest.get("icons", {}).values(),
        *manifest["action"].get("default_icon", {}).values(),
    ]
    files = {"manifest.json"}
    locales = extension / "_locales"
    files.update(path.relative_to(extension).as_posix() for path in locales.glob("*/messages.json"))
    if f"_locales/{manifest['default_locale']}/messages.json" not in files:
        raise SystemExit(f"The default locale {manifest['default_locale']!r} has no messages.json")
    while pending:
        name = pending.pop()
        if name in files:
            continue
        path = extension / name
        if not path.is_file():
            raise SystemExit(f"The extension refers to a missing file: {name}")
        files.add(name)
        pattern = {".html": PAGE_REFERENCE, ".js": MODULE_REFERENCE}.get(path.suffix)
        if pattern:
            text = path.read_text(encoding="utf-8")
            pending.extend(local for reference in pattern.findall(text) if (local := _local(name, reference)))
    return sorted(files)


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def package(output: Path) -> Path:
    with (REPOSITORY / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    numeric = re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version)
    if numeric is None:
        raise SystemExit(f"Project version {version!r} is not a final X.Y.Z release")
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = numeric.group()
    manifest["version_name"] = version
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"mangacouch-extension-{version}.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for name in shipped_files():
            if name == "manifest.json":
                data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
            else:
                data = (EXTENSION / name).read_bytes()
            stream.writestr(_entry(name), data)
    print(archive)
    return archive


if __name__ == "__main__":
    package(Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve())
