# Installation

MangaCouch runs as a standalone executable, in Docker, from a source checkout, or from a built wheel on Windows. Every method keeps two directories: **state** (database, credentials, configuration) and **books** (the library root). Back them up together.

## Requirements

- **Python 3.12–3.15 (including 3.15.2).**
- **Platforms:** Apple Silicon macOS, Windows x64 and glibc Linux x64/ARM64. Native dependencies (libvips, PDFium) ship as wheels, so no compiler or system libraries are needed.
- **Not covered:** Intel macOS — the locked Argon2 bindings and `cryptography` releases publish no Intel macOS wheels. Use Docker there.
- **Node.js 26 LTS** only if you change the web interface. The built interface is committed and included in the wheel.

## Standalone executable

Each [GitHub release](https://github.com/MangaCouch/MangaCouch/releases) carries an archive that needs no Python: `mangacouch-<version>-linux-x64.tar.gz`, `-linux-arm64.tar.gz`, `-macos-arm64.tar.gz` or `-windows-x64.zip`. Check it against `SHA256SUMS`, unpack it, and keep the `mangacouch` folder together: the executable loads its libraries from `mangacouch/_internal`.

```sh
tar -xzf mangacouch-<version>-linux-x64.tar.gz
mkdir -p data/state data/books
./mangacouch/mangacouch --db data/state/library.sqlite --root data/books init
./mangacouch/mangacouch --db data/state/library.sqlite auth-init
./mangacouch/mangacouch --db data/state/library.sqlite serve --origin http://localhost:8780
```

On Windows run `mangacouch\mangacouch.exe` with the same arguments. The executables are not code-signed: on macOS, clear the download quarantine first with `xattr -dr com.apple.quarantine mangacouch`; on Windows, SmartScreen may ask for confirmation. The same platform limits as the wheel apply (no Intel macOS, ARM64 or 32-bit Windows builds).

## From source

```sh
uv sync --locked
mkdir -p data/state data/books
uv run mangacouch --db data/state/library.sqlite --root data/books init
uv run mangacouch --db data/state/library.sqlite auth-init
uv run mangacouch --db data/state/library.sqlite serve --origin http://localhost:8780
```

Open `http://localhost:8780`. `auth-init` asks for the passcode twice; when piping input, use UTF-8.

`init` writes a `runtime.json` next to the database that records the library root. When moving state, keep `runtime.json`, the database and `library.sqlite.sources.key` together.

After changing anything under `web/src`, rebuild the interface:

```sh
npm --prefix web ci
npm --prefix web run build
```

## Docker Compose

Releases publish a `linux/amd64` and `linux/arm64` image to `ghcr.io/mangacouch/mangacouch`, tagged with the version (`0.1.0`), the minor series (`0.1`) and `latest`. `deploy/compose.yaml` uses `latest` unless `MANGACOUCH_VERSION` in `.env` pins a version; Compose pulls the image when it is not present locally.

To build the image from source instead, run `docker compose --env-file .env -f deploy/compose.yaml build` before the commands below. The Dockerfile builds its wheel and locked constraints from a clean source checkout; no host `dist/` preparation is needed.

```sh
cp .env.example .env
mkdir -p deploy/state deploy/books
```

Edit `.env`:

- `MANGACOUCH_UID` / `MANGACOUCH_GID` — the IDs owning those directories (`id -u` / `id -g` on Linux). Both bind mounts must be writable by them.
- `MANGACOUCH_ORIGIN` — the exact URL clients use. Changing `MANGACOUCH_PORT` also requires updating the port in the origin.

```sh
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch init
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch auth-init
docker compose --env-file .env -f deploy/compose.yaml up -d
```

Run `init` and `auth-init` before starting the service. `/data/state` and `/data/books` are paths inside the container, not on the host. Back up `deploy/state` together with the original books.

## Windows

`deploy/install-windows.ps1` installs a built wheel with its constraints into a new virtual environment. Use x64 Python 3.12–3.15, including on ARM64 Windows; the pinned dependencies do not supply native ARM64 or 32-bit Windows wheels.

The installer uses the Python 3 launcher default; pass `-PythonExecutable` to select a specific supported interpreter.

Run `scripts/check_installation.py` from the source checkout or sdist to smoke-check an installation. It starts a temporary library and server, then checks the web shell, the authentication boundary and diagnostics.

## Network access

By default the server listens on localhost only.

- **LAN:** add `--host 0.0.0.0` and set `--origin` to the exact URL clients use, including its port. Allow additional names or IPs with repeated `--allowed-host`; the public origin is always accepted. Host checks also apply to `/`, so `localhost` and `127.0.0.1` are distinct unless both are allowed.
- **HTTPS:** the server speaks plain HTTP. Put a TLS proxy in front, preserve the `Host` header, and set `--origin https://your-host`. The application uses the configured scheme for cookie and same-origin checks; it does not trust arbitrary forwarding headers.
- **Internet exposure:** apply connection and login request limits at the proxy.
