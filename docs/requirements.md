# MangaCouch — Requirements (full wishlist)

> Source: the maintainer's original outline brain-dump (drafted 2024, reorganized 2026-06-14). MangaCouch is a rewrite of LANraragi for **offline e-hentai / exhentai archiving + reading**, plus manual import of zip / pdf / cbz manga.
>
> **This is the full feature superset (traceability), faithful to the original list — every item is preserved.** For the **scoped v1 plan** (what actually gets built first, with architecture + dependencies), see ⭐ **[`design-spec.md`](design-spec.md)**; for the resolved decisions see [`decisions.md`](decisions.md).
>
> Chinese source terms are kept in parentheses so the maintainer can recognize their own wording. Priority tags are a lean: **P0** = v1 core, **P1** = important, **P2** = later / stretch / optional.

---

## A. Hard rules (non-negotiable)

These came through the original list as imperatives, not features. They override convenience and shape the architecture. See [`design-spec.md`](design-spec.md) §3 for how each is satisfied.

| # | Rule | Why | Consequence |
|---|------|-----|-------------|
| R1 | **Redis must go.** | A separate server process can't ship in a click-to-run folder, and LANraragi's Redis-as-database forces every awkward pattern. | Embedded datastore (**SQLite**); in-process workers, no broker. |
| R2 | **Never store absolute paths.** | Drive letters / mount points change between machines and across portable media. | All on-disk references are **relative to the content root**. |
| R3 | **Don't depend on symlink-following.** | exFAT and some NAS exports have no symlinks. | No feature may require following symlinks. |
| R4 | **Hash once at ingest, cache it; only re-hash new/changed files.** | Re-reading whole archives off slow disks on every scan is unacceptable. | Identity hash cached, keyed by **size + mtime**; unchanged files are never re-read. |
| R5 | **Bundled/native libs; use a fast image library.** | The user must not install system libs or loose binaries. | **zip + pdf mandatory** (`zipfile` stdlib + `pypdfium2`); **cbz** trivial (=zip); **7z postponed**; **RAR/CBR dropped** (Q8). Images: **pyvips** primary (fastest) → Pillow fallback. |
| R6 | **CJK tokenization.** | Japanese/Chinese titles and namespaced tags don't tokenize on whitespace. | FTS with a CJK-aware tokenizer (ICU / trigram / segmenter), designed in from day one. |
| R7 | **Cross-platform: Windows / macOS / Linux, no WSL.** | Perl-on-Windows needing WSL is the original sin being corrected. | Single codebase; click-to-run folder or native installer per OS. |

---

## B. Reader (P0 — the daily-use surface)

- **Reading modes:** continuous **scroll** or paged **slide** (滚动 / 翻页); multi-direction scrolling (多种方向滚动); **double-page** view + single-page, with cover-page handling; manga **RTL** / LTR.
- **Paging:** preload (预加载) + seamless/no-flicker navigation (无缝浏览); autoplay with a configurable **timer** (自动播放定时器 / 定时滚动).
- **Robustness — "图不能裂" (images must never break):** every page is a **placeholder** until loaded; **retry images that failed to load** (加载读取失败的照片); **recover cracked/partial images** (加载裂开的照片); a "**load all images**" action (加载所有照片).
- **Zoom & inspection — P2 (optional, per feedback):** zoom in/out; **floating magnifying glass** (浮动放大镜).
- **Fit & display:** **auto-fit image size / text size** (自动适应图片大小/文字大小); **fullscreen** (全屏); **themes** + **night mode** (主题 / 夜间模式).
- **Input:** touch-screen optimization (触屏优化).
- **On-the-fly extraction:** read pages **straight from the archive** without full unpack (在线解压缩 / "read from zip on the go"), with a page cache.
- **Resume:** **quick continue reading** + bookmarks (quick continue / Bookmarks and continue reading); **remember default reading settings**.

## C. Privacy & app shell (P1)

- **Auto-lock** + **passcode** (自动锁定 / pass lock).
- **Background blur** when the app is backgrounded (后台模糊).
- **Image cache** (图片缓存).

## D. Detail / gallery page (P0)

- **Fields:** cover, title, author/circle, tags, **love/read/favorite counts**, language, **rating**, **page count**, preview thumbnails (详细页 / 详情页: 封面 / title / author / tag / love / read / fav count / language / rating / page count / preview).
- **Gallery actions & metadata:** archive (download whole gallery), **torrent**, share, category/type, favorite, similar/related, preview, comments (归档 / 种子 / 分享 / 类型 / 收藏 / 语言 / 评分 / 页数 / 相似 / 预览 / 评论).
- **Comments:** username / time / content (评论: username/time/content); "see comments".
- **View full gallery details** (查看漫画详情).

## E. Browse & discovery (P0 / P1)

