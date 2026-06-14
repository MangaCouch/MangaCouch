# MangaCouch Downloader (browser extension)

A minimal **Manifest V3** browser extension (Chrome + Firefox) that captures an
e(x)hentai gallery URL from the active tab and sends it to your **MangaCouch**
server to trigger an **Archive Download**.

It has **no build step** — it is plain JavaScript with native `fetch` and the
WebExtension `chrome.*` / `browser.*` APIs. Load the folder as-is.

---

## What it does

- Reads the **active tab's URL** and validates it is an e(x)hentai gallery
  (`https://e-hentai.org/g/<gid>/<token>/` or the `exhentai.org` mirror).
- On **Download to MangaCouch**, `POST`s the gallery URL to your server's
  `/api/download` endpoint using your **owner** API key.
- **Polls** `/api/job/{id}` every ~2 seconds and shows the live job state
  (queued / running / preparing / done / failed) with GP cost, progress, and
  any error.
- Adds a right-click **context-menu** entry on e(x)hentai gallery pages and
  gallery links — "Download this gallery to MangaCouch" — that runs the same
  flow from the background service worker (toolbar badge shows the outcome).

The extension uses the **owner** key because triggering a download is a
privileged action (it can spend GP). Treat it accordingly.

---

## Files

| File | Role |
|------|------|
| `manifest.json` | MV3 manifest. Permissions: `activeTab`, `storage`, `contextMenus`. |
| `common.js` | Shared module: cross-browser shim, config storage, URL validation, the `/api/download` + `/api/job/{id}` client. |
| `popup.html` / `popup.css` / `popup.js` | The toolbar popup: shows the tab URL and the Download button, polls the job. |
| `options.html` / `options.js` | Settings page: Server URL + Owner API Key. |
| `background.js` | Service worker: context menu + message relay running the same flow. |
| `icons/icon.svg` | Source icon (not referenced by the manifest — see **Icons**). |

---

## Install (load unpacked)

### Chrome / Chromium / Edge / Brave

1. Go to `chrome://extensions`.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked** and select this `extension/` folder.
4. The "MangaCouch Downloader" action appears in the toolbar. Pin it if you like.

### Firefox

1. Go to `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…**.
3. Select the **`manifest.json`** file inside this folder.
4. The action appears in the toolbar. (A temporary add-on is removed when
   Firefox restarts; reload it the same way, or package & sign it for a
   permanent install.)

> The manifest includes a `browser_specific_settings.gecko` block (add-on id +
> `strict_min_version`) so Firefox loads it without complaint. Chrome ignores
> that block.

---

## Configure

Open **Options** (the extension's Settings page — the popup links to it, or use
the browser's extension details → Extension options):

1. **Server URL** — the base URL of your MangaCouch server,
   e.g. `http://localhost:8000`. No trailing path; a trailing slash is trimmed.
2. **Owner API Key** — paste the **raw** owner API key. The extension
   base64-encodes it for the `Authorization` header automatically. **Do not**
   base64-encode it yourself.
3. Click **Save**.

Settings are stored with `chrome.storage.sync` (so they roam with your browser
profile) and transparently fall back to `chrome.storage.local` if sync is
unavailable.

Until both fields are set, the popup shows a "configure me" message linking to
Options instead of the Download button.

---

## API contract (what the extension depends on)

The extension talks to exactly two endpoints. The server must enable **CORS**.

### `POST {server}/api/download`

- **Headers**
  - `Authorization: Bearer <base64(ownerApiKey)>`
  - `Content-Type: application/json`
- **Body**
  ```json
  { "url": "https://e-hentai.org/g/<gid>/<token>/", "catid": null }
  ```
  `catid` is an optional category id (`number` or `null`). The extension sends
  `null`.
- **Response** (JSON) — at minimum:
  ```json
  { "id": 123, "state": "queued" }
  ```

### `GET {server}/api/job/{id}`

- **Headers**
  - `Authorization: Bearer <base64(ownerApiKey)>`
- **Response** (JSON):
  ```json
  {
    "id": 123,
    "url": "https://e-hentai.org/g/<gid>/<token>/",
    "gid": "<gid>",
    "token": "<token>",
    "state": "running",
    "priority": 0,
    "progress": 0.42,
    "gp_cost": 1234,
    "error": null
  }
  ```
  `state` is one of `queued | running | preparing | done | failed`.
  `progress` is rendered as a percentage — the popup accepts either a `0..1`
  fraction or a `0..100` number. Polling stops at `done` or `failed`; on
  `failed` the `error` string is shown.

This mirrors §5.3 and §6.1 of the MangaCouch design spec.

---

## Authorization header detail

The user pastes the raw owner API key, e.g. `sk_owner_abcdef`. The extension
sends:

```
Authorization: Bearer c2tfb3duZXJfYWJjZGVm
```

i.e. `"Bearer " + btoa(apiKey)`. This matches the spec's
`Authorization: Bearer <base64(apiKey)>` scheme.

---

## Permissions & host access (why it's minimal)

- **`activeTab`** — read the URL of the tab you're on when you open the popup.
- **`storage`** — save the server URL and API key.
- **`contextMenus`** — the right-click "Download this gallery" entry.

The extension does **not** request broad host permissions. Requests to your
server go through `fetch()`, and because the server sends CORS headers, no host
permission is required for the cross-origin call in Chrome. `<all_urls>` is
**not** requested.

If a particular browser/server combination ever blocks the `fetch` for lack of
a host permission, the manifest declares `optional_host_permissions` for
`http://*/*` and `https://*/*`; you can grant the specific server origin at
runtime via the browser's permission prompt without re-shipping the extension.

---

## Icons

The extension ships **without** a configured toolbar/extension icon, on purpose:

- Generating placeholder **PNG** icons requires binary image tooling that isn't
  available in this build environment.
- Chrome MV3 does **not** reliably accept **SVG** for `action.default_icon` or
  the top-level `icons` map (Firefox does), so referencing the SVG directly
  would risk a manifest load error in Chrome.

Both browsers supply a generic placeholder icon when none is declared, which is
harmless. A clean source icon is included at **`icons/icon.svg`**.

**To add a real icon:** rasterize `icons/icon.svg` to PNGs at 16, 32, 48, and
128 px (e.g. with `rsvg-convert`, ImageMagick, Inkscape, or any SVG→PNG tool),
drop them in `icons/`, and add to `manifest.json`:

```json
"icons": {
  "16": "icons/icon-16.png",
  "32": "icons/icon-32.png",
  "48": "icons/icon-48.png",
  "128": "icons/icon-128.png"
},
"action": {
  "default_title": "Download to MangaCouch",
  "default_popup": "popup.html",
  "default_icon": {
    "16": "icons/icon-16.png",
    "32": "icons/icon-32.png",
    "48": "icons/icon-48.png",
    "128": "icons/icon-128.png"
  }
}
```

Firefox alone could instead reference `icons/icon.svg` directly.

---

## Troubleshooting

- **"Set your server URL and owner API key…"** — open Options and fill both
  fields.
- **"This tab is not an e(x)hentai gallery page"** — the popup only enables the
  button on a `…/g/<gid>/<token>/` gallery URL.
- **`download request failed (401)`** — the API key is wrong, or the server
  rejected the role. Re-check the **owner** key in Options.
- **`download request failed (…): … CORS …` / network error** — the server
  isn't sending CORS headers, or the Server URL is wrong/unreachable.
- **Context menu does nothing visible** — the service worker reports outcome via
  the toolbar badge (`…` working, `OK` done, `ERR` failed, `CFG` not
  configured). Open the popup for full job detail.
