# Library, storage and recovery

## Importing books

Upload books from the web app, or put CBZ/ZIP and PDF files in the library's `import/` folder and start a scan in **Settings → Library management**. From the command line:

```sh
uv run mangacouch --db data/state/library.sqlite scan --keep-source
uv run mangacouch --db data/state/library.sqlite scan-status
```

The CLI processes one bounded batch per invocation; repeat `scan` to continue. The web server advances scans in the background. Repeating `scan` keeps the unfinished scan's choice; pass `--keep-source` or `--no-keep-source` to change it.

- Successful scans without `--keep-source` consume verified originals from `import/`.
- Books with invalid or ambiguous metadata keep their source files and are marked **Needs review**.

## Supported formats

- **Archives:** ZIP / CBZ. **Documents:** PDF.
- **Pages:** GIF, JPG/JPEG, WebP and PNG.

Non-PDF reading returns the original image bytes, including GIF animation.

PDF pages render directly to lossless WebP, with no intermediate PNG and no automatic lossy fallback. Page and embedded-image dimensions are checked before rendering, which obeys the pixel budget and the WebP dimension limit.

## Caches

Cache files are disposable and excluded from snapshots.

| Cache | Format | Memory | Disk |
| --- | --- | --- | --- |
| PDF reading copies | lossless WebP | 64 MiB | 512 MiB |
| Thumbnails | lossy WebP (`Q=80`, `effort=3`; first frame of animated GIFs) | 16 MiB | 256 MiB |
| OPDS / Komga covers | JPEG, generated on demand | — | — |

## Backup and restore

Back up the state directory and the original books together. Snapshots (**Settings → Maintenance & recovery**, or `mangacouch snapshot <output>`) contain the database, configuration and credentials — not the comic files. Restore into a new directory and keep the matching `library.sqlite.sources.key`.

Removed books whose files were kept can be restored from the recycle bin for seven days. Finished import jobs, bulk operations and removal receipts older than seven days are cleaned up automatically; running operations and pending file deletions are kept.

### Lost source key

`serve --check` reports missing or mismatched source credentials. If the key cannot be recovered, stop the server and run:

```sh
mangacouch --db <database> sources-reset --confirm
```

This discards source credentials and saved remote download links while keeping settings and local books. Enter the credentials again, then restart.

## Crash safety

- Interrupted imports are recovered when the service acquires its worker lock, before workers start. `mangacouch recover` runs the same recovery manually. It is local import recovery only; uncertain paid download submissions are not replayed automatically.
- Files are published atomically: copy into a temporary file, verify, then rename without replacing an existing destination. Hard links are an optional optimization used only for application-owned temporary imports; scanned user files are always copied.
- State and library storage must support file locking and atomic renames.
- The root marker file identifies the library; do not delete it.
