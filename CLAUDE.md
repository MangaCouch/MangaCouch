# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MangaCouch is a self-hosted comic library for one owner: a Python server (FastAPI, SQLite) that imports CBZ/ZIP/PDF books, serves a React web app, exposes OPDS 1.2 and a Komga-compatible API, and downloads E(x)Hentai galleries.

- `src/mangacouch/` — the server and the `mangacouch` CLI; `desktop/` is the Windows/macOS tray launcher, which runs `serve` as a child process.
- `web/` — the React + TypeScript frontend. Vite builds it into `src/mangacouch/web/`, which is git-ignored build output.
- `extension/` — a Chrome MV3 extension in plain JavaScript (no build step) that queues downloads with a `downloads` API key.
- `tests/` (pytest), `web/tests/` (Node test runner and Playwright), `scripts/` and `deploy/` (packaging, Docker).

User-facing behaviour is documented in `docs/install.md`, `docs/library.md` and `docs/clients.md`; `README.md` and `README.zh-CN.md` mirror each other. Update them when behaviour changes. `docs/development.md` is the reference for checks, CI and releases.

## Commands

Requires Python 3.12–3.15, uv and Node.js 26+. Run everything from the repository root.

```sh
uv sync --locked
npm --prefix web ci
npm --prefix web run build     # serve refuses to start without the bundle
mkdir -p data/state data/books
uv run mangacouch --db data/state/library.sqlite --root data/books init
uv run mangacouch --db data/state/library.sqlite auth-init
uv run mangacouch --db data/state/library.sqlite serve --origin http://localhost:8780
```

There is no Vite dev server or proxy: after editing `web/src`, rebuild and reload. The Python tests that serve the web shell, the Playwright tests and packaging all use this bundle too.

Checks, as CI and `docs/development.md` run them:

```sh
uv run ruff check .
uv run ruff format --check .
npm --prefix web run typecheck:python    # Pyright; installed through web/package.json, configured in pyproject.toml
npm --prefix web run lint                # ESLint (react-hooks rules), zero warnings allowed
npm --prefix web run build               # tsc --noEmit, then vite build
uv run pytest -q -n 4 --dist worksteal   # full Python suite; -n 0 runs serially for debugging
npm --prefix web test                    # component tests in jsdom
npm --prefix web run test:integration    # React app against the real ASGI app; uses .venv or MANGACOUCH_TEST_PYTHON
npm --prefix web run test:browser:ci     # Playwright against the existing bundle; test:browser rebuilds first
node --test extension/*.test.mjs
uv build && uv run python scripts/check_packages.py dist
```

Single tests:

```sh
uv run pytest -q tests/test_downloads.py::test_name
uv run pytest --lf -q                                   # rerun the last failures
cd web && node --import ./tests/register.mjs --test --test-name-pattern="words" tests/reader.test.mjs
cd web && npx playwright test tests/browser/app.spec.ts --project=desktop -g "words"
```

For download and queue work, the fast loop is `uv run pytest -q tests/test_downloads.py tests/test_download_faults.py tests/test_download_archives.py` and `npm --prefix web run test:downloads`.

- CI runs only on pushes to `main` or by hand (`gh workflow run ci.yml --ref <branch>`). It does not run `ruff format --check` and does not lint `scripts/`, so run the Ruff commands above before pushing.
- Playwright needs its browser once (`npx --prefix web playwright install chromium`) or `MANGACOUCH_BROWSER_CHANNEL=chrome` to use an installed Chrome.
- Some Python tests start local HTTP listeners and need to bind loopback ports.
- Formatting: Ruff for Python (line length 110), `npm --prefix web run format` (Prettier) for `web/`.

## Backend architecture

**Composition.** `__main__.py` is the CLI; every command prints JSON. `serve` builds `FileLibrary(Library(db), root, limits)` and hands it to `http.create_app`, which constructs every service (`Downloads`, `Review`, `SidecarSync`, `Scanner`, `Removals`, `Duplicates`, `Organization`, ...), groups them in the `Services` dataclass (`services.py`) and mounts the routes. Each `api_*.py` exports a `*_router(services)` factory; `source_api.py`, `opds.py` and `komga.py` install theirs with `install_*` functions. Tests build the same app through `create_app`.

