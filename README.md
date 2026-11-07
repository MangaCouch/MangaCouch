# MangaCouch

**English** | [简体中文](README.zh-CN.md)

A self-hosted comic library for one owner: import CBZ/ZIP and PDF files, read them in the browser on any device, and connect your favourite reader app over OPDS or a Komga-compatible API.

![Library](docs/screenshots/en/library.png)

## Features

- **Library** — upload books or drop them in an `import/` folder; GIF, JPEG, WebP and PNG pages.
- **Find things** — tag autocomplete with Chinese tag translations, filters by language, type, rating and reading state, plus static and saved-search collections.
- **Reader** — single page, two-page spread or vertical scroll, left-to-right or right-to-left, autoplay and a page-preview scrubber.
- **Synced** — progress, bookmarks and ratings follow you across devices; reading statistics show what you read and when.
- **Tidy metadata** — review queue, page-level duplicate detection and a `.mangacouch.json` sidecar next to every book.
- **Downloads** — queue E(x)Hentai and nHentai galleries from the web app or the Chrome extension, compare editions, and approve paid archives before they are bought.
- **External readers** — OPDS 1.2 and a Komga API subset, with scoped API keys.
- **Safe by default** — passcode login, crash-safe imports, a seven-day recycle bin and one-click backups.
- Chinese and English interface, light and dark themes, phone-friendly layout.

## Screenshots

<table>
  <tr>
    <td><img src="docs/screenshots/en/detail.png" alt="Book details"></td>
    <td><img src="docs/screenshots/en/reader.png" alt="Two-page reader"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/en/search.png" alt="Tag autocomplete"></td>
    <td><img src="docs/screenshots/en/downloads.png" alt="Downloads with edition choice"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/en/activity.png" alt="Reading activity"></td>
    <td><img src="docs/screenshots/en/dark.png" alt="Dark theme"></td>
  </tr>
</table>

<p align="center"><img src="docs/screenshots/en/mobile.png" alt="Phone layout" width="300"></p>

Screenshots use a synthetic sample library with Chinese, Japanese and English titles.

## Quick start

With Docker Compose (pulls `ghcr.io/mangacouch/mangacouch`):

```sh
cp .env.example .env            # set MANGACOUCH_ORIGIN, UID and GID
mkdir -p deploy/state deploy/books
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch init
docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch auth-init
docker compose --env-file .env -f deploy/compose.yaml up -d
```

From source with Python 3.12–3.15 and [uv](https://docs.astral.sh/uv/) on Apple Silicon macOS, Windows x64 or glibc Linux x64/ARM64 (no compiler or Node.js needed):

```sh
uv sync --locked
mkdir -p data/state data/books
uv run mangacouch --db data/state/library.sqlite --root data/books init
uv run mangacouch --db data/state/library.sqlite auth-init
uv run mangacouch --db data/state/library.sqlite serve --origin http://localhost:8780
```

Then open `http://localhost:8780` and sign in with the passcode you set.

Without Python, download the archive for your platform from [Releases](https://github.com/MangaCouch/MangaCouch/releases) and run `mangacouch/mangacouch` with the same arguments; see [Installation](docs/install.md#standalone-executable).

## Documentation

- [Installation](docs/install.md) — standalone executable, Docker, source, Windows, LAN and HTTPS
- [Library, storage and recovery](docs/library.md) — importing, formats, caches, backups
- [Clients and integrations](docs/clients.md) — API keys, OPDS, Komga, browser extension
- [Development](docs/development.md) — checks and tests

## License

[MIT](LICENSE)
