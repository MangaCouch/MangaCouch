# Development

## Checks

Python 3.12 is the minimum supported version. CI checks the latest patch releases of Python 3.12, 3.13, 3.14 and 3.15 on Linux, macOS and Windows; standalone web and extension tests run once on Linux with Python 3.15.

```sh
uv run ruff check src tests scripts deploy
uv run ruff format --check src tests scripts deploy
npm --prefix web run typecheck:python   # Pyright, installed through web/package.json
uv run pytest -q
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web test
npm --prefix web run test:integration
node --test extension/*.test.mjs
uv build
uv run python scripts/check_packages.py dist
```

Some tests start local HTTP listeners and need permission to bind loopback ports.

## Web interface

- The built bundle in `src/mangacouch/web` is committed. CI fails if it differs from a fresh `npm --prefix web run build`.
- Component tests run on the Node test runner with Testing Library and jsdom. Radix and Sonner adapters keep state tests deterministic; Playwright covers their real browser behaviour.
- Browser acceptance: `npm --prefix web run test:browser`. Install its browser first with `npx --prefix web playwright install chromium`, or set `MANGACOUCH_BROWSER_CHANNEL=chrome` to use an installed Chrome.
- `tests/ui_browser_server.py` serves an isolated synthetic library (passcode `browser-fixture-only`). Browser tests and the screenshots in `docs/screenshots` use it.

## Packaging

- `scripts/package_extension.py` zips `extension/` with the project version stamped into its manifest.
- `scripts/check_packages.py` rejects runtime data and private files in the wheel and sdist.
- `scripts/check_installation.py` smoke-checks an installed wheel.
- `scripts/build_standalone.py` freezes the CLI with PyInstaller into `dist/mangacouch-<version>-<platform>.tar.gz` (`.zip` on Windows): `uv sync --group freeze`, then `uv run python scripts/build_standalone.py dist`.
- `scripts/check_standalone.py` smoke-checks a standalone executable or archive as a black box: it imports a CBZ and a PDF, renders a page of each, serves, and checks the web shell, authentication, diagnostics and `health`. The release workflow also runs it against the container image.

## Releases

`pyproject.toml` holds the only version. The wheel, the image tags and the archive names derive from it, and `scripts/package_extension.py` stamps it into the extension zip's manifest (the committed `extension/manifest.json` keeps a `0.0.0` development placeholder).

```sh
uv version --bump patch            # or minor, major, or an explicit version; also updates uv.lock
git commit -am "Release $(uv version --short)" && git push
git tag "v$(uv version --short)" && git push origin "v$(uv version --short)"
```

Publishing a release on github.com with a new `vX.Y.Z` tag works too: it creates the tag, and the workflow attaches the files to that release instead of creating one. Either way the tag must match `pyproject.toml`, and the workflow runs the `release.yml` of the tagged commit.

`.github/workflows/release.yml` reuses the CI run of the tagged commit from its push to `main`, waiting for it if needed, and runs the matrix itself only when the commit has none; a failed CI run stops the release. It then builds and checks the wheel, sdist, extension zip, standalone archives (Linux x64/ARM64, Apple Silicon macOS, Windows x64) and the `linux/amd64` + `linux/arm64` image. Only when all pass does it push `ghcr.io/mangacouch/mangacouch` (`X.Y.Z`, `X.Y`, `latest`) and publish the files with a `SHA256SUMS` file. Running the workflow manually is a dry run that keeps the files as workflow artifacts.

## Pinned tooling

- TypeScript stays on 6.0.3 because typescript-eslint requires `<6.1.0`.
- The `minimatch` override pins 11.0.5 to fix GHSA-g3hv-75wg-m249 in the lint toolchain. Remove it once ESLint and typescript-eslint adopt the patched major version.
