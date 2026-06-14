# MangaCouch — project guide for Claude Code

The full design lives in [`docs/design-spec.md`](docs/design-spec.md) (the self-contained build
spec). This file records the **implemented** stack and the day-to-day commands.

## Stack (chosen & in use)

- **Language/runtime:** Python 3.14, managed with **uv**.
- **Web:** FastAPI + Uvicorn (auto OpenAPI at `/docs`), serving the React PWA as static assets.
- **DB:** embedded SQLite via SQLAlchemy 2 (+ Alembic). Three SQLite files: `library.sqlite`
  (authoritative, in `database/`), `search.sqlite` (FTS5 trigram index) and `thumbs.sqlite`
  (thumbnail blob store) — both rebuildable, in `cache/`.
- **Images:** pyvips (`pyvips[binary]`) — the only image library, no Pillow. PDFs via pypdfium2,
  bridged to pyvips through numpy.
- **Hashing:** xxhash (xxh3-128). **Auth:** argon2-cffi + cryptography (Fernet). **HTTP:** httpx
  (+socks). **Watcher:** watchfiles. **Page cache:** diskcache. **Sort:** natsort.
- **Frontend:** React + TypeScript + Vite PWA in [`frontend/`](frontend/); built bundle is copied
  into `src/mangacouch/web/` and served by FastAPI.
- **Extension:** Manifest V3, no build step, in [`extension/`](extension/).

## Layout

```
src/mangacouch/
  config.py state.py app.py cli.py     # config + app context + FastAPI factory + CLI
  db/          models.py base.py        # SQLAlchemy models + engine (removable-media-safe pragmas)
  auth/        crypto.py security.py    # keyfile/Fernet + argon2id + owner/reader roles
  core/        hashing archives imaging thumbnails sidecars naturalsort
  ingest/      pipeline.py watcher.py   # organize flow (scan→hash→thumb→index→sidecar)
  search/      query.py index.py service.py   # namespace:value parser + FTS5 trigram
  tags/        translation.py           # EhTagTranslation
  acquisition/ client ratelimit ehentai queue   # Archive Download + GP calc + queue worker
  plugins/     base.py registry.py builtin/     # 4 typed ABCs + EHentai login/download/metadata
  api/         deps.py serialization.py routers/  # the /api surface
alembic/  tests/  frontend/  extension/  packaging/  docker/  plugins/
```

## Commands

```bash
uv sync --dev                      # install (verified cp314 wheel set, no source builds)
uv run mangacouch init             # first run: config.toml + DB + prints owner/reader credentials
uv run mangacouch serve            # run the server (UI + /api + /docs on :8000)
uv run mangacouch scan             # one-off scan/index of the manga folder
uv run mangacouch refresh-tags     # download EhTagTranslation db.full.json

uv run ruff check .                # lint  (line-length 110, target py314)
uv run pyright                     # type-check (must be 0 errors)
uv run pytest -q                   # tests (R2/R3/R4, hashing, search, full API flow)
uv run alembic upgrade head        # apply migrations (runtime also create_all's on first run)

cd frontend && npm install && npm run build   # builds frontend/dist (copy to src/mangacouch/web)
```

## Conventions / gotchas

- **R2/R3/R4 are load-bearing:** never store absolute paths, never rely on symlinks, hash once and
  cache by `(rel_path, size, mtime)`. Tests enforce these — keep them green.
- The manga folder is the **source of truth**; the DB is a rebuildable index. Deleting `cache/`
  must always be safe.
- API routes are sync `def` (run in a threadpool) — SQLite + worker threads, no async DB.
- Media routes (`/page`, `/thumbnail`, OPDS PSE) accept `?key=<base64(apiKey)>` because `<img>`
  can't send headers. Other routes use `Authorization: Bearer <base64(apiKey)>`.
- pyvips/pypdfium2/diskcache lack type stubs; their dynamic calls are suppressed file-locally for
  pyright (see `core/imaging.py`). Don't broaden those suppressions.
- `src/mangacouch/web/` is a build artifact (gitignored); CI rebuilds it from `frontend/`.
