"""Build the standalone (no Python required) MangaCouch executable for the current platform.

Run with the `freeze` dependency group: `uv run --group freeze python scripts/build_standalone.py dist`.
The result is one archive in the output directory, named mangacouch-<version>-<platform>.(tar.gz|zip),
holding a `mangacouch/` folder with the `mangacouch` executable and its bundled libraries.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
MACHINES = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}
SYSTEMS = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}


def platform_tag() -> str:
    system = SYSTEMS.get(platform.system())
    machine = MACHINES.get(platform.machine().lower())
    if system is None or machine is None:
        raise SystemExit(f"Unsupported build platform: {platform.system()} {platform.machine()}")
    return f"{system}-{machine}"


def build(output: Path) -> Path:
    with (REPOSITORY / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    work = REPOSITORY / "build" / "standalone"
    shutil.rmtree(work, ignore_errors=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(REPOSITORY / "scripts" / "standalone_entry.py"),
            "--name=mangacouch",
            "--onedir",
            "--console",
            "--noconfirm",
            "--clean",
            f"--distpath={work / 'dist'}",
            f"--workpath={work / 'work'}",
            f"--specpath={work}",
            # The committed web bundle and the version that source requests report.
            "--collect-data=mangacouch",
            "--copy-metadata=mangacouch",
            # uvicorn picks its protocol and loop implementations by name at run time.
            "--collect-submodules=uvicorn",
            # pyvips only uses the bundled libvips when its optional _libvips extension imports.
            "--hidden-import=_libvips",
        ],
        check=True,
    )
    bundle = work / "dist" / "mangacouch"
    output.mkdir(parents=True, exist_ok=True)
    name = f"mangacouch-{version}-{platform_tag()}"
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
    print(archive)
    return archive


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve())
