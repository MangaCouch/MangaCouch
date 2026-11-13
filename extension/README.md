# MangaCouch browser extension

Adds E(x)Hentai galleries to a MangaCouch library's download queue
from the toolbar popup. The extension only submits
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
   That kind can only add downloads and check the key; it cannot read
   the library or change settings.
2. In the extension's Settings page, enter the library origin, for example
   `http://localhost:8780`, and paste the key. Saving asks for host permission
   for that origin only, then checks the connection: a green light means the
   library answered and accepted the key, a red one says what went wrong
   (unreachable address, rejected key, or a read-only key that cannot add
   downloads). **Check connection** repeats the check at any time, and the
   page runs it again whenever it opens.

The key stays in the browser's local extension storage and is sent as a
Bearer token; it never appears in a page URL or content script. Saving a
different library address revokes the previous origin permission. If the
browser cannot remove it, Settings says so, and the old origin can be removed
under the browser's extension permissions.

## Use

Open an E-Hentai or ExHentai gallery and click the toolbar icon.
The popup says whether the gallery was added or
was already in the queue.

Adding a URL only queues it. Open **Downloads** in the library to choose
download quality and confirm any quoted archive cost; nothing is purchased by the
extension.

The extension needs Chrome 103 or a Chromium browser of the same version or
later.

## Develop

```sh
node --test extension/*.test.mjs
```
