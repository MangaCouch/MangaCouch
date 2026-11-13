# Development

## Checks

Python 3.12 is the minimum supported version. Run the checks from the repository root:

```sh
uv run ruff check .
uv run ruff format --check .
npm --prefix web run typecheck:python   # Pyright, installed through web/package.json
npm --prefix web run lint
npm --prefix web run build              # type-checks the web app; the tests and packages serve this bundle
uv run pytest -q -n 4 --dist worksteal
npm --prefix web test
npm --prefix web run test:integration
npm --prefix web run test:browser:ci     # Playwright against the bundle above
node --test extension/*.test.mjs
uv build
uv run python scripts/check_packages.py dist
```

Some tests start local HTTP listeners and need permission to bind loopback ports.

[CI](../.github/workflows/ci.yml) runs on every push to `main` and on demand (`gh workflow run ci.yml --ref <branch>`); other branches and pull requests are not checked automatically. The web bundle is built once and shared between jobs:

- **web** (once, Linux): type-checks and builds the frontend, then uploads the bundle for the checks below.
- **static** (once, Linux, Python 3.15): ESLint, `ruff check src tests`, Pyright, web unit tests, Playwright browser acceptance and the extension tests. It does not run `ruff format --check`, and Ruff does not look at `scripts/` there; the commands above cover both, so run them before pushing.
- **packages** (once, Linux, after web): builds the wheel and sdist, checks them with `scripts/check_packages.py`, exports locked constraints and uploads release packages. It runs independently of static checks.
- **check** (Linux, macOS and Windows × Python 3.12, 3.13, 3.14 and 3.15, after packages): four-process `pytest` with work stealing, React and ASGI integration tests, then installs the shared wheel with its locked constraints and runs `scripts/check_installation.py`.

## Fast local feedback

During an edit, run the affected tests first. For download and queue work:

```sh
uv run pytest -q tests/test_downloads.py tests/test_download_faults.py tests/test_download_archives.py
npm --prefix web run test:downloads
```

After a failure, `uv run pytest --lf -q` reruns the failed cases. Before handing off a broader change,
run the complete suite with `uv run pytest -q -n 4 --dist worksteal`; `-n 0` runs serially for
debugging. Four processes bound memory use and leave room for the app. Tests use isolated temporary
libraries; genuine lock-contention and threaded tests still run inside each worker. Node unit tests
already run files in parallel. Use `npm --prefix web run test:browser:ci` to reuse an up-to-date bundle
instead of rebuilding it with `test:browser` every time; rebuild after frontend edits.

`tests/test_download_faults.py` runs deterministic faults and six seeded sequences (360 transitions)
covering GP depletion/top-up, pause/resume/restart and a real SQLite write lock. It also holds the real
library file lock while pausing/cancelling an import, and holds SQLite beyond the settlement retries.
Failures print the seed/transition history and can be reproduced with `-k` on that parameterized case.
Source responses are synthetic: these tests never spend real GP. Source-budget timing has dedicated
fake-clock tests; queue recovery tests drain the simulated bucket when ending a simulated halt.

CI retains all 12 OS/Python combinations, including integration and package-install checks. Parallel
pytest and bundle reuse reduce repeated work without removing that coverage. Local timing does not
predict GitHub runner timing, especially Windows filesystem latency. On the development Mac, the
same tests passed in 117.52 s serially and 41.50 s with four workers (2026-11-09, Python 3.14).

On Windows runners the check job points `TEMP`/`TMP` at the runner's work drive. Syncing a file on the
system drive takes about 4 ms there against 0.2 ms on the work drive, and the tests sync constantly:
`pytest` took about four minutes with its temporary libraries on `C:` and a little over one on `D:`
(2026-11-10). Defender real-time scanning is already off on those runners, and a ReFS Dev Drive or
eight workers were no faster.

## Web interface

- `npm --prefix web run build` type-checks the app and writes the bundle to `src/mangacouch/web`, which git ignores. CI builds it before the tests, and every release build (wheel, sdist, standalone archives, Docker image) builds it before packaging. `scripts/check_packages.py` fails a wheel or sdist that lacks it, so run the build before `uv build`.
- Component tests run on the Node test runner with Testing Library and jsdom. Radix and Sonner adapters keep state tests deterministic; Playwright covers their real browser behaviour.
- Browser acceptance: `npm --prefix web run test:browser` builds the bundle and runs Playwright in four workers, each against its own `tests/ui_browser_server.py` on a free port. Install its browser first with `npx --prefix web playwright install chromium`, or set `MANGACOUCH_BROWSER_CHANNEL=chrome` to use an installed Chrome.
- `tests/ui_browser_server.py` serves an isolated synthetic library (passcode `browser-fixture-only`) that the browser tests count on. With `--showcase` it serves the invented sample library from `tests/ui_showcase.py` instead.

## Screenshots

The README screenshots in `docs/screenshots/{zh,en}/` come from the showcase library. To re-shoot all of them:

```sh
npm --prefix web run build
MANGACOUCH_BROWSER_CHANNEL=chrome node scripts/docs_screenshots.mjs        # or: ... zh / ... en
```

The script starts a fresh `ui_browser_server.py --showcase` per language on a free port, signs in once, waits for background work to finish, and writes `library`, `search`, `detail`, `downloads`, `activity`, `reader`, `mobile` and `dark`. Without `MANGACOUCH_BROWSER_CHANNEL` it uses Playwright's own Chromium. Check every image against its caption in both READMEs before committing.

