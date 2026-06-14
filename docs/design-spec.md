# MangaCouch — Design Spec (v1)

> **Self-contained build specification.** This document alone contains the requirements and design needed to build MangaCouch v1 — it does not depend on any other file. (Background research and the LANraragi baseline analysis that informed it live separately in the project workspace, but are not required to build from this spec.)
>
> Status 2026-06-14: **approved for build.** Dependency facts verified against PyPI/GitHub on 2026-06-14.

---

## 1. Overview

MangaCouch is a self-hosted, single-process **manga library + e-hentai archiver**. One Python application with an embedded SQLite database and a React PWA reader. The same build runs as a NAS service, a desktop app, or a click-to-run folder on a portable drive.

It is a ground-up rewrite of LANraragi, keeping that project's valuable ideas (on-disk archives as the source of truth, namespaced tags, a typed plugin system, OPDS) while removing its pain points (a separate Redis server, a three-process model, a Perl-on-Windows-needs-WSL story, and a weak file-identity hash).

**Three core flows:**
1. **Archive Download** — a browser extension captures an e(x)hentai gallery URL and sends it to the server, which triggers e-hentai's own **"Archive Download"** feature to fetch the whole gallery as one ZIP.
2. **Organize on-disk** — scan, index, thumbnail, and tag manga already in the manga folder.
3. **Upload & parse** — accept user-uploaded **zip / pdf** (cbz too; zip is encouraged over cbz).

**Out of scope for v1** (deferred by design): BitTorrent; page-by-page image scraping (we use Archive Download only); RAR/CBR; 7z; LANraragi REST-API compatibility; native desktop/mobile wrappers (PWA only); auto-tagging & auto-translation *implementations* (we ship the plugin API surface only, §5.5); multi-user accounts (single owner + one shared read-only passcode, §5.6).

### 1.1 Glossary (e-hentai terms used in this spec)

| Term | Meaning |
|------|---------|
| **gallery** | One manga/doujinshi on e-hentai, addressed by URL `https://e(-\|x)hentai.org/g/<gid>/<token>/` |
| **gid / token** | The numeric gallery id and its access token (from the URL) |
| **e-hentai vs exhentai** | The public site (`e-hentai.org`) vs the members-only "ExHentai" mirror (`exhentai.org`) that requires the `igneous` cookie |
| **GP** | "GP" credits spent to generate an Original archive via the archiver |
| **H@H** | Hentai@Home — the distributed network that serves the prepared archive ZIP (from a host distinct from the gallery host) |
| **archiver.php** | The endpoint that prepares a whole-gallery ZIP for download ("Archive Download") |
| **igneous / ipb_member_id / ipb_pass_hash** | Session cookies; the latter two are login, `igneous` unlocks exhentai |
| **EhTagTranslation** | A community database mapping raw English tags → localized names |

---

## 2. Requirements

Priority: **P0** = v1 must-ship · **P1** = v1 if time · **P2** = optional/later.

### 2.1 Hard rules (non-negotiable)

| # | Rule | How v1 satisfies it |
|---|------|--------------------|
| R1 | **No separate server process** | Embedded SQLite; in-process workers. No Redis/broker. |
| R2 | **Never store absolute paths** | Every on-disk reference is relative to the manga root. |
| R3 | **No symlink dependence** | No feature follows or requires symlinks (works on exFAT/NAS exports). |
| R4 | **Hash once at ingest, cache it; re-hash only changed files** | Identity hash cached keyed by relative path + size + mtime; unchanged files never re-read. |
| R5 | **Bundled/native libs; fastest available** | stdlib `zipfile`, `pyvips`, `pypdfium2`. **zip + pdf mandatory; cbz trivial (=zip); 7z postponed; RAR/CBR dropped.** |
| R6 | **CJK tokenization** | SQLite FTS5 `trigram` tokenizer (+ `LIKE` fallback for 1–2 char queries). |
| R7 | **Windows / macOS / Linux, no WSL** | Single Python 3.14 codebase; click-to-run folder or native installer per OS. |

### 2.2 Functional requirements

**Acquisition (P0):** browser-extension-triggered Archive Download of an e(x)hentai gallery; login/cookie management for both domains; HTTP + SOCKS5 proxy; a SQLite-persisted download queue with priority and resume-of-job; a GP **balance calculator**; a server-side **rate limiter**; tag translation from EhTagTranslation. Default to the **Original** archive with a **Resample** toggle; handle "Insufficient funds" and asynchronous archive preparation gracefully.

**Library & organize (P0):** scan → index zip/pdf/cbz → generate thumbnails → compute both hashes. Namespaced tags (first-class, many-to-many) with localized display. Categories (static + dynamic saved-search); **multi-list favorites**; reading history + logs. Search (quick + fuzzy, `namespace:value` syntax per §5.1), sort-by-time, random. Detail page showing cover, title, author/circle, tags, language, rating, page count, love/read/favorite counts, preview thumbnails, **comments** (username/time/content), similar/related, same-series. Statistics dashboard (P1).

