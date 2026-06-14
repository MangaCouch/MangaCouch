# MangaCouch Frontend

The React + TypeScript + Vite PWA for MangaCouch — a manga library, detail
browser, and (web-first) reader. The production build is served as static
assets by the FastAPI backend at the site root, talking to `/api/*` on the same
origin.

## Stack

- **React 18** + **TypeScript** + **Vite 6**
- **react-router-dom** for navigation
- **vite-plugin-pwa** for the manifest + service worker (installable, offline
  shell, image caching)
- A small hand-rolled data layer (`fetch` wrapper + hooks) — no react-query, to
  keep the bundle lean
- ESLint flat config including **`eslint-plugin-react-hooks`**
- A single global stylesheet (`src/styles/global.css`) with CSS custom-property
  theming — no UI framework. Dark theme by default, light toggle.

## Scripts

```bash
npm install      # install dependencies (network required — not run here)
npm run dev      # Vite dev server on :5173, proxying /api -> http://localhost:8000
npm run build    # type-check + production build into dist/
npm run preview  # preview the production build locally
npm run lint     # ESLint
npm run typecheck# tsc --noEmit
```

> Author-time note: `npm install` / `npm run build` were intentionally **not**
> run in this environment (no network). The source is authored to build cleanly
> with the pinned versions in `package.json`.

## Backend integration

- **Build output:** `npm run build` emits to `frontend/dist/`. Point FastAPI's
  static mount at that directory and serve `index.html` as the SPA fallback for
  any non-`/api` route (client-side routing uses the History API; Vite is
  configured with `base: '/'`).
- **Dev proxy:** during `npm run dev`, `vite.config.ts` proxies `/api` to
  `http://localhost:8000`, so the SPA hits the real FastAPI server with no CORS
  setup. Change the target there if your backend runs elsewhere.
- **Auth:** the SPA POSTs `{ passcode }` to `/api/auth/login` and stores the
  returned `api_key` in `localStorage`. Every request sends
  `Authorization: Bearer <base64(apiKey)>` (the base64 step lives in
  `src/api/client.ts`). A `401` clears the key and returns to the passcode
  screen. Owner-only routes (Downloads, Settings) are gated by the `role`
  returned from login.
- **PWA caching:** the service worker precaches the app shell and runtime-caches
  thumbnail responses (content-addressed, immutable). It never intercepts
  `/api/*` navigations or page-image requests, so reads always reflect the live
  server.

### Media element auth (assumption for the backend)

Browsers can't set an `Authorization` header on `<img src>`. For media endpoints
(`/api/archives/{id}/thumbnail`, `/api/archives/{id}/page`) the client appends
`?key=<base64(apiKey)>` to the URL (see `mediaUrl` in `src/api/client.ts`). **The
backend should accept this `key` query param as an alternative to the Bearer
header on GET media routes.** If the backend instead authorizes media via a
short-lived cookie or a signed URL, only `mediaUrl` needs to change.

## Project structure

```
src/
  api/
    client.ts        # fetch wrapper, base64 Bearer auth, 401 handling, mediaUrl
    endpoints.ts     # typed wrappers for every REST endpoint (§6.1)
    types.ts         # API response types (mirrors §3.2 / §6.1)
  components/
    ArchiveCard.tsx  # library grid card
    Layout.tsx       # nav bar + routed content shell
    SearchBar.tsx    # q / sort / category / random / newonly controls
    SmartImage.tsx   # robust image: skeleton, retry+backoff, force-load
    ui.tsx           # Spinner, ErrorBanner, StarRating, TagChip, Modal
  hooks/
    useApi.ts        # useAsync + usePolling
    useAuth.tsx      # auth context, login, lock + auto-lock, 401 wiring
    useTheme.tsx     # dark/light theme context
  i18n/
    strings.ts       # t() + en / zh-Hans dictionaries (i18n-ready stub)
  lib/
    storage.ts       # namespaced localStorage helpers
    tags.ts          # tag grouping, display names, read/progress helpers
  reader/
    useReader.ts     # reader state machine (pages, current, progress, preload)
    spreads.ts       # double-page spread computation (cover/last/wide rules)
    PagedView.tsx    # paged slide view (single/double, RTL/LTR, tap zones)
    ScrollView.tsx   # continuous webtoon view
    ReaderSettingsPanel.tsx
    settings.ts      # persisted reader defaults + local bookmarks
  routes/
    LockScreen.tsx   # passcode entry
    Library.tsx      # grid + infinite scroll + URL-synced search
    Detail.tsx       # cover, tags, rating, preview, comments, favorites, related
    Reader.tsx       # the reader route (toolbar, keyboard, swipe, fullscreen…)
    Downloads.tsx    # owner: submit URL, GP balance, live job list
    Settings.tsx     # owner: config, scan, regen, upload, plugins, prefs
  App.tsx            # providers + router + auth gate + owner guards
  main.tsx           # entry
```