## Packaging

- The settings footer identifies tagged releases as `vX.Y.Z-<7-character commit>` and development builds as `main-<7-character commit>`. The build hook embeds this identity in wheels and sdists; standalone builds stamp it before freezing. Release packaging reuses CI's web bundle and rebuilds the Python packages with the release tag.
- Docker builds have no `.git` directory. CI supplies `MANGACOUCH_BUILD_SHA` and `MANGACOUCH_BUILD_REF` as build arguments; for a local image, pass the full `git rev-parse HEAD` result and `refs/heads/main` (or the release's `refs/tags/vX.Y.Z`). A source build without Git or embedded build metadata shows `unknown` for the commit.
- `scripts/package_extension.py` zips `extension/` with the project version stamped into its manifest.
- `scripts/check_packages.py` rejects runtime data and private files in the wheel and sdist.
- `scripts/check_installation.py` smoke-checks an installed wheel.
- `deploy/install-windows.ps1` installs a wheel with its constraints into a new virtual environment on Windows (see [install.md](install.md#from-the-wheel)).
- `scripts/build_standalone.py` freezes MangaCouch with PyInstaller into `dist/mangacouch-<version>-<platform>.tar.gz` (`.zip` on Windows), plus `mangacouch-<version>-macos-arm64.dmg` with `MangaCouch.app` on macOS. On Linux run `uv sync --group freeze`; on Windows and macOS, `uv sync --group freeze --extra desktop` so the build includes the tray launcher. Then `uv run python scripts/build_standalone.py dist`.
- `scripts/check_standalone.py` smoke-checks a standalone executable or archive as a black box: it imports a CBZ and a PDF, renders a page of each, serves, and checks the web shell, authentication, diagnostics and `health`. The release workflow also runs it against the container image.

## Releases

Only final releases are supported: the version is `X.Y.Z` and the tag `vX.Y.Z`. The release workflow stops at once for any other tag (`v1.2.0rc1`, `v1.2`), so there are no pre-releases; try a candidate with a manual dry run on a branch instead.

`pyproject.toml` holds the only version. The wheel, the image tags and the archive names derive from it, and `scripts/package_extension.py` stamps it into the extension zip's manifest (the committed `extension/manifest.json` keeps a `0.0.0` development placeholder).

```sh
uv version --bump patch            # or minor, major, or an explicit version; also updates uv.lock
git commit -am "Release $(uv version --short)" && git push
git tag "v$(uv version --short)" && git push origin "v$(uv version --short)"
```

Publishing a release on github.com with a new `vX.Y.Z` tag works too: it creates the tag, and the workflow attaches the files to that release instead of creating one. Either way the tag must match `pyproject.toml`, and the workflow runs the `release.yml` of the tagged commit.

`.github/workflows/release.yml` reuses the CI run of the tagged commit from its push to `main`, waiting for it if needed, and runs the matrix itself only when the commit has none; a failed CI run stops the release. It then builds and checks the wheel, sdist, `constraints.txt`, extension zip, standalone archives (Linux x64/ARM64 on Ubuntu 24.04, Apple Silicon macOS with its `.dmg`, Windows x64) and the `linux/amd64` + `linux/arm64` image. Only when all pass does it push `ghcr.io/mangacouch/mangacouch:X.Y.Z` and publish the files with a `SHA256SUMS` file. Once the GitHub Release exists it points `X.Y` at that image, and `latest` too when the tag is the highest `vX.Y.Z`, so re-running an older release never moves `latest` back and a failed release never moves either.

| Trigger | Ref | Result |
| --- | --- | --- |
| push | `vX.Y.Z` tag | Release, after the gates above |
| Run workflow (manual) | `vX.Y.Z` tag | Same gates and the same release; re-pushes the image tags and replaces files already attached to that release |
| Run workflow (manual) | branch | Dry run: builds and checks everything, keeps the files as workflow artifacts, publishes nothing |

A tag that is not `vX.Y.Z`, a failed or timed-out CI run, a tag that does not match `pyproject.toml`, or any failed build or smoke check stops the release before anything is published.

`.github/workflows/main-image.yml` runs after each successful CI run of a push to `main`, builds and smoke-checks the same image and pushes it as `ghcr.io/mangacouch/mangacouch:main-<short commit>`. A serialized follow-up job (`scripts/promote_main_image.py`) then points `ghcr.io/mangacouch/mangacouch:main` at the image of the current head of `main` once that image exists, so a server can follow `main` without a release, and CI runs finishing out of order never move `main` back to an older commit. Running it manually on `main` rebuilds the head; on another ref it publishes nothing.

## Pinned tooling

- TypeScript stays on 6.0.3 because typescript-eslint requires `<6.1.0`.
- The `minimatch` override pins 11.0.8 to fix GHSA-g3hv-75wg-m249 in the lint toolchain. Remove it once ESLint and typescript-eslint adopt the patched major version.

CI builds and validates the web bundle, wheel, source archive and desktop-inclusive constraints once, then tests installation across the existing three-OS, four-Python matrix. Release jobs reuse those artifacts from the accepted CI run; expired artifacts cause CI to run again. Browser binaries are cached by Playwright version. Main images use native amd64 and arm64 runners before merging their manifests. The `X.Y` image tag moves only when the released tag is the highest final patch within that minor version, so re-releasing an older patch cannot move it backwards.
