# MangaCouch

A self-hosted **manga library + e-hentai archiver**. One Python process, an embedded SQLite
database, and a React PWA reader. The same build runs as a NAS service, a desktop app, or a
click-to-run folder on a portable drive.

MangaCouch is a ground-up rewrite of [LANraragi](https://github.com/Difegue/LANraragi) — it keeps
the good ideas (on-disk archives as the source of truth, namespaced tags, a typed plugin system,
OPDS) and removes the pain points (a separate Redis server, a three-process model, the Perl-on-
Windows-needs-WSL story, and a weak file-identity hash).

See [`docs/design-spec.md`](docs/design-spec.md) for the full design.

## Highlights

- **One process, no broker.** Embedded SQLite + in-process workers. No Redis, no Celery.
- **Archives are the source of truth.** The database is a rebuildable index over `data/manga/`.
  Delete `data/cache/` any time — it rebuilds.
- **Strong file identity.** Primary id = full-file `xxh3-128`; a separate content *fingerprint*
  (hash of sorted per-image digests) finds re-zips and cbz↔zip duplicates. Hashes are cached by
  `(rel_path, size, mtime)` and only recomputed when a file changes.
- **CJK-capable search.** SQLite FTS5 `trigram` tokenizer with `namespace:value` query syntax, plus
  a `LIKE` fallback for 1–2 character queries.
- **e-hentai Archive Download.** A browser extension captures a gallery URL; the server drives
  e-hentai's own *Archive Download* (`archiver.php`) to fetch the whole gallery as one ZIP, with a
  GP balance calculator, a server-side rate limiter, proxy + cookie management, and a persistent
  download queue.
- **Robust web reader.** Continuous-scroll and paged modes, single/double page, RTL/LTR, fit modes,
  fullscreen, autoplay, preload, resume, and per-page recovery so *pages never break*.
- **Portable & safe.** Never stores absolute paths, never depends on symlinks, opens SQLite in a
  removable-media-safe mode (`journal_mode=TRUNCATE`). Works on exFAT/NAS exports.

## Requirements

- **Python 3.14+**
- **[uv](https://docs.astral.sh/uv/)** (recommended package/venv manager)

All native dependencies (`pyvips[binary]`, `pypdfium2`, `cryptography`, `argon2-cffi`,
`watchfiles`, `xxhash`) ship prebuilt wheels for macOS-arm64, Windows-amd64 and Linux-x86_64 — no
source builds.

## Quick start

```bash
# install (creates .venv and resolves the verified wheel set)
uv sync

# first run: creates config.toml + the four roots, prints the generated owner/reader credentials
uv run mangacouch init

# run the server (serves the API + bundled SPA on http://localhost:8000)
uv run mangacouch serve
```

Point your manga folder at the `manga` root in `config.toml` (or pass `--manga-root`), drop some
`.zip` / `.pdf` / `.cbz` files in, and MangaCouch scans, hashes, thumbnails and indexes them.

### Configuration

`config.toml` (next to the executable, or in `data/database/`) holds the **four path roots** —
each independently configurable and resolved relative to the executable at startup — plus runtime
settings. The data roots default under `data/` so runtime files never mix with the checkout. See
`config.example.toml` and §6.2 of the design spec.

| Root | Holds | Precious? |
|------|-------|-----------|
| `data/database` | `library.sqlite` + the secrets keyfile | ✅ back this up |
| `data/cache` | `search.sqlite`, `thumbs.sqlite`, the extracted-page cache | ♻️ disposable |
| `data/manga` | the archives + sidecars (`<name>.json`, `<name>.mc.json`) | ✅ source of truth |
| `executable` | the application itself | n/a |

## Development

```bash
uv sync --dev
uv run ruff check .         # lint
uv run pyright              # type-check
uv run pytest               # tests (R2/R3/R4, hashing, search correctness)
```

The frontend lives in [`frontend/`](frontend/) (React + TS + Vite) and the browser extension in
[`extension/`](extension/) (Manifest V3). The built SPA is copied into `src/mangacouch/web/` at
package-build time and served by FastAPI.

## License

MIT © KokomiCat / xiazeyu. Bundled `libvips` (via `pyvips[binary]`) is LGPL-3.0+ and is used
unmodified via dynamic linking.
