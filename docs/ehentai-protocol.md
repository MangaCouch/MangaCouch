# E-Hentai / ExHentai Offline Archiving — Technical Protocol Notes (2026)

> Reference for MangaCouch's acquisition layer (the hard, differentiating part — no good server-side prior art exists). Compiled 2026-06-14.
>
> Primary sources: **ehwiki.org** (API, Namespace, Image_Limits, Archiver, Hentai@Home, Multi-Page_Viewer), and open-source reference implementations **EhViewer** (`exzhawk/EhViewer`, Java), **EhPanda** (`EhPanda-Team/EhPanda`, Swift), **gallery-dl** (`mikf/gallery-dl`, Python — caveat below), and **LANraragi** (`Difegue/LANraragi`, Perl — cleanest Archiver impl). Tag DB: `EhTagTranslation/Database`. Uncertain items flagged inline.
>
> **Load-bearing caveat:** As of **gallery-dl v1.32.3 (2026-06-13)** the exhentai extractor appears **removed from master** (`exhentai.py` absent; no E-Hentai row in `supportedsites.md`). Logic below was verified against the **v1.30.0** sdist (last version confirmed to ship it). If leaning on gallery-dl, **pin ≤1.30.x and re-verify**; otherwise treat **EhViewer/EhPanda as authoritative**. (Removal reason: uncertain.)

---

## 1. Authentication & Access

### E-Hentai vs ExHentai and the "sad panda"
- **`e-hentai.org`** — public galleries; mostly viewable anonymously.
- **`exhentai.org`** — members-only mirror hosting restricted/expunged galleries that e-hentai.org dropped. Unreachable anonymously.
- **Sad Panda:** a request to `exhentai.org` *without valid auth cookies* returns a blank page with a crying-panda image — **not** an HTTP error, just a refusal. gallery-dl detects it as a response with **no `Cache-Control` header + empty body** → auth error.

### Required cookies
| Cookie | Purpose | How obtained |
|---|---|---|
| `ipb_member_id` | IPB forum member ID (identifies account) | Set on login to **forums.e-hentai.org** |
| `ipb_pass_hash` | IPB persistent-login hash (authenticates) | Forum login with "remember me" (`CookieDate=1`) |
| `igneous` | **ExHentai access token — the gate to exhentai** (not needed for e-hentai.org) | Issued server-side when an authed account first loads exhentai.org; **IP/region-bound** |
| `sk` | Session key (supplementary) | Set during browsing (role uncertain) |
| `nw` | "Not Worksafe" warning bypass | Client sets `nw=1` itself |

- **`igneous` is the real key.** `ipb_*` alone is often insufficient. If `igneous` is empty or the literal `mystery`, exhentai fails — documented fix is **log out and back in from a EU/US IP** so the server issues a proper random `igneous` (implies IP/region binding).

### Login flow (how cookies get set — gallery-dl reference)
- **POST** `https://forums.e-hentai.org/index.php?act=Login&CODE=01`, `Referer: https://e-hentai.org/bounce_login.php?b=d&bt=1-1`
- Body: `CookieDate=1`, `b=d`, `bt=1-1`, `UserName=…`, `PassWord=…`, `ipb_login_submit=Login!`
- Success string: `"You are now logged in as:"`. A captcha forces fallback to **cookie-paste auth** (export from a logged-in browser).
- After login, GET `<root>/favorites.php` to "collect more cookies" — on exhentai this triggers issuance of the access cookies. Cookies cache ~90 days.

### Account requirements
- An existing **E-Hentai forum account** with **"EX-site permission"** is mandatory.
- **New-account prerequisite** (age / "must browse a gallery first"): **uncertain** — widely repeated community lore, not confirmed in authoritative sources. Confirmed: existing account + EX permission + correct cookies (esp. `igneous`) + non-restricted login IP.
- Tools requesting an `ex` URL with no auth silently fall back to e-hentai.org and disable original-image handling.

---

## 2. Gallery Metadata API

**Endpoint:** `https://api.e-hentai.org/api.php` — POST, JSON in/out. (`e-hentai.org/api.php` and `exhentai.org/api.php` resolve to the same API.)

### `gdata` — request
```json
{ "method": "gdata", "gidlist": [[2231376, "a7584a5932"]], "namespace": 1 }
```
- `gidlist` = array of **`[gid, gtoken]` pairs**.
- `namespace: 1` (optional) → tags carry their namespace prefix; without it, prefixes omitted.