**Reader — web first (P0):** continuous-scroll and paged-slide modes; double-page + single (with cover and wide-page handling); manga RTL / LTR; preload + seamless paging; autoplay timer; fit-to-width/height/container; fullscreen; themes + night mode; touch optimization. **Robustness ("图不能裂" — pages must never break):** per-page placeholders, retry images that failed to load, recover partial/corrupt images, a "load all images" action. Resume/continue-reading + bookmarks; remember default reading settings. Pages **streamed from the archive** with a page cache (no full unpack). **Zoom & floating magnifier — P2 (optional).**

**Privacy, platform, ops (P1):** passcode + auto-lock; image cache. i18n via Crowdin; Keep-a-Changelog discipline; OPDS catalog (P1, after the web reader).

---

## 3. Architecture overview

LANraragi's three cooperating processes (web app + a file-watcher + a Redis-backed job worker, all over a separate Redis server) collapse into **one process** with cooperating in-process subsystems over **embedded SQLite**:

```
┌──────────────────────────── single Python 3.14 process ───────────────────────────┐
│  FastAPI + Uvicorn  ──  REST (auto-generated OpenAPI) · serves the React PWA · OPDS │
│      │                                                                              │
│  ┌───┴────────────┐  ┌───────────────────┐  ┌──────────────────────────────────┐  │
│  │ watchfiles      │  │ ingest workers     │  │ download workers                 │  │
│  │ folder watcher  │  │ (ProcessPool):     │  │ (threading; SQLite-persisted     │  │
│  │ manga/ ↔ DB     │  │ hash·thumb·index   │  │ queue): archiver.php → ZIP       │  │
│  └───┬─────────────┘  └─────────┬──────────┘  └──────────────┬───────────────────┘  │
│      └────────── database/library.sqlite (authoritative) ────┘                      │
└─────────────────────────────────────────────────────────────────────────────────────┘
        manga/  (source of truth, relative paths + sidecars)   ·   cache/  (regenerable)
```

- **Web/API** (FastAPI): REST surface (§6.1) with auto-generated OpenAPI, the React PWA served as static assets, and OPDS 1.2 + Page-Streaming Extension (P1).
- **File watcher** (`watchfiles`): keeps the manga folder in sync with the DB, cross-platform. Replaces a dedicated watcher process.
- **Ingest workers** (`concurrent.futures.ProcessPoolExecutor`): hashing, thumbnailing, indexing — CPU fan-out, uniform across OSes.
- **Download workers** (`threading`): drain the SQLite-persisted download queue; an interval timer re-checks `next_run` (no cron/Celery).

**Invariant:** the manga folder is the **source of truth**; the database is a rebuildable index over it. This makes the system robust, migratable, and safe to re-scan.

### 3.1 Path layout (four configurable roots)

Each root is independently configurable. All internal references are **relative** (R2), so the whole thing relocates across machines and removable drives.

| Root | Holds | Precious? |
|------|-------|-----------|
| **executable** | the application (frozen `--onedir` build, or installed) | n/a |
| **database** | `library.sqlite` (config, content hashes, all metadata/tags/categories/progress) + the secrets keyfile | ✅ back this up |
| **cache** | `search.sqlite` (FTS index), `thumbs.sqlite` (thumbnail **blob store** — one file, not one file per image; see §4), extracted-page cache | ♻️ fully disposable / rebuildable |
| **manga** | the archives (**zip/pdf**, cbz) **+ sidecars** (`<name>.json`, `<name>.mc.json`) | ✅ source of truth |

`config.toml` (next to the executable, or in `database/`) stores the four roots as paths resolved relative to the executable location at startup, plus runtime settings (§6.2). Deleting `cache/` must always be safe — the app rebuilds `thumbs.sqlite` and the FTS index from the authoritative DB + manga folder.

