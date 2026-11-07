# MangaCouch browser extension

Adds E(x)Hentai and nHentai galleries to a MangaCouch library's download queue
from the toolbar popup or the link context menu. The extension only submits
gallery URLs; source cookies, User-Agent and proxy settings live in the
library's own **Settings → Online sources** page.

## Install

Chrome or another Chromium browser: open `chrome://extensions`, enable
Developer mode, choose **Load unpacked** and select this `extension` directory
(or the unzipped `mangacouch-extension-<version>.zip` from a GitHub Release).

The extension's own language follows the browser UI language: English by
default, Simplified Chinese when the browser runs in `zh-CN`.

## Configure

1. In the library web app, open **Settings → Access & connections → API keys**
   and create a key of kind **`downloads`** (or run
   `mangacouch --db <database> key-create --kind downloads --label <name>`).
   That kind can only add downloads and check their progress; it cannot read
   the library or change settings.
2. In the extension's Settings page, enter the library origin, for example
   `http://localhost:8780`, and paste the key. Saving asks for host permission
   for that origin only.

The key stays in the browser's local extension storage and is sent as a
Bearer token; it never appears in a page URL or content script. Saving a
different library address revokes the previous origin permission. If the
browser cannot remove it, Settings says so, and the old origin can be removed
under the browser's extension permissions.

## Use

Open an E(x)Hentai, ExHentai or nHentai gallery and click the toolbar icon.
nHentai galleries offer two entries: find an E(x)Hentai edition first (the
default) or download from nHentai directly. The link context menu always uses
the E(x)Hentai-first entry.

Adding a URL only queues it. Open **Downloads** in the library to compare
candidates and confirm any quoted archive cost; nothing is purchased by the
extension.

If a context-menu request fails, the toolbar icon shows a `!` badge, the
message appears when hovering the icon and in the popup. Settings opens
automatically only when the address, key or origin permission needs fixing.

## Develop

```sh
node --test extension/*.test.mjs
```