### `gdata` — response
```json
{ "gmetadata": [ {
  "gid": 2231376, "token": "a7584a5932",
  "archiver_key": "434484--d3a5cb53…",
  "title": "...", "title_jpn": "...",
  "category": "Doujinshi", "thumb": "https://ehgt.org/…_l.jpg",
  "uploader": "...", "posted": "1466721491",
  "filecount": "30", "filesize": 30000000,
  "expunged": false, "rating": "4.45", "torrentcount": "1",
  "torrents": [ { "hash":"...", "added":"...", "name":"...", "tsize":"...", "fsize":"..." } ],
  "tags": ["language:english","parody:original","artist:gentsuki","female:pantyhose"]
} ] }
```
- **Type gotcha:** `posted`, `filecount`, `rating`, `torrentcount` are **string-encoded**; `filesize` and `gid` are real ints; `expunged` is a real bool.
- Error form: `{ "gid": 2231376, "error": "Key missing, or incorrect key provided." }`.

### `gtoken` — page → gallery resolution
```json
{ "method": "gtoken", "pagelist": [[618395, "40bc07a79a", 11]] }
→ { "tokenlist": [ { "gid": 618395, "token": "0439fa3666" } ] }
```
`pagelist` entries = `[gid, page_token, page_number]`.

### gid + gtoken from URLs
- Gallery URL: **`https://e-hentai.org/g/<GID>/<TOKEN>/`** (works on exhentai too). `GID` numeric; `TOKEN` (gtoken) = **exactly 10 lowercase hex** `[0-9a-f]{10}`. Regex: `/g/(\d+)/([0-9a-f]{10})`.
- Page/image URL: `https://e-hentai.org/s/<IMAGE_TOKEN>/<GID>-<PAGE>` (10-hex per-page token; tail `gid-pagenum`).

### Rate limits (documented polite ceiling)
- **Max 25 `[gid,token]` per `gdata` request.**
- **~4–5 sequential requests OK, then wait ~5s.** → batch 25/call, sleep ~5s every ~4–5 calls.
- ehwiki does **not** document an explicit IP ban for *API* abuse (uncertain) — distinct from the image 509 limit and the "excessive pageloads" IP ban (§6).

### Namespaced tags
- Tags are **`namespace:value`** strings, prefixed only when `namespace:1` sent.
- Namespaces (+ shorthands): `artist`(a) `group`(g,circle) `parody`(p,series) `character`(c,char) `female`(f) `male`(m) `mixed`(x) `language`(l,lang) `cosplayer`(cos) `reclass`(r) `other`(o) `location`(loc) `temp`.
- Parse: split each tag on first `:` → `(namespace, value)`; group per namespace.

---

## 3. Image / Page Fetching Flow

> `showpage`/`imagedispatch`/`nl` are **undocumented internal APIs** reverse-engineered from EhViewer + EhPanda (which agree). ehwiki only documents `gdata`/`gtoken`.

- Static thumbnail/image CDN proxy: **`ehgt.org`**. Lo-fi mirror: `lofi.e-hentai.org`.

### Gallery → page token → image
1. Gallery page `/g/GID/GTOKEN/` renders a thumbnail grid (`#gdt`); each thumb links to a **page URL** `/s/<pToken>/<gid>-<page>` (page is 1-based).
2. **Page tokens** scraped from `#gdt` hrefs, or bulk-fetched via the `gtoken` API.
3. Resolve page → image URL, two ways:

**(A) Scrape the `/s/` HTML page:**
- `#i3 img[src]` → **displayed (resampled) image URL**
- `#i7 a[href]` → **original full-res link** (`fullimg.php?...`; present only when original ≠ resample)
- `#i6 … nl('<token>')` → the **skip-server / "nl" retry token**
- inline `var showkey = "…"` → unlocks method (B)

**(B) `showpage` JSON API** (avoids re-fetching HTML per page; reuse one gallery-wide `showkey`):
```json
{ "method":"showpage", "gid":901103, "page":12, "imgkey":"91ea4b6d89", "showkey":"<showkey>" }
```
→ HTML fragments: `i3` (img URL), `i6` (`nl(...)`), `i7` (`fullimg.php…`). Stale showkey → `"Key mismatch"` → refetch HTML for a fresh one.

### Original vs resampled
- `#i3` = **resampled** (downsized to configured res: 780/980/1280/1600/2400/auto), served from an H@H node.
- **Original** = `#i7` → **`fullimg.php?gid=…&page=…&key=…`** (exists only when original differs). Controlled by account `uconfig.php` "Original Images" + per-client toggle.
- **Cost:** originals are far pricier against the image limit — roughly **10 × filesize_MB hits** vs 1 for a normal view.