## Routes

| Path | Screen | Access |
|------|--------|--------|
| `/` | Library (grid, search, sort, category, random, newonly) | reader+ |
| `/archive/:id` | Detail page | reader+ |
| `/read/:id?page=N` | Reader (full-bleed) | reader+ |
| `/downloads` | Downloads (submit, GP balance, job list) | owner |
| `/settings` | Settings/admin (config, scan, regen, upload, plugins) | owner |

The lock screen replaces the whole app whenever there is no valid key.

## Reader notes

- **Modes:** continuous vertical scroll (webtoon) and paged slide. Paged
  supports single + double-page spreads (cover and last page shown alone; a
  landscape page collapses its spread to single), with manga RTL and LTR.
- **Fit:** width / height / container / original; fullscreen; autoplay timer;
  configurable preload (default 2, doubled in double-page mode).
- **Robustness ("图不能裂"):** every page is a `SmartImage` with a skeleton,
  automatic retry with exponential backoff, a manual retry button, recovery from
  partial/corrupt decodes, and a "Load all images" action that force-loads every
  page.
- **Resume:** opens at the saved progress page (or `?page=N`); page changes
  debounce-PUT `/api/archives/{id}/progress/{page}`. "Read" = progress > 85%.
- **Bookmarks** are local (localStorage). Reading defaults (mode/direction/fit/
  preload/theme) persist in localStorage.
- **Touch:** swipe to page, tap zones for prev/next. Pinch zoom is intentionally
  out of scope (P2).

## API shape assumptions

These are the response shapes the frontend expects (see `src/api/types.ts`). The
backend is the source of truth; adjust either side to match.

- `POST /api/auth/login` → `{ api_key, role: "owner"|"reader" }`
- `GET /api/archives` → `{ archives: Archive[], total, page, page_size? }`
- `GET /api/archives/{id}` → `Archive` (includes `tags`, `progress`, optional
  `comments`, optional `love_count`/`read_count`/`favorite_count`)
- `GET /api/archives/{id}/pages` → `{ pages: [{ index, path }] }`
- `GET /api/archives/{id}/page?path=` → image bytes
- `GET /api/archives/{id}/thumbnail` (cover) / `?page=N` (page grid) → image bytes
- `Tag` = `{ namespace, value, translated? }`
- `GET /api/categories` → `{ categories: Category[] }`
- `GET /api/favorites/lists` → `{ lists: FavoriteList[] }` (each may include
  `archive_ids` so the detail page can show membership)
- `GET /api/jobs` → `{ jobs: DownloadJob[] }`; job `progress` may be 0–1 or
  0–100 (the UI normalizes >1 as a percentage)
- `GET /api/ehentai/balance?url=` → `{ balance, original_cost, resample_cost,
  sufficient?, gallery_title?, error? }`
- `GET /api/plugins` → `{ plugins: PluginInfo[] }`
- `GET/PUT /api/config` → an opaque JSON object (edited as JSON in Settings)

Where the spec left a shape implicit, the assumption is documented inline in
`src/api/types.ts`.
