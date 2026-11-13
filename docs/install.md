# Installation

MangaCouch runs as a standalone executable (with a desktop launcher on Windows and macOS), in Docker, from a source checkout, or from the release wheel. Every method keeps two directories: **state** (database, credentials, configuration) and **books** (the library root). Back them up together.

Releases are published on [GitHub Releases](https://github.com/MangaCouch/MangaCouch/releases) and as the `ghcr.io/mangacouch/mangacouch` image; MangaCouch is not on PyPI. Each release carries:

| File | Contents |
| --- | --- |
| `mangacouch-<version>-linux-x64.tar.gz`, `-linux-arm64.tar.gz`, `-macos-arm64.tar.gz`, `-windows-x64.zip` | Standalone executable, no Python needed |
| `mangacouch-<version>-macos-arm64.dmg` | `MangaCouch.app`, the macOS menu bar app |
| `mangacouch-<version>-py3-none-any.whl`, `mangacouch-<version>.tar.gz`, `constraints.txt` | Wheel, sdist and the locked dependency versions to install them with |
| `mangacouch-extension-<version>.zip` | Chrome extension (see [clients.md](clients.md#browser-extension)) |
| `SHA256SUMS` | Checksums of all of the above |

## Requirements

- **Python 3.12–3.15** for the wheel and source checkout. Standalone builds and the Docker image bring their own.
- **Platforms:** Apple Silicon macOS, Windows x64 and Linux x64/ARM64 with glibc. Native dependencies (libvips, PDFium) ship as wheels, so no compiler or system libraries are needed.
- **Linux standalone builds** need Ubuntu 24.04 or another Linux with glibc 2.39 or later.
- **Not covered:** Intel macOS — the locked Argon2 bindings and `cryptography` releases publish no Intel macOS wheels. Use Docker there.
- **Node.js 26 or later** only to run from a source checkout. The built interface is not checked in; every release file and the Docker image include it.

## Standalone executable

Download the archive for your platform, check it against `SHA256SUMS`, unpack it, and keep the `mangacouch` folder together: the executable loads its libraries from `mangacouch/_internal`.

```sh
tar -xzf mangacouch-<version>-linux-x64.tar.gz
mkdir -p data/state data/books
./mangacouch/mangacouch --db data/state/library.sqlite --root data/books init
./mangacouch/mangacouch --db data/state/library.sqlite auth-init
./mangacouch/mangacouch --db data/state/library.sqlite serve --origin http://localhost:8780
```

On Windows run `mangacouch\mangacouch.exe` with the same arguments. On Windows and macOS the executable started without arguments opens the [desktop launcher](#desktop-launcher-windows-and-macos) instead; with arguments it is the CLI above. The executables are not code-signed: on macOS, clear the download quarantine first with `xattr -dr com.apple.quarantine mangacouch`; on Windows, SmartScreen may ask for confirmation.

## Desktop launcher (Windows and macOS)

On Windows and macOS the server can run from a tray icon instead of a terminal:

- **macOS:** open `mangacouch-<version>-macos-arm64.dmg` and drag `MangaCouch.app` onto `Applications`. The app is not code-signed, so run `xattr -dr com.apple.quarantine /Applications/MangaCouch.app` once before opening it. It lives in the menu bar and has no Dock icon. The `-macos-arm64.tar.gz` archive works the same way: `mangacouch/mangacouch` without arguments opens the launcher.
- **Windows:** double-click `mangacouch\mangacouch.exe` from the Windows archive; the launcher appears in the notification area.
- **From source or the wheel:** install the `desktop` extra (`uv sync --extra desktop`, or see [From the wheel](#from-the-wheel)) and run `mangacouch desktop`. The settings window needs Tk, which the python.org and uv Python builds include. This also works on Linux desktops with a notification area; the Linux standalone archives leave the launcher out.

The first start opens a setup window: choose a data folder (default `~/MangaCouch/state`) and a books folder (default `~/MangaCouch/books`), and set the passcode. To keep using a library created with the CLI, choose its existing data folder instead; the books folder is read from its `runtime.json`.

The icon's menu shows the server status and lets you open the web app (also a left click on Windows), start, stop or restart the server, open the books folder or the server log, toggle launch at login, and quit. **Settings…** changes the port, LAN access, public URL, extra host names, trusted proxy addresses, the thumbnail cache and upload limits; saving changed server options restarts an active server; a manually stopped server stays stopped. With LAN access on, the server listens on every interface, accepts any IP address and `<computer name>.local` as host, and the menu shows the LAN URL to use from other devices.

The launcher keeps its own settings (`desktop.json`), a lock file and `logs/server.log` in `~/Library/Application Support/MangaCouch` (macOS), `%APPDATA%\MangaCouch` (Windows) or `~/.config/mangacouch` (Linux), and starts the server with these settings every time; other `MANGACOUCH_*` variables in the launcher's environment are ignored, with two exceptions. `MANGACOUCH_LOG_LEVEL` still sets the server log level, and when the trusted proxy addresses in **Settings…** are left empty, an inherited `MANGACOUCH_TRUSTED_PROXIES` takes effect without appearing in the settings window. The server log starts afresh at each start (automatic retries of that start append to it) and whenever it reaches 5 MiB, keeping the three previous files as `server.log.1` (newest) to `server.log.3`. If `desktop.json` cannot be read, the launcher says so, keeps the file as `desktop.json.invalid` and starts from the defaults.

Launch at login is a LaunchAgent (`~/Library/LaunchAgents/io.github.mangacouch.desktop.plist`) on macOS, the `MangaCouch` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` on Windows, and on Linux an XDG autostart entry, `$XDG_CONFIG_HOME/autostart/mangacouch.desktop` (by default `~/.config/autostart/mangacouch.desktop`), which GNOME, KDE Plasma, Xfce and other desktops following the XDG Autostart specification run at login. Each records the executable (or Python interpreter) running the launcher when it is turned on; starting the launcher from its new location updates the entry. If it cannot be registered or removed, a notification says why and the menu item switches back; the server starts or restarts regardless. On macOS, move `MangaCouch.app` to `Applications` before turning it on: an app run from the disk image (or from Downloads, which macOS runs from a temporary copy) would not be found at the next login.

Quitting the launcher stops the server gracefully, with a 90-second deadline before forced termination. macOS termination requests and Windows session shutdown also run cleanup, and on Windows the server itself treats a sign-out or shutdown as a stop request; the operating system can still force termination. If the launcher is killed, the server notices the closed pipe and shuts down. Closing during an upgrade cancels and rolls back the active upgrade. A recently occupied port or library lock is retried for up to 90 seconds; Stop cancels that retry.

## From source

```sh
uv sync --locked
npm --prefix web ci
npm --prefix web run build
mkdir -p data/state data/books
uv run mangacouch --db data/state/library.sqlite --root data/books init
uv run mangacouch --db data/state/library.sqlite auth-init
uv run mangacouch --db data/state/library.sqlite serve --origin http://localhost:8780
```

Open `http://localhost:8780`. `auth-init` asks for the passcode twice; when piping input, use UTF-8.

`init` writes a `runtime.json` next to the database that records the library root. When moving state, keep `runtime.json`, the database and `library.sqlite.sources.key` together, and stop the server before copying (or copy a backup instead; see [Backup and restore](library.md#backup-and-restore)).

After changing anything under `web/src`, run `npm --prefix web run build` again. Without a build, `serve` refuses to start and `serve --check` reports the missing interface.

## From the wheel

Install the release wheel into a virtual environment together with its `constraints.txt`, which pins every dependency to the tested version:

```sh
python3 -m venv mangacouch-env
mangacouch-env/bin/pip install --constraint constraints.txt mangacouch-<version>-py3-none-any.whl
mangacouch-env/bin/mangacouch --help
```

Then follow the commands under [From source](#from-source) with `mangacouch` in place of `uv run mangacouch`. For the desktop launcher on Windows or macOS, install `"mangacouch-<version>-py3-none-any.whl[desktop]"` instead and run `mangacouch desktop`.

On Windows, `deploy/install-windows.ps1` does the same in a new directory and smoke-checks the result. It needs x64 Python 3.12–3.15, also on ARM64 Windows (the locked dependencies have no native ARM64 or 32-bit Windows wheels), and installs the CLI only, without the desktop extra:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1 `
  -Wheel mangacouch-<version>-py3-none-any.whl -Constraints constraints.txt `
  -InstallDirectory C:\MangaCouch\env
```

It uses the default `py -3` interpreter; pass `-PythonExecutable` to choose another. The CLI is then `C:\MangaCouch\env\Scripts\mangacouch.exe`.

Run `scripts/check_installation.py` from the source checkout or sdist with the installed Python to smoke-check an installation. It starts a temporary library and server, then checks the web shell, the authentication boundary and diagnostics.

## Docker Compose

Releases publish a `linux/amd64` and `linux/arm64` image to `ghcr.io/mangacouch/mangacouch`, tagged with the version (`1.0.0`), the minor series (`1.0`) and, for the newest release, `latest`. Only final releases are published; there are no pre-release images or files. `deploy/compose.yaml` uses `latest` unless `MANGACOUCH_VERSION` in `.env` pins a version; Compose pulls the image when it is not present locally.

Every push to `main` that passes CI also publishes the `main` tag (and `main-<short commit>`), which follows the development branch with its bug fixes ahead of a release. Set `MANGACOUCH_VERSION=main` in `.env` to track it, and run `docker compose --env-file .env -f deploy/compose.yaml pull` before `up -d` to move to the newest build.

The compose file and `.env.example` live in this repository, so start from a clone. To build the image from source instead of pulling it, run `docker compose --env-file .env -f deploy/compose.yaml build` before the commands below; the Dockerfile builds its wheel and locked constraints from the checkout.

```sh
git clone https://github.com/MangaCouch/MangaCouch.git && cd MangaCouch
cp .env.example .env
mkdir -p deploy/state deploy/books
```

Edit `.env`. Every start uses the values it holds at that moment: a variable that is removed or left empty goes back to its default, whatever an earlier start used.

- `MANGACOUCH_UID` / `MANGACOUCH_GID` — the IDs owning those directories (`id -u` / `id -g` on Linux). Both bind mounts must be writable by them.
- `MANGACOUCH_ORIGIN` — the exact URL clients use. Changing `MANGACOUCH_PORT` also requires updating the port in the origin.
- `MANGACOUCH_ALLOWED_HOSTS` — further host names or IPs clients may use, comma-separated (the same as repeated `--allowed-host`, see [Network access](#network-access)).
- `MANGACOUCH_TRUSTED_PROXIES` — the address or network of the reverse proxy in front, if any (the same as repeated `--trusted-proxy`). Required behind a proxy: otherwise every client counts as the proxy's single address for sign-in throttling; see [Behind a reverse proxy](#behind-a-reverse-proxy).
- `MANGACOUCH_UPLOAD_BYTES` — the largest upload the web app accepts; the default is 2 GiB (`2147483648`). A reverse proxy in front needs a matching body limit (nginx `client_max_body_size`). Larger books can always be dropped into `deploy/books/import/` instead (see [library.md](library.md)).
- `MANGACOUCH_MAX_BYTES` — the largest archive the library imports from an upload or the `import/` folder; unlimited by default, and it also caps the upload limit. Downloads have their own 32 GiB limit. Outside Docker this is `--max-bytes` (or the same variable), given before the command (`mangacouch --max-bytes 34359738368 serve`).
- `MANGACOUCH_CACHE_BYTES` — disk budget of the cover and thumbnail cache in the state directory; the default is 20 GiB (`21474836480`). Shrinking it below the current file empties the cache.
- `MANGACOUCH_LOG_LEVEL` — how much `docker compose logs mangacouch` shows: `info` (default) logs downloads, source checks, imports, scans, sign-ins and source rate limits; `debug` adds every request to E-Hentai/ExHentai and every HTTP request served; `warning` keeps only problems; `error` only failures. Outside Docker, pass `--log-level` before the command (`mangacouch --log-level debug serve`) or set the same variable.

```sh
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch init
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch auth-init
docker compose --env-file .env -f deploy/compose.yaml up -d
```

Run `init` and `auth-init` before starting the service. `/data/state` and `/data/books` are paths inside the container; on the host they are `deploy/state` and `deploy/books`. Back up `deploy/state` together with the original books: stop the service first (`docker compose --env-file .env -f deploy/compose.yaml stop`), or download a backup from the web app instead.

Stopping gives open requests up to 15 seconds and background work up to 60 more; `deploy/compose.yaml` and the desktop launcher allow 90 seconds. With plain `docker run`, pass `--stop-timeout 90` (Docker's default is 10 seconds).

## Upgrading

Version 1.0.0 starts a new library format at schema 1. Pre-1.0 databases and snapshots are refused, even if their schema number is also 1. Existing installations need a separate conversion of a backed-up copy before switching to 1.0.0; changing the version number alone is not a conversion. Keep the original state and its matching older application for rollback.

Take a backup first (**Settings → Maintenance & recovery → Backup & restore**, or `mangacouch --db <database> snapshot <output>`; see [library.md](library.md#backup-and-restore)). Then install the new version — pull the image, or replace the executable or wheel — and start it against the same state and books directories.

For supported schema upgrades within the 1.0 format family, when `snapshot` encounters an older schema, it first writes the original database (including committed WAL changes) to `<output>.pre-upgrade.sqlite`, then upgrades and builds the snapshot. Keep that file for rollback.

When a release changes the library format, the upgrade happens in place at startup, once, before the server starts; restoring an older backup upgrades it the same way. Before changing the schema, startup holds the single-instance lock and saves a unique `<database>.schema-<old-version>-<id>.pre-upgrade.sqlite` copy beside the database. If this backup fails, the upgrade does not run. A failed upgrade leaves the database unchanged. Some upgrades read page details from the archives, so the books folder must be connected; if it is not, startup stops with a message and changes nothing, and the next start after connecting it finishes the upgrade. A large library may take a little longer to start the first time. An older release refuses to open a database upgraded by a newer one, so going back means restoring the backup taken before the upgrade. Commands that only read (`serve --check`, `health`, `list`, `key-list`, `scan-status`, `conflicts`, `page`) never upgrade; they refuse a library in an older format until `serve` (or another command) has upgraded it.

## Moving the library to a new location

There is no move command: copy the folders while the server is stopped. The database never records where the books folder is. It knows the folder by the hidden `.mangacouch-root.json` inside it and stores every book's path relative to it, so a copy that keeps that file is accepted at any path, and a folder without it is refused as unavailable.

1. Stop the server (`docker compose --env-file .env -f deploy/compose.yaml stop`, or **Quit** in the desktop launcher).
2. Copy the books folder with everything in it, hidden files included: `rsync -a old/books/ new/books/`, `cp -a old/books new/books`, or on Windows `robocopy old\books new\books /E /COPY:DAT`. To move the state too, copy the whole state folder the same way (`library.sqlite` with its `-wal`/`-shm` files if present, `library.sqlite.sources.key`, `runtime.json` and the thumbnail cache `.mangacouch-cache-library.sqlite/`).
3. Start with the new books folder in `--root` (or `MANGACOUCH_ROOT`) and the same database in `--db`: `mangacouch --db new/state/library.sqlite --root new/books serve …`.
4. `runtime.json` beside the database still names the old folder (`library_root_hint`), and every command run without `--root` or `MANGACOUCH_ROOT` uses it, the desktop launcher included. Either keep passing `--root`, or with the server stopped replace that value with the new folder's absolute path.
5. Files with preserved sizes and modification times retain their availability stamps. Changed stamps trigger content verification on first access. `mangacouch --db <database> --root <books> doctor` does this for every book at once and should list them all as `available`.

Then delete the old copy. With Docker, only the host side of the bind mount changes (`./books` in `deploy/compose.yaml`); inside the container the folder stays `/data/books`, so `runtime.json` needs no change.

## Network access

By default the server listens on localhost only. `serve` takes its HTTP settings from this run's options and environment only: an option dropped from the command line (or a variable removed from `.env`) is back at its default on the next start.

- **LAN:** add `--host 0.0.0.0` and set `--origin` to the exact URL clients use, including its port. Allow additional names or IPs with repeated `--allowed-host` (or `MANGACOUCH_ALLOWED_HOSTS`, comma-separated); the public origin is always accepted. Host checks also apply to `/`, so `localhost` and `127.0.0.1` are distinct unless both are allowed. The server logs a rejected host name once. `*.example.com` allows subdomains and `0.0.0.0` any IP address; `*` allows any host and so turns off the protection against DNS rebinding, until it is removed and the server restarted.
- **HTTPS:** the server speaks plain HTTP. Put a TLS proxy in front, preserve the `Host` header, and set `--origin https://your-host`. Requests to the origin's own host use the configured scheme for cookie and same-origin checks, so a page served from `http://your-host` is refused. An additional allowed host may be reached either way (a LAN address over plain HTTP, or a second name through the same TLS proxy): its writes must still come from a page on that same host, and the sign-in cookie follows the scheme the browser used.
- **Reverse proxy:** set `--trusted-proxy` (or `MANGACOUCH_TRUSTED_PROXIES`) to the proxy's address; see [Behind a reverse proxy](#behind-a-reverse-proxy).
- **Internet exposure:** also apply connection and request limits at the proxy.

### Behind a reverse proxy

Sign-in attempts are limited to 10 per minute for each client address (each `/64` for IPv6), so that one client cannot use up the limit for everyone. Behind a reverse proxy every request reaches MangaCouch from the proxy's own address. **Unless the proxy is listed as trusted, all clients behind it count as that one address**: ten sign-in attempts from anyone within a minute (a guessing bot, say) lock everyone out of signing in until the minute is over.

List the proxy with repeated `--trusted-proxy` (or `MANGACOUCH_TRUSTED_PROXIES`, comma-separated), as IP addresses or networks such as `172.18.0.0/16`. For requests from those addresses only, the client is the nearest `X-Forwarded-For` entry that is not itself a trusted proxy; from any other address the header is ignored, so it cannot be forged. The proxy must preserve the `Host` header and set `X-Forwarded-For`. Without the setting, the server logs a warning naming the address when a sign-in carries `X-Forwarded-For`; that is the address to list.

- **Proxy on the same machine, no Docker:** `--trusted-proxy 127.0.0.1 --trusted-proxy ::1`.
- **Docker:** the container sees the proxy's address on the Docker network: the proxy container's address, or the network's gateway when the proxy runs on the host and forwards to the published port. List that network, which `docker network inspect deploy_default` shows under `Subnet` (`deploy_default` is the network Compose creates for `deploy/compose.yaml`).

A Caddy proxy in the same Compose project, as `deploy/compose.override.yaml` next to `deploy/compose.yaml` (Caddy keeps the `Host` header and sets `X-Forwarded-For` by itself, and obtains the HTTPS certificate):

```yaml
services:
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
volumes:
  caddy-data:
```

```text
# deploy/Caddyfile
manga.example.com {
    reverse_proxy mangacouch:8780
}
```

```sh
# .env
MANGACOUCH_ORIGIN=https://manga.example.com
MANGACOUCH_TRUSTED_PROXIES=172.18.0.0/16   # the Subnet of deploy_default
```

Start both with `docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.override.yaml up -d`. With nginx instead, forward the same headers:

```nginx
location / {
    proxy_pass http://mangacouch:8780;   # or http://127.0.0.1:8780 from the host
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 2g;             # match MANGACOUCH_UPLOAD_BYTES
}
```

## Forgotten passcode

`change-passcode` sets a new passcode on the server's machine without asking for the old one. It signs out every device (web sessions); API keys keep working. It asks for the new passcode twice.

```sh
# From source (with the wheel: mangacouch instead of uv run mangacouch)
uv run mangacouch --db data/state/library.sqlite change-passcode
# Docker Compose (the service may keep running)
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch change-passcode
# Standalone executable (Windows: mangacouch\mangacouch.exe; macOS app: /Applications/MangaCouch.app/Contents/MacOS/mangacouch)
./mangacouch/mangacouch --db data/state/library.sqlite change-passcode
```

With the desktop launcher, the database is `library.sqlite` in the data folder chosen at setup (by default `~/MangaCouch/state/library.sqlite`).

## Command line

Options before the command apply to every command: `--db` (env `MANGACOUCH_DB`), `--root` (env `MANGACOUCH_ROOT`; needed by `init`, later read from `runtime.json`), `--max-bytes` (env `MANGACOUCH_MAX_BYTES`) and `--log-level` (env `MANGACOUCH_LOG_LEVEL`: `debug`, `info`, `warning` or `error`). An explicit option wins over the variable. `mangacouch --help` lists the commands and `mangacouch <command> --help` each command's options.

| Command | Does |
| --- | --- |
| `init` | Create a library: the database and its books folder (`--root`) |
| `auth-init` | Set the passcode of a new library |
| `serve` | Run the web server: `--host`, `--port`, `--origin`, `--allowed-host`, `--trusted-proxy`, `--upload-bytes`, `--cache-bytes`; `--check` only reports |
| `health` | Exit 0 when the running server answers (the Docker health check) |
| `doctor` | Check the books folder and book availability; warns when the source key is lost |
| `import <file>` / `index <file>` | Copy a file into the library / register a file already in the books folder |
| `scan`, `scan-status`, `scan-pause` | Import from `import/` one batch at a time, show or pause the scan |
| `list [--query] [--limit 50] [--after cursor]` | List a page of books as JSON |
| `key-list` / `key-revoke <id>` | List or revoke API keys |
| `change-passcode [--revoke-keys]` | Reset the passcode, optionally revoking API keys |
| `sync`, `conflicts`, `resolve` | Write pending `.mangacouch.json` files, list and resolve sidecar conflicts |
| `page <book> <index> <file>` | Write one page image to a file |
| `key-create --kind --label` | Create an API key |
| `snapshot <file>` / `restore <file> <folder>` | Back up / restore the database, settings and credentials (see [library.md](library.md#backup-and-restore)) |
| `recover` | Clean up interrupted imports without publishing them |
| `sources-reset --confirm` | Discard source credentials after losing the key, with the server stopped (the web app has the same reset) |
| `desktop` | Open the tray launcher (Windows/macOS; Linux from source or wheel with a notification area) |

Command results use JSON. Operational failures and invalid configuration exit with status 1 and a JSON error. Argument-parser errors and an unready `serve --check` exit with status 2; argument-parser errors use text. The packaged desktop launcher supports Windows and macOS; source and wheel installations can also use Linux notification areas with the desktop extra.

`doctor` writes the observed availability of books to the database. It is not a read-only command.
`list --limit 50 --after <cursor>` returns `{ "items": [...], "after": ... }`; the maximum limit is 1000.
Use `key-list` to list API keys, `key-revoke <id>` to revoke one, or `change-passcode --revoke-keys` to revoke all keys while resetting the passcode.
The desktop network settings accept comma-separated trusted proxy IP addresses or networks, matching `serve --trusted-proxy`.

`MANGACOUCH_MAX_BYTES` is saved in `runtime.json` by `init`. Removing the environment variable later preserves that stored limit. To restore unlimited imports, stop the server, set `limits.max_archive_bytes` to `null` in `runtime.json`, leave the variable unset, then restart.

`MANGACOUCH_HOST` controls the listening address (`127.0.0.1` by default, `0.0.0.0` in the container image). `MANGACOUCH_PORT` is the host's published port in Compose; inside the image it is the listening port, fixed to 8780 by this Compose file. `MANGACOUCH_DESKTOP_LANGUAGE` selects the desktop launcher's language (`en` or `zh`); `MANGACOUCH_DESKTOP_HOME` selects its configuration directory.

Session cookie names include the access port so separate instances on one hostname retain independent sign-ins. Existing sessions may need a one-time sign-in after this naming change. Explicitly allowed LAN hosts can use HTTP even when the public address uses HTTPS; public-address cookies stay Secure, and cookie writes still require the matching Origin.