### Hentai@Home (H@H) distribution
- P2P network of volunteer Java nodes caching/serving images. Image URLs point at an **H@H node** (per-request host/port), so they are **temporary and expire** — re-resolve the page rather than long-caching the URL.
- **`nl` ("no-load") param** = retry-with-a-different-server: on a failed image load, append `?nl=<token>` to the `/s/` request to skip the failing node. EhViewer collects up to 5 `nl` tokens, retries up to 5×, stops on a duplicate. Use this instead of blind retries.
- **Uncertain:** exact H@H URL path breakdown (server-id/file-id/keystamp/expiry). Solid: targets an H@H node, time-limited, `nl` rotates the node.

### 509 "Bandwidth Exceeded" / image limits
- **Signal:** *not* an HTTP 509 — the resolved image URL is swapped for a sentinel ending in **`/509.gif`** or **`/509s.gif`** (e.g. `ehgt.org/g/509.gif`). Detect by checking the URL suffix.
- **Hit costs:** normal view = **1**; force-reload of a failing image = **50**; original ≈ **10 × filesize_MB**; an API request when opted out of H@H = **5**.
- **Tracking:** by **IP** by default; regenerates ~**3–5/min**.
- **Scaling:** account-based tracking unlocks at **Bronze Star+** or the *More Pages* Hath Perk; non-donators can buy 24h account quota of **10,000 for 20,000 GP**; donators get effectively unlimited. No single published "N/day" figure.

### Multi-Page Viewer (MPV) API
- **Eligibility:** Gold Star+ or the *Multi-Page Viewer* Hath Perk. When enabled, gallery links become **`/mpv/<gid>/<gtoken>`**.
- **Step 1 — fetch MPV keys** (GET `/mpv/…`; keys in inline `<script>`): `mpvkey = "<mpvkey>"` (gallery-wide) and `var imagelist = [ {"n":…,"k":"<imgkey>","t":…}, … ]` (per-page imgkey).
- **Step 2 — resolve each page** via `imagedispatch` (POST JSON to api.php):
```json
{ "method":"imagedispatch", "gid":<int>, "page":<int>,
  "imgkey":"<imagelist[i].k>", "mpvkey":"<mpvkey>", "nl":"<retry token, optional>" }
```
Response: `"i"` = resampled image URL; `"s"` = skip-server/nl token (carry forward as next `nl`); `"lf"` (optional) = `fullimg.php?…` slice (prepend host) for the original. `/509.gif` check applies here too.

---

## 4. Archiver / Hath Download (whole-gallery ZIP)

### What it is
The gallery-page **"Archive Download"** invokes the **Archiver** — produces a single ZIP of the whole gallery, billed against **GP (GalleryPoints)** (or Credits). Two delivery modes: direct HTTPS ZIP, or hand-off to your own running **H@H client**.

### Exact request flow (authoritative: LANraragi `EHentai.pm`)
Real endpoint is **`archiver.php`** (not `gallerypopups.php`, which is just the browser popup):
1. Build `https://<domain>/archiver.php?gid=<gid>&token=<token>`.
2. **GET** (follow ≤5 redirects) to surface errors: `"Invalid archiver key"` or `"This page requires you to log on."`.
3. **POST** the form choosing quality:

| Choice | `dltype` | `dlcheck` |
|---|---|---|
| Original | `org` | `Download+Original+Archive` |
| Resample | `res` | `Download+Resample+Archive` |

4. Parse response: `"Insufficient funds"` → abort. Else body has `document.location = "<finalURL>"` (an H@H/download node).
5. **Append `?start=1`** to `<finalURL>` to serve the ZIP.

- The `gdata` **`archiver_key`** field encodes the credentials the archiver form needs.
- **No archiver *method* in api.php** — documented methods are only `gdata`, `gtoken`, `showpage`/`imagedispatch`.

### GP cost model
- **Base: 20 GP/MiB** (15 GP/MiB donators).
- **Recreated-archive penalty:** galleries >365 days old with no downloads in 90 days cost **3×** (direct server archiver only). H@H downloader is flat 20 GP/MiB, exempt.
- **Resample is NOT free** — same per-MiB rate as original; only saving is smaller filesize. (Corrects a common assumption.)
- **Free quotas:** donators/award holders get a weekly free quota (<1yr galleries); healthy H@H clients earn ~1 GB/day free.
- **Link constraints:** valid **7 days**; usable from ≤**2 /24 IP ranges**; max **4 simultaneous streams**.
- **H@H resolution options:** 780 / 980 / 1280 / 1600 / 2400 / original.

