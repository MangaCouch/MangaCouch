# Clients and integrations

## API keys

Create keys in **Settings → Access & connections → API keys**, or with:

```sh
mangacouch --db <database> key-create --kind <kind> --label <name>
```

| Kind | Can do |
| --- | --- |
| `readonly` | Read-only (`GET`/`HEAD`) access; reading progress is not written back |
| `management` | Full API access, including progress sync from Komga clients |
| `downloads` | Add downloads and check their progress only (used by the extension) |

## OPDS

OPDS 1.2 catalogs are served at `/opds` and `/komga/opds/v1.2`. The username is ignored; use a `readonly` or `management` API key as the Basic-auth password.

Catalogs include cover and original-download links for every supported book. Page-streaming (PSE) links are offered only when every page of a book uses the same JPEG, PNG or GIF MIME type; mixed formats, WebP and PDF books do not get PSE links.

## Komga-compatible API

`/komga` implements the legacy read/list/page subset used by the tested reader flows. It is **not** a complete Komga 1.x server: session authentication, arbitrary advanced filters and Mihon tracker binding are not supported. Test with the actual client version you plan to use.

## Browser extension

The Chrome extension in `extension/` sends E(x)Hentai and nHentai gallery URLs to the library's download queue from the toolbar popup or the link context menu.

1. Open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked** and select `extension/` (or the unzipped `mangacouch-extension-<version>.zip` from a GitHub Release).
2. Create an API key of kind `downloads`.
3. In the extension's options page, enter the library origin (for example `http://localhost:8780`) and the key. The extension requests host permission for that origin only.

Adding a URL only queues it. Candidates are compared and any paid archive is approved in the library's **Downloads** page; the extension never purchases anything. See [`extension/README.md`](../extension/README.md) for details.