- **Categories / sections** (分类): theme, **popular / E-站热门** (E站热门), tags, favorites, **ranking** (排行), history (主题 / 热门 / 标签 / 收藏 / 排行 / 历史).
- **Favorites with multiple lists** (收藏: 多个列表).
- **History** + per-action **logs** (历史 / 历史记录 / 日志).
- **Search:** quick + fuzzy (快速 / 模糊搜索); namespaced `namespace:value` queries (from LANraragi).
- **Sort by time** (按时间排序).
- **Random** gallery (随机).
- **Related recommendations** (相关推荐 / 相似) and **same-series grouping** (同系列分类).
- **Comic categories** (分类漫画 / Comic categories).
- **Statistics / dashboards** (数据统计).
- **Thumbnails** for the grid (thumbnail).

## F. Tagging (P0)

- **Namespaced tags** as first-class (artist / group / parody / character / female / male / language / …).
- **Tag translation** via EhTagTranslation (标签翻译 / Tag翻译).
- **CJK tokenization** (see R6).

## G. Acquisition / downloads (P0 — the differentiator)

- **v1 focus (per feedback):** a **browser extension** captures the e(x)hentai gallery URL → server triggers e-hentai's **"Archive Download"** (whole-gallery ZIP via `archiver.php`). Modeled on LANraragi's **Tsukihi** extension + `/api/download_url`.
- **Downloads:** **resume / breakpoint-continue** (断点续传), **priority** ordering (优先下载). ~~**BitTorrent**~~ (bittorrent) — **deferred** (Q3). ~~multi-threaded per-image scraping~~ — not used; we fetch the prepared archive.
- **Download new galleries** (下载新的漫画 / 下载新的漫画).
- **Update tracking / auto-update** of followed galleries or searches (追踪漫画更新 / update detect / 自动更新).
- **Queue** management (队列).
- **Proxy** support (代理) — important for exhentai reachability.
- **Plugin system for other sites** (其他站点的插件系统) — pluggable sources beyond e-hentai (Pixiv, etc.).
- **Skip / remove ad pages** (skip ads / 删除广告页面).
- **Pixiv** source (Pixiv API for Python).

## H. Formats & ingest (P0)

- **Archive formats:** **zip + pdf (v1 mandatory)**; cbz (trivial, =zip); ~~7z, tar, cb7, cbt~~ (postponed); ~~rar, cbr~~ (**dropped**, Q8 — encourage convert-to-zip).
- **Image formats:** jpg, png, **apng**, webp, bmp.
- **PDF** support (PDF).
- **Thumbnails** generated at ingest (thumbnail).
- **Metadata extraction** à la **ComicTagger** (Info: ComicTagger) + the Eze `info.json` sidecar convention.
- **Stream from archive** on read (在线解压缩 / cache, read from zip on the go).

## I. Platform & ops (P1)

- **Windows / macOS / Linux** (see R7).
- **i18n** via **Crowdin** (i18n / accounts.crowdin.com).
- **Changelog discipline** — Keep a Changelog format (keepachangelog.com).

## J. Stretch / ML (P2)

- **Auto-typeset & translate** — OCR a page, machine-translate, re-letter the bubbles (自动嵌字&翻译). Large, separate project; defer.
- **CNN-based** processing (CNN) — most likely **auto-tagging** (WD-Tagger/DeepDanbooru over covers/pages) and/or part of the translation pipeline. Ship as an **optional add-on**, CPU base + GPU opt-in.

---

## K. Source / stack references collected (and what each implies)

| Reference | Implication |
|---|---|
| `shuiqukeyou/E-HentaiCrawler` (Python) | e-hentai crawling prior art → Python acquisition layer |
| `ccloli/E-Hentai-Downloader` (userscript) | gallery-page → archive download flow; ZIP assembly client-side |
| Pixiv API for Python | Python is the natural home for the Pixiv source plugin |
| `accounts.crowdin.com` | i18n workflow = Crowdin |
| `keepachangelog.com` | changelog conventions |
| **LANraragi Redis → RocksDB** | the maintainer was hunting for an **embedded** store. Resolution: **SQLite**, not RocksDB — relational joins + FTS are needed (a KV store re-creates Redis's problems). See `decisions.md` Q2. |
| `eslint-plugin-react-hooks` | frontend = **React** |
| `google/pytype` | **typed Python** (pytype / pyright) for safety |

---

## L. Scope conflicts — RESOLVED (see [`decisions.md`](decisions.md))

1. **RAR / CBR** → **dropped** (Q8) — avoids the one loose external binary; encourage convert-to-zip.
2. **Client form-factor** → **Responsive PWA** (Q9); native-only gaps (true background-blur, OS lock) accepted as losses.
3. **ML scope (嵌字 + CNN)** → **plugin API surface only in v1** (Q10); auto-tag + auto-translate hooks designed, not implemented.
4. **Paths** → four roots (Q-feedback): executable · database (config/hashes/metadata) · cache (search index/thumbnails/extracted cache) · manga (zip/pdf + sidecars). See [`design-spec.md`](design-spec.md) §3.2.
