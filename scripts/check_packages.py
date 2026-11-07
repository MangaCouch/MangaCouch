"""Reject runtime data and local artifacts in the built wheel and source distribution."""

import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def main(directory: Path) -> None:
    packages = [*directory.glob("*.whl"), *directory.glob("*.tar.gz")]
    if not any(p.suffix == ".whl" for p in packages) or not any(p.name.endswith(".tar.gz") for p in packages):
        raise SystemExit("Expected both a wheel and a source distribution")
    for package in packages:
        if package.suffix == ".whl":
            with zipfile.ZipFile(package) as archive:
                names = archive.namelist()
        else:
            with tarfile.open(package) as archive:
                names = archive.getnames()
        forbidden = []
        for name in names:
            path = PurePosixPath(name)
            parts = path.parts
            if (
                any(
                    part in {"__pycache__", "node_modules", "secrets", ".venv", ".DS_Store"} for part in parts
                )
                or any(part.endswith("_cache") for part in parts)
                or any(
                    parts[i : i + 2] in {("deploy", "exports"), ("deploy", "state"), ("deploy", "books")}
                    for i in range(len(parts) - 1)
                )
                or path.name in {"config.toml", ".env"}
                or (path.name.startswith(".env.") and path.name != ".env.example")
                or path.suffix.lower() in {".cbz", ".zip", ".pdf", ".key", ".pyc"}
                or ".sqlite" in path.name
            ):
                forbidden.append(name)
        if forbidden:
            raise SystemExit(f"{package.name} contains runtime/private data: {forbidden}")
        print(f"{package.name}: {len(names)} entries checked")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))
