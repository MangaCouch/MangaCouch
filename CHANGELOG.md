# Changelog

All notable changes to MangaCouch are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Phase 0 — Scaffold.** `uv` project (Python 3.14), FastAPI application factory, the four
  configurable path roots with `config.toml`, the SQLite schema (SQLAlchemy 2 models) + Alembic,
  two-tier owner/reader auth (argon2id passcodes, hashed API keys), secrets-at-rest via a generated
  keyfile (`cryptography`/Fernet), and the `mangacouch` CLI (`init`, `serve`).
- **Phase 1 — Organize.** `watchfiles` folder scan/watch, full-file `xxh3-128` identity hash and a
  content dedup fingerprint (both cached by `(rel_path, size, mtime)`), zip + pdf archive readers,
  `pyvips` thumbnailing into a single `thumbs.sqlite` blob store, the ingest pipeline, and the Eze
  + `.mc.json` sidecars.
- **Phase 2 — Read API + web reader.** Archive listing, streamed page serving (zip + pdf) with a
  `diskcache` page cache, FTS5 `trigram` search with the `namespace:value` query syntax, namespaced
  tags + EhTagTranslation, and the server-side reading-progress model.
- **Phase 3 — Upload.** zip/pdf/cbz upload → parse → ingest.
- **Phase 4 — Archive Download.** EHentai login/download/metadata plugins (cookies, both domains,
  proxy), the `archiver.php` flow with the GP balance calculator, the server-side rate limiter, the
  async-prepare retry, and the SQLite-persisted download queue with a threading worker.
- **Phase 5 — Organize polish.** Static + dynamic (saved-search) categories, multi-list favorites,
  reading history, comments, and the detail surface.
- **Phase 6 — Plugin/ML API surface + OPDS.** The four plugin ABCs, the auto-tag/auto-translate
  hook signatures (interface only), and OPDS 1.2 + the Page-Streaming Extension.

[Unreleased]: https://github.com/MangaCouch/MangaCouch/commits/main
