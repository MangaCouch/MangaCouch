"""Check the built wheel and sdist: no runtime data or local artifacts, and the web bundle present."""

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
        if package.name.endswith(".tar.gz") and not any(
            name.endswith("/schema/native-sidecar.schema.json") for name in names
        ):
            raise SystemExit(f"{package.name} lacks the schema required by its tests")
        forbidden = []
        for name in names:
            path = PurePosixPath(name)
            parts = path.parts
            if (
                any(part in {"__pycache__", "node_modules", ".venv", ".DS_Store"} for part in parts)
                or any(part.endswith("_cache") for part in parts)
                or any(
                    parts[i : i + 2] in {("deploy", "state"), ("deploy", "books")}
                    for i in range(len(parts) - 1)
                )
                or path.name == ".env"
                or (path.name.startswith(".env.") and path.name != ".env.example")
                or path.suffix.lower() in {".cbz", ".zip", ".pdf", ".key", ".pyc"}
                or ".sqlite" in path.name
            ):
                forbidden.append(name)
        if forbidden:
            raise SystemExit(f"{package.name} contains runtime/private data: {forbidden}")
        # The web bundle is git-ignored build output: a package built without it cannot serve the app.
        web = [name for name in names if "/mangacouch/web/" in f"/{name}"]
        if not any(name.endswith("web/index.html") for name in web) or not any(
            name.endswith(".js") for name in web
        ):
            raise SystemExit(f"{package.name} lacks the web bundle; run `npm --prefix web run build` first")
        if not any(name.endswith("mangacouch/_build_version.txt") for name in names):
            raise SystemExit(f"{package.name} lacks its build identity")
        print(f"{package.name}: {len(names)} entries checked")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))
