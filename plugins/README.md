# Drop-in plugins

Put `.py` files here to add plugins without touching the MangaCouch package. On startup the server
discovers every non-`_`-prefixed `.py` file in this directory (resolved relative to the install's
base directory — i.e. next to `config.toml`), imports it, and registers any concrete subclass of the
four plugin ABCs it finds.

Plugins are also discoverable via `importlib.metadata` entry points (group `mangacouch.plugins`) for
pip-installed plugin packages.

## The four plugin types (§5.4)

| Type | ABC | Entry point |
|------|-----|-------------|
| **Login** | `LoginPlugin` | `do_login(ctx) -> httpx.Client` |
| **Download** | `DownloadPlugin` | `matches(url)`, `download(ctx) -> DownloadResult` |
| **Metadata** | `MetadataPlugin` | `get_tags(ctx) -> MetadataResult` |
| **Script** | `ScriptPlugin` | `run_script(ctx) -> ScriptResult` |

Every plugin returns a validated `PluginInfo` from `plugin_info()`: a unique `namespace`, a `type`,
declared `parameters` (mark secrets with `secret=True` — they are encrypted at rest, §5.6), an
optional advisory `cooldown` (enforced server-side by the rate limiter, §5.3), an optional
`login_from` (a Login plugin whose session is injected), and — for download plugins — a `url_regex`.

**Trust model:** a single owner trusts all plugins; they run **in-process** (no sandbox).

## ML extension points (API surface only, §5.5)

Two hooks are designed but unimplemented in v1:
- **Auto-tagging** — a Metadata/Script plugin that runs a model (e.g. a WD-Tagger ONNX model) over
  the cover/pages and returns namespaced tags.
- **Auto-translation** — `PageProcessHook.process_page(ctx) -> PageProcessResult`, a hook in the
  image-serving path that can **replace** a page image or **attach overlay data** for the browser
  to render.

See `sample_uppercase_metadata.py.example` for a minimal working plugin.