**Request boundary.** Every request passes the `Boundary` ASGI middleware in `http.py` before any route. It checks the Host allow-list, enforces CSRF for cookie sessions (a non-GET needs a matching `Origin` and the `X-MangaCouch-Request: 1` header), authenticates (session cookie, `Bearer` API key, or HTTP Basic under `/opds` and `/komga/`), applies key scopes (`readonly` keys get GET/HEAD only; `downloads` keys get `POST /api/downloads` and `GET /api/auth/me` only), verifies that the books folder is present, caps JSON bodies at 512 KiB (`/api/import` streams instead) and adds the security headers. A new route that must keep working while the books folder is offline has to be added to `ROOT_EXEMPT_PREFIXES` or `ROOT_EXEMPT_PATHS`.

**Errors.** Services raise the domain exceptions in `errors.py` (`NotFound`, `InvalidInput`, `RevisionConflict`, `LibraryBusy`, `RateLimited`), each carrying a stable `code`; `install_error_handlers` turns them into `{"error": code, "message": text}`. Server messages are English only, and the web app localizes by code. `web/tests/error-codes.test.mjs` scans the Python sources with regular expressions for code literals and fails unless each one has Chinese and English text in `web/src/api.ts` (or `web/src/sourceErrors.ts` for `SourceError` codes). Adding a backend error code therefore means adding its text there.

**Database.** One SQLite file in WAL mode, no ORM. `Library.connection()` opens a fresh autocommit connection per use; `Library.transaction()` wraps it in `BEGIN IMMEDIATE`. A book stores pydantic documents in JSON columns (`content_json`, `metadata_json`, `personal.state_json`), and the derived search columns and tables (`search_text`, `book_tag`, ...) are rewritten in the same transaction by `write_metadata`. Models derive from `models.Model`, which is strict and forbids extra fields. Edits are optimistic: the caller passes `expected_revision`, and a mismatch raises `RevisionConflict` (HTTP 409). Lists page with opaque keyset cursors (`cursors.py`, the `after` parameter).

**Format changes.** `schema.py` holds the complete current schema and `SCHEMA_VERSION`, and `Library` refuses to open any other version. A change to the schema, or to the shape of JSON already stored in it, is a step appended to `migrate.STEPS` (keyed by the version it upgrades from) together with a `SCHEMA_VERSION` bump; all other code reads and writes only the current format. Upgrades run in one transaction, after a pre-upgrade backup, for any command outside `READ_ONLY` in `__main__.py`. Separately, `schema/native-sidecar.schema.json` must equal `NativeSidecar.model_json_schema(by_alias=True)`; a test compares them.

**Files.** `files.py` holds the crash-safe storage primitives: `LibraryRoot` (the books folder, identified by `.mangacouch-root.json`; safe relative-path resolution; `mutation()` takes the `.mangacouch-operation.lock` file lock), `atomic_write`, `publish_file` (never replaces an existing file, with fallbacks for filesystems lacking no-replace rename or hard links) and `FileLock`. `ingest.FileLibrary` stages an import under `.mangacouch-staging/`, hashes and validates it (`archives.py` checks ZIP and PDF content against `limits.Limits` and reads pages through libvips and PDFium), then publishes it as `<first two hex digits>/<sha256>.<ext>`. Use these helpers, not bare `open` or `rename`, for anything inside the books folder or the state directory.

**Concurrency.** uvicorn runs one worker, and one server owns a library at a time (`Downloads.start` takes `<db>.downloads.lock`). Route work that takes the operation lock, touches library files or writes rows in proportion to the library goes through `await services.bounded(...)`: two slots and a 25-second cooperative deadline, after which the request answers 503 `library_busy`. Short database-only writes are ordinary sync routes. Background work runs on the single `Maintenance` thread (`maintenance.py`), which loops over the `steps` list assembled in `create_app` (scans, bulk jobs, pending removals, sidecar sync, duplicate indexing, cache warm-up) plus an `hourly` purge list, and on the `Downloads` worker pools. Long loops call `check_cancel` with the per-thread callback from `set_thread_cancel`, so deadlines and shutdown can interrupt them. `database.busy(error)` separates a transient SQLite lock (retry, or 503 `database_busy`) from a storage fault.

**Online sources.** `source_http.SourceHTTP` is the only path to E(x)Hentai: it enforces the public-address guard, the usage-bucket model in `source_limits.py` and the halts after bans and rate limits. `source_adapters.Sources` parses pages and API answers. `source_config.SourceVault` stores the settings and Fernet-encrypted cookies (key file `<db>.sources.key`). `downloads.py` is the download queue's state machine; read its module docstring before changing it, because paid archive requests have their own recovery rules. `source_checks.py` and `online.py` refresh metadata from the source.

