"""Build the standalone (no Python required) MangaCouch executable for the current platform.

Run with the `freeze` dependency group: `uv run --group freeze python scripts/build_standalone.py dist`.
The result is an archive in the output directory, named mangacouch-<version>-<platform>.(tar.gz|zip),
holding a `mangacouch/` folder with the `mangacouch` executable and its bundled libraries. On macOS a
disk image, mangacouch-<version>-macos-<arch>.dmg, holds the MangaCouch.app menu bar app.
Windows and macOS builds need the `desktop` extra for the tray launcher (`uv sync --extra desktop`).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

from mangacouch.version import write_build_version

REPOSITORY = Path(__file__).resolve().parents[1]
MACHINES = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}
SYSTEMS = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}


def platform_tag() -> str:
    system = SYSTEMS.get(platform.system())
    machine = MACHINES.get(platform.machine().lower())
    if system is None or machine is None:
        raise SystemExit(f"Unsupported build platform: {platform.system()} {platform.machine()}")
    return f"{system}-{machine}"


def app_icon(work: Path) -> Path | None:
    """The logo as .icns (macOS) or .ico (Windows) for the executable and app bundle."""
    if sys.platform not in {"darwin", "win32"}:
        return None
    from mangacouch.desktop.icons import logo

    work.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        path = work / "MangaCouch.icns"
        logo(1024, padding=0.08).save(path)
    else:
        path = work / "MangaCouch.ico"
        logo(256).save(path, sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])
    return path


def disk_image(work: Path, image: Path, version: str) -> Path:
    """A compressed, read-only disk image with MangaCouch.app and an Applications link to drag it onto."""
    staging = work / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    # ditto keeps the bundle's symbolic links, permissions and code signatures.
    app = "MangaCouch.app"
    subprocess.run(["ditto", str(work / "dist" / app), str(staging / app)], check=True)
    (staging / "Applications").symlink_to("/Applications")
    image.unlink(missing_ok=True)
    subprocess.run(
        [
            "hdiutil",
            "create",
            "-volname",
            f"MangaCouch {version}",
            "-srcfolder",
            str(staging),
            "-fs",
            "HFS+",
            "-format",
            "UDZO",
            "-ov",
            str(image),
        ],
        check=True,
    )
    return image


def build(output: Path) -> list[Path]:
    write_build_version(REPOSITORY)
    with (REPOSITORY / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    work = REPOSITORY / "build" / "standalone"
    shutil.rmtree(work, ignore_errors=True)
    icon = app_icon(work)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(REPOSITORY / "scripts" / "mangacouch.spec"),
            "--noconfirm",
            "--clean",
            f"--distpath={work / 'dist'}",
            f"--workpath={work / 'work'}",
        ],
        env={**os.environ, "MANGACOUCH_VERSION": version, "MANGACOUCH_ICON": str(icon or "")},
        check=True,
    )
    bundle = work / "dist" / "mangacouch"
    output.mkdir(parents=True, exist_ok=True)
    name = f"mangacouch-{version}-{platform_tag()}"
    archives = []
    if sys.platform == "win32":
        archive = output / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
            for path in sorted(bundle.rglob("*")):
                stream.write(path, Path("mangacouch") / path.relative_to(bundle))
    else:
        # tar keeps the executable bit and the symbolic links PyInstaller creates on macOS.
        archive = output / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(bundle, arcname="mangacouch")
    archives.append(archive)
    if sys.platform == "darwin":
        archives.append(disk_image(work, output / f"{name}.dmg", version))
    for archive in archives:
        print(archive)
    return archives


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve())
