# Clients and integrations

## API keys

Create keys in **Settings → Access & connections → API keys** while signed in to the web app, or on the server with:

```sh
mangacouch --db <database> key-create --kind <kind> --label <name>
```

An API key cannot create other keys; only a signed-in browser session (or the CLI) can.

| Kind | In Settings | Can do |
| --- | --- | --- |
| `readonly` | **Read-only** | Browse, read and download (`GET`/`HEAD` only), including sidecar conflicts and sync status, duplicate comparisons and review previews; nothing is written back, reading progress included |
| `management` | **Full access** | Everything else, including library changes and progress sync from Komga clients |
| `downloads` | **Browser extension: can only add downloads** | Only `POST /api/downloads` and `GET /api/auth/me` |

**Settings → Access & connections → Change library passcode** signs out every web device. Tick **Also revoke all API keys (the browser extension and other clients then need new keys)** to revoke every key at the same time; otherwise keys keep working.

## OPDS

OPDS 1.2 catalogs are served at `/opds` and `/komga/opds/v1.2` (**Settings → Access & connections → External readers (OPDS)** shows the URL). Sign in with HTTP Basic authentication: the username is not checked (Settings shows “Any”), and the password is a `readonly` or `management` API key. Clients that send `Authorization: Bearer <key>` work too.

Catalogs include cover and original-download links for every supported book. Page-streaming (PSE) links are offered only when every page of a book uses the same JPEG, PNG or GIF MIME type; mixed formats, WebP and PDF books do not get PSE links.

## Komga-compatible API

Point Komga clients at `<origin>/komga` (for example `http://192.168.1.20:8780/komga`) with any username and an API key as the password. Use a `management` key if the client should sync reading progress back; a `readonly` key can browse and read only.

The server implements the Komga v1 REST subset used by the tested reader flows: libraries, series and books, page images, thumbnails, file downloads and read progress. It is **not** a complete Komga server: session authentication, arbitrary advanced filters and Mihon tracker binding are not supported. Test with the actual client version you plan to use.

## Browser extension

The Chrome extension (Chrome 103 or later) adds E(x)Hentai galleries to the download queue. Load `extension/` or the unzipped `mangacouch-extension-<version>.zip` from a release as an unpacked extension, then enter the library origin and a `downloads` key in its Settings page. See [`extension/README.md`](../extension/README.md) for details.

Komga progress updates that carry a page never undo a completed book, even with `completed: false`. Only `{"completed": false}` without a page, or `DELETE /komga/api/v1/books/{id}/read-progress`, marks a book unread. Marking a book read from a client (`completed: true` without a page) counts like the web menu's **Mark as read**: in **Finished in period**, not as reading activity; reaching the last page counts as reading. Rating-only edits, including bulk ratings, advance a client's observation without creating a false progress conflict.