### How bulk tools structure requests
- **LANraragi** — Archiver (`archiver.php` + `dltype`/`dlcheck`); exposes `forceresampled`.
- **Hitomi-Downloader** — per-image (not archiver); `ipb_*` cookies; up to 24 threads but added a **per-page delay** to avoid temp bans.
- **gallery-dl (≤1.30.x)** — per-image via `showpage` paging; **never uses Archiver / never spends GP**. For `fullimg` originals, `gp` option: `resized` (default, fall back to 1280x on `" requires GP"`), `stop`, or `wait`.
- **EhViewer / xeHentai** — per-image, same `/s/<token>/<gid>-<num>` + `showpage` pattern.

---

## 5. Metadata Preservation & Tag Translation

### EhTagTranslation Database
`github.com/EhTagTranslation/Database` — maps e-hentai **English raw tag → localized** display names + descriptions. License CC BY-NC-SA 3.0. Consumed by EhSyringe, EhTagConnector, EhPanda, etc.

- **Source:** `database/*.md`, one per namespace (13 files), each = YAML front matter + a 4-col Markdown table `raw | name | intro | links`.
- **Built/released:** `release` branch + GitHub Release assets, **regenerated multiple times daily** by a bot (latest verified `v7.25128.1`, 2026-06-14). Files = 5 variants × 3 encodings:
  - Variants: `db.raw.*` (Markdown source), `db.text.*` (plain text), `db.html.*` (intro rendered to HTML), `db.ast.*` (parsed AST), **`db.full.*` (all renderings — richest; MangaCouch uses this)**.
  - Encodings: `.json`, `.json.gz`, `.js` (JSONP).
  - **Pin via the stable "latest" URL** (don't hardcode a version): `https://github.com/EhTagTranslation/Database/releases/latest/download/db.full.json` (or `db.full.json.gz`). The `release`-branch raw/jsdelivr URLs still work but the releases/latest asset is the cleaner pin. (~4 MB.) ⚠️ **`db.text.json` is the lightweight alternative if you only need plain-text names; `db.full.json`/`db.html.json` are the current canonical set — don't assume `db.text` is "the" file.**

- **JSON schema** (authoritative: `EhTagTranslation/Editor` → `src/shared/interfaces/ehtag.ts`):
```jsonc
{
  "repo": "https://github.com/EhTagTranslation/Database",
  "head": { "sha": "…", "message": "…",
            "committer": { "name":"ehtagtranslation[bot]", "when":"2026-06-14T01:56:47.000Z" } },
  "version": 7,
  "data": [ { "namespace": "female", "frontMatters": {…}, "count": 612,
              "data": { "lolicon": { "name":"萝莉", "intro":"…", "links":"" } } } ]
}
```
- **Schema gotchas:** timestamp is at `head.committer.when` (nested); the TS interfaces live in the separate `Editor` repo. `temp` is **not** an EhTagTranslation namespace (canonical list = 13: artist, character, cosplayer, female, group, language, location, male, mixed, other, parody, reclass, rows). `rows` is a meta/UI namespace.
- **Translate a tag:** `female:lolicon` → namespace `female`, key `lolicon` → find `data[]` where `.namespace==="female"` → `.data["lolicon"].name` (`萝莉`).
- Live counts (~42.6k entries): artist 15,261 · group 13,953 · character 9,040 · parody 2,795 · language 87 · female 612 · male 574 · cosplayer 160 · other 60 · mixed 23 · reclass 11 · location 7.

### Metadata fields worth preserving (from `gdata` + page)
`gid`, `token`, `archiver_key`, `title`, `title_jpn`, `category` (Doujinshi/Manga/Artist CG/Game CG/Western/Non-H/Image Set/Cosplay/Asian Porn/Misc/Private), `thumb`, `uploader`, `posted` (UNIX, string), `filecount` (string), `filesize` (int), `expunged` (bool), `rating` (string), `torrentcount` (string), `torrents[]` (hash/name/added/fsize/tsize), `tags[]` (namespaced).
**Relationship fields** (version/parent chain): `parent_gid`/`parent_key`, `first_gid`/`first_key`, `current_gid`/`current_key` (pairing semantics partly uncertain).

---

## 6. Practical Constraints & Lessons

### Two distinct limit systems — do not conflate
1. **Image-view quota ("509")** — per-IP by default; costs per §3; regenerates ~3–5/min; signalled by `509.gif`/`509s.gif`; resettable by spending GP (gallery-dl `limits-action="reset"` POSTs `reset_imagelimit=Reset+Quota` to `home.php`).
2. **IP temp-ban for "excessive pageloads"** — separate, harder block. Message: *"Your IP address has been temporarily banned for excessive pageloads which indicates that you are using automated mirroring/harvesting software."*

### Ban-avoidance — real data points
- **Triggers fast, lasts long:** bans reported after only ~3 galleries, lasting ~22h; range minutes → 24–72h.
- **Pageloads (request rate/pattern), not just images, are the trigger** — even metadata-only harvesting has tripped it.
- **Retry loops are the #1 accidental cause** — use the `nl=` fallback token, not blind retries.
- The mitigation that actually shipped across tools: a **per-page download delay**.

### Recommended polite request pattern (synthesized)
- **Prefer the API over HTML scraping.** Batch metadata: `gdata` ≤25 pairs/POST; pause ~5s per ~4–5 calls.
- **Image-extraction delay:** gallery-dl's exhentai default is a **randomized 3.0–6.0s** interval between requests — a good baseline. (`sleep-429` default 60s on HTTP 429.)
- **Low/serial concurrency** — concurrency trips harvesting detection.
- **Cache metadata** (gid/token/filecount/archiver_key) + a download-archive key (`{gid}_{num}`) so re-runs don't re-hit the site.
- **For whole-gallery archives, prefer `archiver.php`** over scraping N image pages — one billed request vs N pageloads (politer, avoids the 509 quota, costs GP instead).
- **TLS note:** exhentai needs non-DH ciphers — gallery-dl sets `ciphers="DEFAULT:!DH"` to avoid `DH_KEY_TOO_SMALL`.

### Legal / ToS
- Auth mandatory for exhentai; the ban message explicitly names "automated mirroring/harvesting software" → treat bulk scraping as abuse. An archiver should: throttle to the documented budget, **require the user's own logged-in account**, never distribute credentials. Content is adult material; respect the user's jurisdiction and the site age-gate/ToS. Account-level (vs IP-level) enforcement: uncertain.

### Consolidated uncertainty list
- New-account exhentai prerequisite (age / "browse first"): unconfirmed.
- Exact H@H image-URL keystamp/expiry encoding: unconfirmed.
- Any single fixed "images/day" number: not published (rules/multipliers only).
- Whether the API hard-bans IPs for abuse: not documented (only 25/≤5s throttle).
- gallery-dl exhentai removal commit/version: absent on v1.32.3/master, present in v1.30.0; reason unknown — **pin ≤1.30.x if relying on it.**
- `torrents` subfields, `parent/first/current` pairing, `sk` cookie role: partly uncertain.

---

## Implications for MangaCouch's acquisition layer (checklist)
- [ ] Cookie-jar auth: `ipb_member_id` + `ipb_pass_hash` + (exhentai) `igneous`; support password login (forums POST) **and** raw cookie paste; reject `igneous == "mystery"`; solve sad-panda by GET-ing exhentai once post-login to mint `igneous`.
- [ ] Metadata via `gdata` (≤25/POST, ~5s every ~4–5 calls); keep `gid`+`token` as primary key everywhere; store page tokens for `gtoken` reverse lookups.
- [ ] Two-tier image fetch (showpage **or** MPV `imagedispatch`); extract resampled (`#i3`) + original (`#i7`/`fullimg`); **build in the `nl` skip-server refetch from day one** or 509 placeholders silently poison the archive.
- [ ] Detect `/509.gif` suffix + the ban-interval page distinctly; real backoff (not naive retry); randomized 3–6s request interval; serial/low concurrency.
- [ ] Optionally use `archiver.php` for whole-gallery ZIPs (politer, costs GP) vs per-image scraping.
- [ ] Namespaced tags stored raw (never pre-translated); ingest **EhTagTranslation `db.full.json`** (via `releases/latest/download/`) as a versioned cached side dataset (poll release `head.committer.when`; bot regenerates daily); translate at display time; generate zh-Hant from zh-Hans.
- [ ] Persist galleries as **CBZ + JSON sidecar** (adopt Eze `info.json` convention for LANraragi interop); preserve the full metadata field set above; consider original-resolution for archival fidelity (aware of higher GP/limit cost).
- [ ] Domain-fronting / IP-pinning fallback (EhPanda, picacg-qt) since DNS to e-hentai is frequently blocked.