**Removable-media safety:** SQLite opened with `PRAGMA journal_mode=TRUNCATE` (not WAL — WAL's shared-memory mapping is unreliable on exFAT/removable/network filesystems) and `synchronous=FULL` (or `NORMAL`); short, frequently-committed transactions; a UI "stop/eject-safe" action that checkpoints and releases file handles. Recommend exFAT for portable drives (no 4 GB file cap; cross-OS) — and therefore **no symlink reliance** (R3).

### 3.2 Data model (relational)

Single transactional SQLite store. Sketch (not final DDL):

| Table | Key columns | Notes |
|-------|-------------|-------|
| `archive` | `id` PK (xxh3-128 hex), `fingerprint` (indexed), `rel_path`, `size`, `mtime`, `format`, `page_count`, `title`, `original_filename`, `summary`, `rating`, `added_at`, `source_url`, `source_gid`, `source_token`, `cover_status` | `id` = full-file hash (exact identity); `fingerprint` = content hash for dedup (§4) |
| `tag` | `id` PK, `namespace`, `value`, UNIQUE(namespace,value) | first-class; replaces a flat comma-string |
| `archive_tag` | `archive_id`, `tag_id` | many-to-many |
| `tag_translation` | `namespace`, `raw`, `translated`, `intro` | from EhTagTranslation `db.full.json` |
| `category` | `id` PK, `name`, `type` (static\|dynamic), `predicate`, `pinned` | dynamic = a saved search predicate |
| `category_archive` | `category_id`, `archive_id` | static membership |
| `favorite_list` | `id` PK, `name`, `position` | **multi-list favorites** |
| `favorite` | `list_id`, `archive_id`, `added_at` | |
| `progress` | `archive_id` PK, `page`, `updated_at` | "read" = page/page_count > 0.85 |
| `history` | `id` PK, `archive_id`, `opened_at` | reading history + logs |
| `comment` | `id` PK, `archive_id`, `username`, `posted_at`, `content` | e-hentai gallery comments |
| `download_job` | `id` PK, `url`, `gid`, `token`, `domain`, `dltype` (org\|res), `state` (queued\|running\|preparing\|done\|failed), `priority`, `progress`, `gp_cost`, `error`, `next_run`, `created_at` | the SQLite-persisted queue (survives restarts) |
| `plugin_config` | `namespace`, `key`, `value`, `is_secret` | secrets (cookies) encrypted at rest (§5.6) |
| `app_config` | `key`, `value` | centralized typed settings |
| `auth_credential` | `role` (owner\|reader), `passcode_hash`, `api_key_hash` | two-tier auth (§5.6) |
| `archive_fts` *(in `cache/search.sqlite`)* | FTS5 external-content over `title`, `tags_text` | rebuildable; `trigram` tokenizer (§5.1) |

### 3.3 Storage & sidecars

- The archive file is stored **unmodified** — writing into it would change its content hash (R4). All added metadata lives in **two external sidecars** next to the archive:
  - `<name>.json` — **Eze format** (the community-standard e-hentai metadata sidecar; gid, token, title, title_jpn, category, uploader, posted timestamp, filecount, filesize, rating, and namespaced tags).
  - `<name>.mc.json` — **MangaCouch-native** (our archive id, both hashes, source gid/token/url, tags, rating, progress pointer, and ingest provenance).
- The database is the index; the sidecars make the manga folder **self-describing and portable** — re-importing into a fresh database reconstructs everything. No ComicInfo.xml and no in-archive writes (interop is deferred).

### 3.4 Unicode & paths

`str` + `pathlib` + baked-in UTF-8 mode end-to-end. Decode filesystem bytes once at the boundary (`os.fsdecode` with `surrogateescape`). Store the **exact path** (which re-opens the file) and a **normalized display name** separately; normalize to NFC in exactly one place; never double-decode. This deletes an entire class of encoding bugs that a Perl/bytes design forces.

---

## 4. Identity, hashing & dedup

The file's identity must address whole-content, not just a prefix (LANraragi hashed only the first 512 KB, which both false-merged distinct archives sharing a cover/header and false-split re-zips).

- **Primary key = full-file `xxh3-128`**, streamed in chunks. Exact identity, no head-collisions. `xxh3` runs far faster than any disk can supply bytes, so hashing the whole file is IO-bound (fine even on slow USB).
- **Dedup fingerprint = hash of sorted per-entry image digests** (ignore container, compression level, and entry order; skip `__MACOSX`/metadata entries). This makes re-zips and cbz↔zip conversions of the same pages share a fingerprint.
- Both are **cached keyed by `(rel_path, size, mtime)`** (R4); a file is only (re)hashed when new or changed. Reject empty and truncated reads explicitly.
- **Near-duplicate covers** (P1): a real perceptual hash (pHash/dHash) compared by Hamming distance over the hash *bits* — finds re-encoded/resized covers. Prefer computing it via pyvips + numpy to avoid an extra image-library dependency.
- **Thumbnails live in a single SQLite blob store** (`cache/thumbs.sqlite`) — *not* one file per image. A library of thousands of galleries × hundreds of pages would otherwise scatter **millions** of tiny files, which cripples NAS/network shares (per-file stat/open storms, slow backups), wastes cluster slack and risks inode exhaustion on exFAT/portable drives, and chokes sync/antivirus tooling. One indexed table — key `(archive_id, page, size_variant)`, value a ~KB JPEG/WebP (SQLite's fast small-blob sweet spot, where a blob read beats opening a discrete file) — sidesteps all of it. It sits in `cache/`, so it stays disposable and rebuildable, and never touches `library.sqlite`. **By default, covers are generated eagerly at ingest; per-page grid thumbnails are generated lazily on first request** and then cached in the same store (most galleries never need them), keeping the file small. A config knob (`thumbnails.prewarm = full`, §6.2) instead pre-generates every page thumbnail for the whole library as a resumable, low-priority background sweep — for always-on NAS/desktop with idle time. Served with long-lived, immutable HTTP cache headers (the id is content-addressed, so the ETag never changes).

---

## 5. Subsystem design

### 5.1 Search & query syntax

Engine = SQLite **FTS5** with the built-in **`trigram`** tokenizer (CJK-capable, substring matching, ships in CPython 3.14's bundled SQLite on all three platforms with no extension-loading) for fuzzy title/tag text, combined with structured SQL predicates for filters. Add a `LIKE` fallback for 1–2 character queries (trigram needs ≥3 chars). The FTS index lives in `cache/search.sqlite` and is rebuildable from the authoritative DB.

**Query syntax (preserve exactly):**
- comma-separated tokens, AND-combined;
- `namespace:value` (namespace-anchored) vs a bare term (matches across all namespaces);
- **negation** `-term`; **exact match** `"…"` or a trailing `$`;
- **wildcards** `*` / `%` (multiple chars), `?` / `_` (single char);
- **numeric predicates** `pages:>N`, `pages:<=N`, `read:>=N` (resolved against `page_count` / `progress`);
- filters layered on top: category (static → id-list intersect, dynamic → predicate appended), `newonly`, `untaggedonly`, `hidecompleted` (drop archives with progress/pages > 0.85);
- sort by `title` (natural, unicode-collated), `date_added`, `lastread`, or any tag namespace.
- A **saved search is a dynamic category**. Searches are shareable as URL query params.

> Tag namespaces treated as "basic" (don't count an archive as "tagged"): `artist parody series language event group date_added timestamp source` — used by the untagged filter; kept as configuration, not hard-coded.

### 5.2 Tags & organization

- **Namespaced tags** (`artist`, `group`, `parody`/`series`, `character`, `language`, `event`, `source`, `date_added`, `rating`, …), first-class many-to-many.
- **`source:<url>`** is indexed (URL → archive) for **dedup-on-download** (recognize a gallery already fetched). `date_added` auto-tag on import. `rating:` written from the star widget.
- **Tag rules / blacklist** applied during auto-tagging: replace a tag, remove/blacklist a tag, rename a namespace, strip a namespace prefix.
- **Categories:** static (explicit membership list) and dynamic (a saved-search predicate evaluated at query time). **Multi-list favorites** are first-class `favorite_list`s.
- **Tag translation:** ingest EhTagTranslation `db.full.json` into `tag_translation`; translate at display time; can derive zh-Hant from zh-Hans. Refresh on first run and daily (the upstream bot regenerates multiple times per day). Details in Appendix A.

### 5.3 Acquisition layer — Archive Download (P0)

**Browser extension (minimal Manifest V3).** Permissions: `activeTab`, `storage`. A service worker + a popup with one button that reads the active tab's URL and calls the server:
```
POST {server}/api/download        body: { url, catid? }       header: Authorization: Bearer <base64(ownerApiKey)>
```
then polls `GET {server}/api/job/{id}` for state. (CORS enabled server-side.) The extension uses the **owner** key (it triggers a privileged download).

**Server archiver flow** (see Appendix A for the wire details):
1. Parse `gid`/`token` from the URL; detect e-hentai vs exhentai.
2. `GET archiver.php?gid=&token=` — validate the gallery/login; **parse the GP cost and current GP balance here** (the balance calculator).
3. `POST archiver.php` with `dltype=org&dlcheck=Download+Original+Archive` (or `dltype=res` for Resample). Detect "Insufficient funds".
4. Parse `document.location="…"` from the response → the H@H archive URL.
5. `GET <that URL>?start=1` to stream the ZIP. **Retry with backoff** — H@H prepares large archives asynchronously and may first return a "being prepared" page.
6. Store the ZIP into `manga/`, write the Eze + `.mc.json` sidecars, and enqueue ingest.

**GP balance calculator.** Before confirming, surface the parsed Original/Resample GP cost and the account's current GP. **When the balance is short, behavior is configurable; the default is "block & report"** (fail with a clear "out of GP" message); an opt-in mode auto-falls-back to Resample.

**Rate limiter (server-side, enforced).** A central throttle on archiver/download calls: a configurable minimum interval between calls and a per-source concurrency of 1. This protects GP and avoids the e-hentai "excessive pageloads" IP ban. (This is the mechanism that enforces a plugin's advisory `cooldown`.)

**Cookies** (managed by the Login plugin) set on **both** `e-hentai.org` and `exhentai.org`: `ipb_member_id`, `ipb_pass_hash`, `igneous` (exhentai), `nw=1` (skip the content warning). Stored in `plugin_config` with secrets encrypted (§5.6).

**Proxy.** A single optional proxy setting (`http://…` or `socks5h://…`). When set, it applies to **all** acquisition traffic by default — `e-hentai.org`, `exhentai.org`, **and the H@H download host** (a third host that must be reachable, or the ZIP fetch fails). An optional per-host scope override lets users who only need exhentai proxied narrow it. Use **`socks5h://`** (not `socks5://`): the `h` resolves DNS at the proxy, so a censored/poisoned local resolver can't block the host — which also means "proxy only exhentai" is the wrong default (where e-hentai is blocked, it needs the proxy too). One persistent HTTP client with the cookie jar, a realistic User-Agent, and redirect-following.

**Queue.** The `download_job` table is the persistent queue (survives restarts); a `threading` worker drains it by priority; an interval timer reads `next_run` for scheduled re-checks.

### 5.4 Plugin system

Four typed plugins as Python abstract base classes, discovered from a drop-in `plugins/` directory and `importlib.metadata` entry points, registered by `namespace` (uniqueness enforced); download plugins also indexed by a compiled `url_regex`. Plugin metadata is a typed `PluginInfo` (Pydantic) validated at import; contexts and results are typed objects (not loose dicts). **Trust model: single owner trusts all plugins; plugins run in-process** (no sandbox). Configuration is stored per-plugin in `plugin_config`. Named parameters only (no legacy positional style).

| Type | Entry point | v1 implementation |
|------|-------------|-------------------|
| **Login** | `do_login() → http session` | EHentai (both domains; the authenticated session is **cached**, not rebuilt per run) |
| **Download** | `matches(url)`, `provide_url()/download() → archive \| url \| error` | EHentai Archive Download |
| **Metadata** | `get_tags(ctx) → {tags, title, summary} \| error` | EHentai (from gallery data) |
| **Script** | `run_script(ctx) → result` | type defined; no v1 implementation |

A Login plugin returns a configured HTTP session injected (via a `login_from` reference) into the calling Download/Metadata plugin's context. A plugin's `cooldown` is **enforced server-side** by the rate limiter (§5.3).

### 5.5 ML extension points (API surface only)

Designed now, implemented later — both plugin-shaped:
- **Auto-tagging** — a Metadata/Script plugin that runs a model (e.g. a WD-Tagger ONNX model via onnxruntime) over the cover/pages and returns namespaced tags. v1 ships the interface only.
- **Auto-translation** — a **page-processing hook** in the image-serving path that can either (a) **replace** a page image with a translated render, or (b) **attach overlay data** to the page API response for the browser to render. v1 ships the hook signature + the response field only.

### 5.6 Auth & permissions (two-tier)

Single-owner — no multi-user accounts — but **two credential tiers**:
- **Owner** — full read/write + admin (config, downloads, edit, delete, plugins). One owner passcode + one owner API key.
- **Reader (shareable, read-only)** — a separate passcode/API key granting **browse + read + own-progress only**: no edit, delete, download, config, or plugin access. Safe to hand to others.

Passcodes are hashed with **argon2id** (argon2-cffi); API keys are random `secrets` tokens stored only as hashes. A FastAPI dependency enforces role → allowed verbs on every route. Auto-lock + an in-app passcode prompt are the PWA-side privacy features.

**Secrets at rest.** Login cookies and any credential material in `plugin_config` are **encrypted at rest** using a key from a **generated keyfile in `database/`** (created on first run, `0600`). Encryption via the `cryptography` library (Fernet/AES-GCM). The keyfile lives beside the DB so a backup of `database/` is self-sufficient; document that the keyfile is as sensitive as the DB.

### 5.7 Reader & frontend

- Pages are served on demand **from the archive in memory** (no full unpack) into a `diskcache`-backed page cache; optional server-side resize.
- **Natural page sort** via `natsort` + unicode-aware collation (not zero-padding, which mis-sorts ≥5-digit page numbers). Keep a cover-first / credits-last heuristic, but make the keyword lists configurable and unicode-aware.
- Single/double-page (first and last shown alone; a wide/landscape page collapses a spread to single); manga RTL/LTR; fit modes; configurable preload count (default 2, doubled in double-page mode); webtoon continuous scroll.
- **One server-side progress model** per credential; "read" = progress > 85%.
- **Frontend:** React + TypeScript + Vite, shipped as a responsive **PWA**, served as static assets by FastAPI; linted with `eslint-plugin-react-hooks`. Native-only capabilities (true OS-level background blur, OS lock) are accepted losses.

---

## 6. Interfaces

### 6.1 REST API surface (outline)

All under `/api`. Auth: `Authorization: Bearer <base64(apiKey)>` or a session; role (owner/reader) gates write/admin verbs. OpenAPI is auto-generated by FastAPI.

| Area | Endpoints |
|------|-----------|
| Library | `GET /api/archives` (query: `q`, `category`, `sort`, `sortdir`, `page`); `GET /api/archives/{id}`; `GET /api/archives/{id}/pages`; `GET /api/archives/{id}/page?path=`; `GET /api/archives/{id}/thumbnail?page=`; `PUT /api/archives/{id}/metadata` *(owner)*; `DELETE /api/archives/{id}` *(owner)*; `PUT /api/archives/{id}/progress/{page}` |
| Tags | `GET /api/tags/stats` (tag cloud); `GET /api/tags/translate?ns=&value=` |
| Categories | `GET/POST /api/categories` *(POST owner)*; `PUT/DELETE /api/categories/{id}` *(owner)*; `PUT/DELETE /api/categories/{id}/{archiveId}` *(owner)* |
| Favorites | `GET/POST /api/favorites/lists` *(POST owner)*; `PUT/DELETE /api/favorites/{listId}/{archiveId}` |
| Downloads | `POST /api/download {url, catid?}` *(owner)*; `GET /api/jobs`; `GET /api/job/{id}`; `POST /api/job/{id}/priority` *(owner)*; `GET /api/ehentai/balance?url=` (the GP calculator) |
| Upload | `POST /api/upload` (multipart) *(owner)* |
| Plugins | `GET /api/plugins`; `POST /api/plugins/{namespace}/config` *(owner)* |
| Admin | `GET/PUT /api/config` *(owner)*; `POST /api/library/scan` *(owner)*; `POST /api/thumbnails/regen` *(owner)* |
| OPDS (P1) | `GET /api/opds`; `GET /api/opds/{id}`; `GET /api/opds/{id}/pse?page=` |

### 6.2 Configuration (key settings)

Stored in `app_config` (and the four path roots in `config.toml`):
- **Paths:** executable / database / cache / manga (relative).
- **Acquisition:** proxy URL + scope (all-EH \| exhentai-only); default `dltype` (org); GP-short behavior (**block** \| resample-fallback); rate-limit interval + concurrency; EhTagTranslation refresh schedule.
- **Reader:** default mode/direction/fit/preload; theme; language.
- **Thumbnails:** `prewarm` (`lazy` \| `full`) — default `lazy` (covers at ingest, page-grid thumbnails on first open); `full` pre-generates **every** page thumbnail for the whole library as a low-priority background job (a one-off catch-up sweep on enable, plus per-archive at ingest), for always-on NAS/desktop with idle time. Background pass yields to interactive work, is resumable, and honors `max_cache_mb` (optional LRU cap on `thumbs.sqlite`, evicting page thumbnails before covers); changing the variant size invalidates and rebuilds.
- **Auth:** owner passcode + API key; reader passcode + API key; auto-lock timeout. Secrets keyfile path (in `database/`).

---

## 7. Dependencies

**Package manager: `uv`. Python: 3.14** (oldest target Windows = 10/11). Every dependency ships cp314 wheels or is pure-Python on macOS-arm64 / Windows-amd64 / Linux-x86_64 — no source builds. Versions verified 2026-06-14.

| Concern | Pick | Latest | Notes |
|---|---|---|---|
| Package/venv manager | **uv** | 0.11.21 | Rust binary, version-agnostic |
| Web framework / server | **FastAPI** + **Uvicorn** | 0.136.3 / 0.49.0 | auto-OpenAPI; pure-Python |
| Models / validation / settings | **Pydantic v2** + **pydantic-settings** | 2.13.4 / 2.14.1 | cp314 ✅ |
| ORM + migrations | **SQLAlchemy 2** + **Alembic** | 2.0.50 / 1.18.4 | cp314 ✅ |
| File watcher | **watchfiles** | (Rust/abi3) | chosen over watchdog (watchdog lacks a cp314 macOS-arm64 wheel) |
| HTTP client + proxy | **httpx** + **httpx[socks]** | 0.28.1 | use `proxy=`/`mounts=`; SOCKS5h via socksio. Pin 0.28.1 (1.0 unreleased) |
| Images | **pyvips** via `pyvips[binary]` | 3.1.1 / 8.18.3 | **the** image library — fastest bulk thumbnails (abi3). **No Pillow fallback.** Bundled libvips is LGPL-3.0+ — fine under MIT via unmodified dynamic linking |
| PDF | **pypdfium2** | 5.9.0 | Apache/BSD; rasterize pages + page count; in-process (no shell-out) |
| Content hash | **xxhash** (xxh3) | 3.7.0 | cp314 ✅ |
| Perceptual hash (P1) | pyvips + numpy (preferred) | — | avoids pulling Pillow; alternative `imagehash` brings Pillow/scipy |
| Upload parsing | **python-multipart** | 0.0.32 | the Kludex package |
| Passwords / secrets | **argon2-cffi** + **cryptography** | 25.1.0 / (PyCA, abi3) | argon2id for passcodes; `cryptography` (Fernet/AES-GCM) for secrets-at-rest; `secrets` for API keys. (passlib is dead — not used) |
| Archives | stdlib **`zipfile`** (zip/cbz) | — | 7z deferred (would add `py7zr`) |
| Page cache | **diskcache** | — | in `cache/` |
| Lint / type / test | **ruff** / **pyright** / **pytest** | 0.15.17 / 1.1.410 / 9.1.0 | `--target-version py314` |
| Tag DB | **EhTagTranslation** `db.full.json` | daily | pulled via `releases/latest/download/` |
| Frontend | **React + TypeScript + Vite**, PWA | — | `eslint-plugin-react-hooks`; built bundle served by FastAPI |
| Search (future) | **tantivy** (tantivy-py) | 0.26.0 | optional sidecar upgrade only; not v1 |

### 7.1 Packaging & release artifacts
Every tagged release publishes, from CI:
- **PyPI package** — the canonical distribution (`pip` / `uv` / `pipx install mangacouch`); ships sdist + wheel, pulls the verified wheel set, and exposes a `mangacouch` console entry point that serves the API + bundled SPA. For Python users and a clean upgrade path.
- **Docker image → GHCR** (`ghcr.io/mangacouch/mangacouch`), **multi-arch `linux/amd64` + `linux/arm64`** (covers x86 and ARM NAS boxes), tagged `:latest` and `:<version>`; one container running the app + in-process workers, SQLite default.
- **Multi-platform desktop / portable builds** for the three verified targets (**Windows-amd64, macOS-arm64, Linux-x86_64**):
  - **Portable:** PyInstaller **`--onedir`** (runs in place; not `--onefile`), UTF-8 mode baked in, the app folder sitting beside `database/ cache/ manga/`.
  - **Native installer:** Inno Setup/MSI (Windows) · `.pkg` (macOS) · AppImage/deb (Linux).
- CPU-only base build; any future GPU work (auto-tagging) ships as an opt-in add-on, never baked into the base.

### 7.2 Why Python (rationale)
A single static binary is the only axis where Go/Rust beat Python — and a click-to-run folder or installer ships Python invisibly, neutralizing that edge. The decisive factor is a **domain-specific** ecosystem: the e-hentai/Pixiv/imaging/ML libraries are Python-first or Python-only, and the riskiest code (the e-hentai protocol) has no Go/Rust prior art. Hot paths are IO-bound or run in native code (libvips, xxhash) regardless of host language, so interpreter speed never binds. The compile-time safety Go/Rust would give is recovered with strict typing (pyright/pytype) enforced in CI.

---

## 8. Phased build plan

Each phase leaves a working, testable system.

- **Phase 0 — Scaffold.** `uv` project (py3.14); FastAPI skeleton; four path roots + `config.toml`; SQLite schema + Alembic; two-tier auth + secrets keyfile; CI (ruff/pyright/pytest) green on macOS-arm64 + Windows-amd64 + Linux-x86_64.
- **Phase 1 — Organize (flow 2).** `watchfiles` scan → both hashes (cached) → thumbnails (pyvips) → DB index → `.mc.json` sidecar. Tests prove R2/R3/R4, unicode round-trip, and hash false-merge/false-split correctness.
- **Phase 2 — Read API + web reader.** Archive listing; streamed page serving (zip + pdf); FTS5 trigram search with the `namespace:value` syntax; namespaced tags + EhTagTranslation; reading progress. React PWA reader (scroll/slide/double, robustness, resume, natural sort).
- **Phase 3 — Upload (flow 3).** zip/pdf (+cbz) upload, parse, ingest; encourage-zip UX.
- **Phase 4 — Archive Download (flow 1).** Login plugin (cookies, both domains, proxy) → Download plugin (archiver.php → `?start=1` ZIP, GP balance calculator, rate limiter, async-prepare retry) → SQLite job queue → the MV3 browser extension → Eze + `.mc.json` sidecars.
- **Phase 5 — Organize polish.** Categories (static + dynamic); multi-list favorites; history/logs; detail page (comments, similar, same-series); stats; perceptual-hash dedup.
- **Phase 6 — Plugin/ML API surface + OPDS + i18n.** Finalize the four plugin ABCs and the auto-tag/translate hook signatures (no implementations); OPDS + PSE; Crowdin wiring.

---

## 9. Design lineage (informational)

MangaCouch is a rewrite of **LANraragi** (Perl/Mojolicious + Redis). Decisions carried over and decisions reversed, summarized so this spec stands alone:

**Kept:** on-disk archives as the source of truth with a rebuildable DB index; namespaced tags; static + dynamic (saved-search) categories; the four-type plugin contract (login/download/metadata/script) with login-session injection; the `namespace:value` search syntax; OPDS + Page-Streaming Extension; content-hash archive ids; the "read = >85%" semantic.

**Reversed/fixed:** Redis + three-process model → one process + SQLite; the 512 KB-prefix SHA-1 id (false-merges/false-splits) → full-file xxh3 + a content fingerprint; the Perl/Windows `IS_UNIX` fork and `Win32::*` wrappers → `pathlib`, gone; the double-UTF-8 encode/decode dance → `str` + UTF-8 mode; the frozen rebuilt search cache → real indexed queries + FTS5; absolute-path file map → relative paths; advisory-only plugin cooldown → a server-enforced rate limiter; single password + lockdown mode → explicit owner/reader roles; a broken SHA-1-of-cover "duplicate detection" → a real perceptual hash; thumbnail FS-sharding (one file per image, fanned out by id prefix) → a single `cache/thumbs.sqlite` blob store (one file instead of millions — NAS/exFAT/backup-friendly).

---

## Appendix A — e-hentai / exhentai protocol (the parts v1 needs)

**Authentication (cookie jar).** Log-in is cookie-based, set on **both** `e-hentai.org` and `exhentai.org`:
- `ipb_member_id`, `ipb_pass_hash` — the login identity (required).
- `igneous` — required to reach **exhentai**; also unlocks access from some regions.
- `nw=1` — skips the "offensive content warning" interstitial (without it, gallery/token retrieval can break).
- (`star`, `ipb_coppa=0` are optional/forum-related.)
Reuse one persistent HTTP client carrying these cookies + a realistic User-Agent.

**Gallery metadata (optional, for richer sidecars/comments).** The JSON API at `https://api.e-hentai.org/api.php` accepts `{"method":"gdata","gidlist":[[gid,token],…],"namespace":1}` — up to **25 pairs per call**, and politeness requires a short pause (~5s) every few calls. It returns title, title_jpn, category, uploader, posted timestamp, filecount, filesize, rating, and namespaced tags. Gallery **comments** (username/time/content) come from the gallery page HTML. Either source can populate the Eze `<name>.json` and our `<name>.mc.json` sidecars.

**Archive Download (`archiver.php`) — the v1 acquisition path:**
1. From the gallery URL `https://e(-|x)hentai.org/g/<gid>/<token>/`, extract `gid` and `token`; the domain decides e-hentai vs exhentai.
2. `GET {domain}/archiver.php?gid=<gid>&token=<token>` (follow redirects). Detect `"Invalid archiver key"` (bad gid/token) and `"This page requires you to log on."` (bad/missing cookies). The page also shows the **GP cost** and the account's **current GP** → parse for the balance calculator.
3. `POST {domain}/archiver.php?gid=<gid>&token=<token>` (form-urlencoded). For **Original**: `dltype=org&dlcheck=Download+Original+Archive`. For **Resample**: `dltype=res&dlcheck=Download+Resample+Archive`. Detect `"Insufficient funds"`.
4. The response contains a JS redirect; parse `document.location = "(.*)"` → the **H@H archive URL** (on a host distinct from the gallery host — proxy/cookie/redirect handling must cover it).
5. `GET <that URL>?start=1` to stream the ZIP. The `?start=1` is the programmatic equivalent of the "Click Here To Start Downloading" button. Because H@H prepares large archives asynchronously, **retry with backoff** if the first response is a "being prepared" page rather than the ZIP body.

**GP cost model.** Original archives **cost GP** (scales with size). Resample archives are much cheaper (served via H@H). Exact pricing is account/policy-dependent — treat "Resample = cheap, Original = costs GP" as the stable rule and always read the live cost/balance (step 2) rather than assuming.

**Rate / ban limits.** Two separate systems: a per-IP **image quota** (signalled by a `/509.gif` image URL, not an HTTP status) and a harder **"excessive pageloads" IP ban**. The Archive-Download path mostly avoids the per-image quota, but automated archiver abuse can still exhaust GP and trip the pageload ban — hence the server-side rate limiter (§5.3): a configurable minimum interval and per-source concurrency of 1.

**EhTagTranslation database.** `github.com/EhTagTranslation/Database` maps raw English tags → localized names. Pull `db.full.json` via the stable `https://github.com/EhTagTranslation/Database/releases/latest/download/db.full.json` (regenerated multiple times daily; ~4 MB). Shape:
```jsonc
{ "data": [ { "namespace": "female",
              "data": { "lolicon": { "name": "萝莉", "intro": "…", "links": "" } } } ] }
```
To translate `female:lolicon`: find the `data[]` entry with `namespace == "female"`, then `.data["lolicon"].name`. Thirteen namespaces (artist, character, cosplayer, female, group, language, location, male, mixed, other, parody, reclass, rows). Store raw tags; translate at display time. License CC BY-NC-SA 3.0.

**Reference implementations** (for protocol behavior, if needed during build): EhViewer and EhPanda (auth + parsing), and LANraragi's `EHentai.pm` download/login plugins (the cleanest Archive-Download implementation). Note: gallery-dl removed its exhentai extractor around v1.32.3 — pin ≤1.30.x if relied upon.

---

## 10. Open items (small, confirm during build)
- **Resample auto-fallback** when GP is short: implemented as a config toggle, **default = block & report** (confirmed).
- **Secrets at rest:** encrypted via a generated keyfile in `database/` (confirmed).
- **Published name** — resolved: use **MangaCouch** (unified with the GitHub org/repo). PyPI dist + import package + CLI = `mangacouch`; GHCR image = `ghcr.io/mangacouch/mangacouch`.
- **Docker `linux/arm64` wheels** — the dependency set was verified on Linux-**x86_64**; the multi-arch image also needs manylinux **aarch64** wheels for `pyvips[binary]`, `pypdfium2`, `cryptography`, `argon2-cffi`, `watchfiles`. All are expected to ship them — verify in CI, and drop arm64 from the image (amd64-only) if any are missing rather than build from source.