**Sidecars.** A book's metadata is mirrored to `<archive>.mangacouch.json` (`sidecars.py`, which also detects conflicts with external edits) and, for E(x)Hentai books, to `<archive>.eze.json` (`eze.py`, `eze_cache.py`).

**Light commands.** `__main__.py` imports the modules that load libvips and PDFium (`archives`, `ingest`, `lifecycle`, `snapshots`) inside the commands that need them, so `health`, the container's periodic probe, stays light. A test asserts that `health` loads none of them.

## Frontend architecture

- React 19, strict TypeScript (`noUncheckedIndexedAccess`, `verbatimModuleSyntax`) and plain CSS. There is no router, state or i18n library.
- Routing is hash-based. `navigation.tsx` defines the valid routes and each route's parent; `App.tsx` owns history handling and lazy-loads the pages, passing them `t`, `navigate` and a `guard` ref that a page sets to block navigation while it has unsaved work.
- All API calls go through `request`, `post` and `put` in `api.ts`, which add the CSRF header, throw `ApiError` with the server's code and dispatch `mc-locked` on a 401. `errorMessage(error, t)` is the one path from a failure to user-facing text.
- The UI is bilingual. Each string is written inline as `t("中文", "English")` (`Translate` in `types.ts`), so every new string needs both languages.
- `types.ts` mirrors the backend models by hand; change both sides together.
- Reader settings are per device, in localStorage under `mangacouch:` keys (`device.ts`). Preferences shared by all devices come from the server (`PreferenceContext.tsx`).
- Styles live in `tokens.css` (design tokens), `style.css`, `components.css` and `management.css`. `web/tests/css-lint.test.mjs` fails on a class selector no component uses, an undefined custom property or an unused token, so remove CSS together with the markup that used it.

## Tests

**Python** (`tests/`):

- `conftest.py` provides `files` (a temporary `FileLibrary`), `library`, `auth`, `client` (a `TestClient`; sign in with `helpers.login(client)` and send `helpers.CSRF` headers on writes), `api` (a `TestClient` with a management Bearer key), `queue`, `remote` and `sources` (downloads against `source_fixtures.SourceFixture`, a synthetic E-Hentai), plus `without_maintenance` and `after_maintenance` to control the background thread.
- An autouse fixture makes `SourceHTTP` answer 503 unless a test passes its own transport. Tests never reach a real source or spend real GP; keep it that way.
- `helpers.py` has the builders: `import_book`, `make_zip`, `register_book`, `save_metadata`, `drive` (runs the download workers to completion) and `CRASH_AFTER` for crash-recovery subprocess tests.
- `test_batch_*.py` are regression suites from audit rounds and are not tied to one feature.
- `ResourceWarning` is an error, so close connections and caches. `-m perf` selects the tests that guard query counts and query plans.

**Web** (`web/tests/`):

- `*.test.mjs` run on the Node test runner with Testing Library and jsdom. `register.mjs` installs loader hooks that transpile `.ts` and `.tsx` and replace `radix-ui` and `sonner` with the shims in `radix.mjs` and `sonner.mjs`. `harness.mjs` (`setupFixture`, `renderPage`) boots `fake-backend.js`, an in-memory imitation of the HTTP API. When an endpoint's contract changes, update the fake as well; `backend-contract.test.mjs` pins part of its behaviour.
- `integration/` renders the real `App` with `fetch` forwarded over stdin/stdout to `tests/web_integration.py`, which runs the real ASGI app.
- `browser/*.spec.ts` are Playwright specs with `desktop` and `mobile` projects in the `zh-CN` locale, so they assert Chinese text. Each worker starts its own `tests/ui_browser_server.py`, a synthetic 31-book library (passcode `browser-fixture-only`); with `--showcase` the same server provides the sample library for the README screenshots (`scripts/docs_screenshots.mjs`).

## Packaging and releases

- `pyproject.toml` holds the only version; `uv version --bump patch|minor|major` updates it and `uv.lock`. `extension/manifest.json` keeps a `0.0.0` placeholder that `scripts/package_extension.py` replaces. Only final `vX.Y.Z` tags release; the tag must match `pyproject.toml`.
- The wheel and sdist ship the built web bundle, so build the web app before `uv build`; `scripts/check_packages.py` rejects packages without it. `hatch_build.py` stamps the build identity from `version.py` (`vX.Y.Z-<commit>` for releases, `main-<commit>` otherwise).
- Pinned tooling: TypeScript stays on 6.0.3 because typescript-eslint requires `<6.1.0`, and the `minimatch` override fixes an advisory in the lint toolchain.
