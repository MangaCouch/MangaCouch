# MangaCouch — Decision Log

> Records what is decided and why. All open questions are now **resolved** per the maintainer's [`feedback.md`](feedback.md) (2026-06-14). The consolidated result is in [`design-spec.md`](design-spec.md) — read that to review/approve. This file is the rationale trail.

## Decided (hard constraints)

1. **Minimal deps / easy to ship.** Embedded **SQLite** + embedded search; in-process workers; no Redis/broker. Packaging = **click-to-run folder or installer** (not required to be a single static binary).
2. **Modern server-client.** RESTful API + **React PWA**; NAS / desktop / portable drive. **Not Perl.**
3. **No `tools/openapi.yaml` carry-over;** refer to LANraragi behavior when useful.
4. **Primary purpose = e-hentai/exhentai offline archiving** + manual zip/pdf/cbz import.
5. **Seven hard rules** (R1–R7): Redis must go · never store absolute paths · no symlink dependence · hash-once-and-cache · bundled libs for zip/pdf (+cbz; 7z later) + fast image lib · CJK tokenization · Win/macOS/Linux no-WSL.
6. **Content folder = source of truth; DB = rebuildable index.**
7. **Package manager: `uv`.** **Python 3.14** (oldest target Windows = 10/11).

## Resolved questions

| Q | Topic | Resolution |
|---|-------|-----------|
| **Q1** | Backend language | **Python 3.14, strictly typed** (pyright/pytype). Frontend React+TS. Rationale: click-to-run goal neutralizes Go/Rust's static-binary edge; domain ecosystem (EH/Pixiv/imaging/ML) is Python-first; EH protocol has no Go/Rust prior art; hot paths IO-bound/native. |
| **Q2** | Search | **SQLite FTS5 + built-in `trigram` tokenizer** (CJK-capable, zero extension-loading, all 3 platforms) + `LIKE` fallback for 1–2 char queries. Index lives in `cache/` (rebuildable). **Tantivy sidecar = deferred** upgrade only (cp314 wheels exist; add only if easy + low-risk, per feedback). jieba `simple` ext rejected (needs `load_extension`, disabled on python.org macOS build). |
| **Q3** | Download architecture | **Archive-Download only** (e-hentai `archiver.php` → whole-gallery ZIP). **No BitTorrent** (deferred). **Proxy support required** (HTTP+SOCKS5, per-host so only exhentai is proxied). Broker-free **`threading` + SQLite-persisted queue** (Kapowarr model). |
| **Q4** | Plugin model | **LANraragi typed plugins** (Login / Download / Metadata / Script) as Python ABCs. Kept **simple** (e(x)hentai is the only real source v1). **Trust model: server owner trusts all plugins, in-process** (no sandbox). |
| **Q5** | Storage / metadata | Store **original archive unmodified** (never write into it → preserves hash) + **two sidecars**: `<name>.json` (Eze) and `<name>.mc.json` (MangaCouch-native). **No interop** (no ComicInfo.xml) for now. **Both hashes:** full-file `xxh3` = primary key, content fingerprint = dedup index. |
| **Q6** | Reader | **Do both web reader + OPDS, but build the web reader first.** |
| **Q7** | LANraragi API compat | **No compatibility.** Define our own clean endpoints. (We still borrow Tsukihi's `/api/download_url` *shape* for our own extension — convenience, not compat.) |
| **Q8** | RAR / CBR | **Dropped.** (Avoids the one loose external binary; encourage convert-to-zip on import.) |
| **Q9** | Client form-factor | **Responsive PWA.** Native-only gaps (true background-blur, OS lock) are accepted losses. |
| **Q10** | ML (auto-tag + auto-translate) | **Design as plugins; ship the API surface only in v1.** Auto-tag = Metadata/Script plugin. Auto-translate = image-serving hook that either replaces the page image or passes overlay data to the browser. No implementations v1. |
| **Q11** | Oldest Windows → Python | **Windows 10/11 → Python 3.14** (3.15-ready). |

## Requirements deltas from feedback
- **R5:** **zip + pdf are mandatory**; **cbz** trivial (same code as zip); **7z postponed** (optional). **RAR/CBR dropped** (Q8).
- **Images:** use the **fastest** well-supported native lib — **pyvips** primary (faster than Pillow), Pillow fallback. Must support mac-arm64 / win-amd64 / linux-x86_64.
- **Zoom & inspection (incl. floating magnifier): optional (P2).**
- **Three core flows:** (1) Archive-Download trigger from e(x)hentai, (2) organize on-disk, (3) upload+parse user zip/pdf/cbz (encourage zip).
- **Four path roots:** executable · database (config/hashes/metadata) · cache (search index/thumbnails/extracted cache) · manga (zip/pdf + sidecars).
- **Dependency hygiene:** every dep verified actively maintained + cp314 wheels on the 3 target platforms (2026-06-14). Notable swaps: **passlib → argon2-cffi** (passlib dead); **watchdog → watchfiles** (no cp314 macOS-arm64 wheel). See [`design-spec.md`](design-spec.md) §4.

## Round-2 clarifications (2026-06-14 feedback on design-spec)
- **Python 3.14** confirmed as the floor.
- **Images: pyvips only — NO Pillow fallback** (fallback adds complexity for little gain). LGPL-3.0+ bundled libvips is acceptable under MIT as long as libvips itself isn't modified (dynamic link). Perceptual-hash dedup will prefer a pyvips+numpy pHash to avoid pulling Pillow transitively.
- **Default to Original archive** confirmed — **plus a GP "balance calculator"** (parse cost + current GP from the `archiver.php` page, show before download) **and a server-side "rate limiter"** (throttle archiver/download calls; enforces what LANraragi left advisory).
- **Proxy scope corrected:** when a proxy is set it routes **all** e-hentai/exhentai/**H@H** traffic by default (the H@H ZIP host is a third host that must be reachable; in fully-blocked regions e-hentai is blocked too), with an optional per-host narrow-to-exhentai override. Use **`socks5h://`** (DNS resolved at the proxy) so a poisoned local resolver can't block the host.
- **Auth: single-owner + a shareable read-only passcode** (two tiers). Owner = full RW/admin; Reader = browse/read/own-progress only. argon2-cffi hashes; API keys = `secrets` tokens (store hashes).
- **EhTagTranslation** confirmed: pull `db.full.json` via `releases/latest/download/`, refresh daily. Protocol doc corrected (`db.text.json` → `db.full.json`).

## Still to confirm (design-spec §7)
Resample auto-fallback vs block-and-report when GP short (recommend configurable, default block) · secrets-at-rest key source (recommend a generated keyfile in `database/`).
