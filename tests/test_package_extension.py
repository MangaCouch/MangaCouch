from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "package_extension", REPOSITORY / "scripts/package_extension.py"
)
assert spec and spec.loader
package_extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_extension)


def test_the_zip_holds_exactly_what_the_extension_loads(tmp_path: Path):
    archive = package_extension.package(tmp_path)
    with zipfile.ZipFile(archive) as stream:
        names = stream.namelist()
        manifest = json.loads(stream.read("manifest.json"))
    assert names == sorted(names)
    assert set(names) == {
        "manifest.json",
        "common.js",
        "popup.html",
        "popup.js",
        "options.html",
        "options.js",
        "style.css",
        "_locales/en/messages.json",
        "_locales/zh_CN/messages.json",
        "icons/icon-16.png",
        "icons/icon-32.png",
        "icons/icon-48.png",
        "icons/icon-128.png",
    }
    assert not any(name.endswith(".test.mjs") or name == "README.md" for name in names)
    assert manifest["version"] != "0.0.0"
    assert manifest["minimum_chrome_version"]


def test_the_zip_is_reproducible(tmp_path: Path):
    first = package_extension.package(tmp_path / "a")
    second = package_extension.package(tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as stream:
        assert {info.date_time for info in stream.infolist()} == {package_extension.ZIP_EPOCH}


def test_stray_files_are_not_shipped_and_missing_references_fail(tmp_path: Path):
    extension = tmp_path / "extension"
    (extension / "_locales/en").mkdir(parents=True)
    (extension / "_locales/en/messages.json").write_text("{}")
    (extension / "manifest.json").write_text(
        json.dumps(
            {
                "default_locale": "en",
                "action": {"default_popup": "popup.html"},
                "options_ui": {"page": "popup.html"},
            }
        )
    )
    (extension / "popup.html").write_text(
        '<link rel="stylesheet" href="style.css" /><a href="https://example.com/">x</a>'
        '<script type="module" src="popup.js"></script>'
    )
    (extension / "popup.js").write_text('import { a } from "./lib/common.js";\n')
    (extension / "lib").mkdir()
    (extension / "lib/common.js").write_text('export { b } from "../shared.js";\n')
    (extension / "shared.js").write_text("export const b = 1;\n")
    (extension / "background.js").write_text('import "./shared.js";\n')
    (extension / "style.css").write_text("")
    (extension / "notes.txt").write_text("not shipped")
    (extension / "popup.test.mjs").write_text("")
    assert package_extension.shipped_files(extension) == [
        "_locales/en/messages.json",
        "lib/common.js",
        "manifest.json",
        "popup.html",
        "popup.js",
        "shared.js",
        "style.css",
    ]
    (extension / "style.css").unlink()
    with pytest.raises(SystemExit, match="style.css"):
        package_extension.shipped_files(extension)
