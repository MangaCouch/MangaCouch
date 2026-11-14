# Library, storage and recovery

## Importing books

Upload books from the web app, or put CBZ/ZIP and PDF files in the library's `import/` folder and start a scan in **Settings → Library management → Scan library**. From the command line:

```sh
uv run mangacouch --db data/state/library.sqlite scan --keep-source
uv run mangacouch --db data/state/library.sqlite scan-status
```

The CLI processes one bounded batch per invocation; repeat `scan` to continue. The web server advances scans in the background. Repeating `scan` keeps the unfinished scan's choice; pass `--keep-source` or `--no-keep-source` to change it.

- By default a scan **deletes** each original from `import/` once its copy in the library is verified. Pass `--keep-source` (or tick the option in the web app) to keep them.
- Metadata travels with the archive: `<archive name>.eze.json` (an Eze export, also accepted as `<stem>.eze.json`) or `<archive name>.mangacouch.json` next to the file is imported with it and removed together with it.
- Under Docker the folder is `deploy/books/import/` in the repository checkout (`/data/books/import/` inside the container); the scan is started from the web app or with `docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch scan`.
- Books with invalid or ambiguous metadata keep their source files and are marked **Needs review**.

`import` and `index` add a single file from the command line: `import <file>` copies the file into the library and leaves the original alone; `index <file>` registers a file that already sits inside the books folder where it is, without copying or moving it.

Web upload import work has a 30-minute cooperative deadline, with a 30-second limit when waiting for an upload slot. If both slots stay occupied, the server returns HTTP 503 `library_busy` with `Retry-After: 5`; retry by uploading the files again. Active transfers retain their 60-second idle timeout. Retrying a failed upload requires uploading its files again. Runtime configuration stores only limit overrides; legacy default limits follow current defaults while custom values remain. JPEG pages named `.jfif` or `.jpe` are accepted. Recognized image suffixes whose contents are unsupported (such as TIFF, BMP, AVIF or JPEG XL) fail import with the member name rather than silently disappearing.

### Books folder layout

```text
books/
  .mangacouch-root.json            identifies the library; never delete it
  import/                          drop files here for a scan
  ab/ab12…ef.cbz                   a book, named after its SHA-256 (PDFs keep .pdf)
  ab/ab12…ef.cbz.mangacouch.json   its MangaCouch metadata
  ab/ab12…ef.cbz.eze.json          E(x)Hentai books: the source metadata in Eze format
```

- Imported books are stored by content hash, in a folder named after the first two hex digits; indexed books stay where they were. Do not rename or move the hashed files.
- The `<archive name>.mangacouch.json` sidecar holds the library's metadata for the book and is updated after every edit (**Settings → Library management → Metadata sync** lists pending writes and conflicts). Its format is described by [`schema/native-sidecar.schema.json`](../schema/native-sidecar.schema.json).
- The `<archive name>.eze.json` sidecar is only a snapshot of what the source last reported, written for E(x)Hentai books when the source is read. Editing a book in MangaCouch never changes it; edits go to the database and `.mangacouch.json` only.
- Files and folders whose names start with `.mangacouch-` (the root marker, locks, staging and temporary files) belong to the server: do not move, edit or delete them.

## Selection and collection drafts

Selections stay across pages of the same search. Changing the search or filters, including through the logo or browser history, clears the selection. Tag chips and free text share the 1,024-character query limit.

New collection and rule-editing dialogs keep unsaved drafts when you click outside. Closing or navigating away with changes asks you to keep editing or explicitly discard them. Saving successfully closes the dialog normally.

## Reading and duplicate detection

Opening a page from the thumbnail grid consumes the page number in the URL after the reader opens. Later reloads resume an unfinished book’s saved position. Unread books become Reading when their first page is ready. Reaching the last page makes a book Finished; rereading starts at page 1 and saves no further progress or reading events. Mark as unread clears that state and removes the book from the recently read list; its reading history stays. Manual marking and bulk marking do not count as reading activity.

**Settings → Library management → Duplicate books** first shows page indexing progress, then book comparison progress. An empty result is shown only when both passes have caught up. Comparison uses four 16-bit hash bands with a maximum Hamming distance of 3, plus the existing contrast, aspect, grayscale and ordered-coverage checks. Pages with more than 500 candidates are skipped to limit matches from common pages, and matches such a page found before it became that common are dropped. This favors close visual matches; larger changes may not be detected.

## Online sources

E-Hentai and ExHentai accounts, the download quality and source request settings live in **Settings → Online sources → Source accounts & downloads**.

### Download controls

**Maximum download size (GiB)** in source settings controls `download_limit_bytes`: a positive whole number of bytes, up to 1 PiB (1,048,576 GiB), with a 32 GiB default. It is separate from the local import archive limit.

Approved jobs with insufficient GP wait automatically and share a periodic account balance check; **Check balance now** also releases eligible jobs. A busy local database requeues work without spending a source retry attempt. Links such as `#downloads/<id>` open the page containing the requested task without duplicating it on the first page; scan and source-check tasks link to `#settings/scan` and `#settings/source-checks`.

**Download disk reserve (GiB)** controls `download_reserve_bytes`, defaulting to 10 GiB across all installations; like the download size above, the field uses GiB. Both download staging and library storage must retain this reserve. Low space parks affected transfers with their partial files; a shared check runs every five minutes and automatically resumes eligible jobs. Manually paused jobs remain paused.

Batch enqueue validates all lines, commits valid distinct galleries in one transaction and wakes workers once. Transfer progress is kept in memory and saved at phase changes or interruption; restart resumes from the actual partial file with its stored ETag. Download pages retain their position after actions and clamp to the last remaining page. Browsing later pages excludes newly added jobs until returning to the first page.

### Request budgets

E-Hentai and ExHentai share one usage bucket per IP address, and MangaCouch keeps a model of it. Every request adds points before it is sent: a gallery page (also comments, single-image and torrent pages) costs 1, an API call 1.5, a search or listing page (the front page, tags, uploaders, favorites, popular, watched, toplists) 4.5 and `archiver.php` 0.5. The points stay spent whatever the answer, and each redirect hop counts as a new request. The bucket drains at a steady rate, and **E-Hentai usage** in the source settings sets it: **Capacity (points)** (1100 by default, 100–3000), **Recovery per minute (points)** (13, 1–60) and **Max concurrency** (4, 1–8). The defaults are about 80% of what E-Hentai allowed in tests; browsing E-Hentai from the same IP spends from the same bucket, and MangaCouch cannot see that.

While at least half of the bucket is free, requests run at full concurrency. Below half they slow down step by step, until, with less than a tenth left, one request runs at a time at the rate the bucket drains (with the defaults, an API call about every 7 seconds, a page every 5 and a search every 21).

A request that does not fit waits; when the wait would be longer than a minute it stops instead, and the work is retried by itself when the wait is over, with nothing to click: downloads and source checks go back to their queues, comments a download or refresh could not read are fetched again in the background, and **Review against source** (the source links form) or **Update source comments** on a book page answer that they are queued and finish in the background even after the page is closed. Those two buttons wait at most 20 seconds for their answer; a slower one is queued the same way, to run about a minute later. If a queued refresh then fails for another reason (the gallery is gone, say), the book shows up as needing review with that error. Only **Verify** in the source settings, a one-off check, just says when it can be tried again (or that it timed out).

Background source checks stop at 80% used, leaving the rest to downloads and requests you make. The settings page shows how much is left and when it is full again.

A ban, a "too fast" warning or an HTTP 429 halts every E-Hentai request until it ends (a ban until it lifts), fills the bucket and scales its capacity and recovery by 0.67 for 24 hours; another one within that day scales them again, down to 0.3 of the settings, and each one restarts the 24 hours. Answers to requests that were already in flight only extend the halt. The settings page lists the latest ones. Three short answers in a row that MangaCouch does not recognize (not a ban page, not a gallery, not valid API data) pause E-Hentai requests for 10 minutes as a precaution, without filling or shrinking the bucket; this pause is not stored and is not listed. Server errors (5xx) never count toward it: they are retried like a dropped connection.

Archive downloads, thumbnails and other file servers spend no budget; each host takes four transfers at a time. Getting an archive costs about one point where fetching a 200-page gallery image by image would cost 200, so downloads always use archives. The budgets, the halts and the 24-hour reduction are stored in the database, so a restart neither clears nor resets them. Up to four archives transfer or import at once. Two separate metadata workers inspect and quote galleries. Low disk space delays only the affected transfer; metadata requests and smaller downloads can continue.

Batch confirmation freezes the selected titles, qualities, revisions and GP total in a confirmation dialog. Each result is reported separately; failed and ineligible items remain selected. Cancelled tasks stay in History until explicitly retried; Retry keeps the approved quality and price ceiling, while a task cancelled before approval still requires confirmation. Network/5xx failures and failed EH archive-link recovery share a budget of five failures per run; exhausted tasks wait for an explicit retry. Successful inspection or transfer and a new approval reset the budget; recovery quotes and repeated submissions do not reset it. A failed request with an unknown payment result keeps its recovery state. Rate limits, GP exhaustion, archive preparation and temporary local storage availability wait without spending that budget. GP waiters share one balance check with an increasing interval (up to 15 minutes); **Check balance now** probes immediately.

### After a restart

Pausing or cancelling a download interrupts a stalled response-body socket without waiting for its 20-second read timeout. Pause keeps the partial archive and its resume validator; cancel removes the partial file. An interrupted paid response still keeps the possible-charge marker and follows the existing recovery rules. Connection setup and waiting for response headers retain their existing timeouts.

Downloads continue by themselves after the server restarts, including the restart after saving the desktop launcher's settings: queued downloads stay queued, and a download that was being inspected, transferred or imported is queued again at the same step, a transfer resuming from the bytes already received. An approved paid request whose outcome is unknown automatically reads the archive page, reuses a prepared link if available, or submits again at no more than the approved price. Reading the price does not pay. A higher price requires new approval and shows that the previous request may already have been charged. Queued source checks continue as well.

### Source updates

A book linked to an E(x)Hentai gallery is checked against its source when you choose **Check metadata online** in the shelf menu (one book or a selection); importing a book does not check it. Books checked once are checked again in the background every 90 days. Checks run 25 at a time: one E(x)Hentai API request fetches the metadata of the whole batch. When the source lists a newer version of the gallery, the book page shows a **Newer version** link next to the reading state.

Checks read gallery metadata only, unless **Fetch comments in background source checks too (one more request per book)** is on (it is off by default, and needs **Fetch source comments**); the **Update source comments** button in a book's **Source comments** panel fetches them on request. Stored comments change only when a comment fetch completes: with comments off, or when the comment page fails (rate limit or warning page, blank page), the comments saved earlier stay as they were. Comments that a rate limit kept back are fetched again in the background once it allows. Without an ExHentai cookie, the comments of an ExHentai gallery are read from its e-hentai.org page (the metadata already comes from the E-Hentai API); a gallery that exists only on ExHentai then reports that a login is needed.

Online checks automatically ignore retired nH sources; local metadata, source links and cached comments remain available. Bulk source checks report these books as ignored rather than successfully queued. Changes to an online rating alone do not reopen a confirmed metadata review.

The detail page’s **Review metadata** opens the local review dialog; it does not queue an online check.

### Source network

Source requests ignore system and environment proxy variables; set **Proxy URL** in the source settings instead (`socks5h://` lets the proxy resolve host names). Without a proxy, every source host must resolve to a public address, and a host that resolves to a private one fails with "resolved to a private network address". A TUN proxy in fake-IP mode (Clash, Surge, ...) answers DNS with addresses in `198.18.0.0/15`; turn on **Allow fake-IP DNS answers (198.18.0.0/15)** to accept that one range for host names. Other private ranges, IP-address links and every redirect stay checked.

## Reading history

Reading history is kept indefinitely by default. In **Settings → Preferences**, choose one or three years to periodically delete older reading events, in bounded batches. This deletion is permanent, but keeps each book’s current progress and rating. Statistics and history use the browser’s IANA time zone, including historical daylight-saving transitions.

Marking a book completed from a menu counts in **Finished in period**, without creating a reading visit or an active day; marking it unread again takes that completion back out. Earlier manual completions without a recorded date are not backfilled.

## Supported formats

- **Archives:** ZIP / CBZ. **Documents:** PDF.
- **Pages:** GIF, JPG/JPEG, WebP and PNG.

Non-PDF reading returns the original image bytes, including GIF animation.

PDF pages render directly to lossless WebP, with no intermediate PNG and no automatic lossy fallback. Page and embedded-image dimensions are checked before rendering, which obeys the pixel budget and the WebP dimension limit.

### Limits

Fixed limits protect the server from damaged or hostile files; a book beyond them is refused at import:

| Limit | Value |
| --- | --- |
| Archive size | unlimited by default; `--max-bytes` / `MANGACOUCH_MAX_BYTES` for uploads and `import/`, 32 GiB for downloads |
| Web upload | 2 GiB by default (`--upload-bytes` / `MANGACOUCH_UPLOAD_BYTES`), never above the archive size limit |
| Pages per book | 20,000 |
| Entries in a ZIP | 50,000 (32 MiB of central directory) |
| One page | 128 MiB |
| All pages unpacked | 32 GiB |
| Page dimensions | 32,768 px per side, 178,956,970 pixels (about 179 MP) |
| PDF page as rendered | scaled down to at most 16 megapixels |

## Caches

Cache files are disposable and excluded from backups. They live next to the database in `.mangacouch-cache-library.sqlite/`; deleting that folder while the server is stopped only costs re-rendering.

After books are added the server renders every missing cover (each book's first page) in the background, a few at a time between scans and imports, so the shelf is served from the cache; once covers are done it renders WebP thumbnails of every page the same way. Progress shows in the task queue and in **Settings → Library management → Cache**, where **Render missing covers** and **Render missing thumbnails** start a full pass on request. The thumbnail pass stops when the cache is nearly full rather than evicting covers; raise the budget with `--cache-bytes` (default 20 GiB) to hold every page. When the cache is full the oldest entries go first.

| Cache | Format | Memory | Disk |
| --- | --- | --- | --- |
| PDF reading copies | lossless WebP | 64 MiB | 512 MiB |
| Covers and page thumbnails | lossy WebP (`Q=80`, `effort=3`; first frame of animated GIFs) | 16 MiB | 20 GiB (`--cache-bytes` / `MANGACOUCH_CACHE_BYTES`) |
| OPDS / Komga covers | JPEG, rendered on demand | shared with covers | shared with covers |

## Backup and restore

Back up the state directory and the original books together. A backup (**Settings → Maintenance & recovery → Backup & restore → Download library backup**, or `mangacouch --db <database> snapshot <backup.zip>`) contains the database, configuration and source credentials with their key — not the comic files. Building a backup in the web app has a 30-minute deadline (see [Busy library](#busy-library)). Derived page fingerprints and duplicate comparisons are omitted and rebuilt automatically after restore.

To copy the state directory itself, stop the server first (or download a backup instead): a copy of a live database can miss changes still in its `-wal` file.

Restore on the server into a new directory, then start from the restored database:

```sh
mangacouch restore <backup.zip> <new-dir>
mangacouch --db <new-dir>/library.sqlite serve --origin http://localhost:8780
```

The new directory holds `library.sqlite`, `runtime.json` (which records the books folder) and, when the backup has source credentials, `library.sqlite.sources.key`; keep them together. If the books folder has moved, pass it before the command: `mangacouch --root <books> restore <backup.zip> <new-dir>`. A restored library signs every device out, pauses unfinished downloads and online source requests, and stops pending file deletions. Check the account and any current source-site suspension before enabling online requests in **Settings → Online sources**, then resume downloads explicitly. An uncertain paid download keeps its approval and first checks for an existing link when resumed. Stopped deletions are listed in the record recycle bin; restoring an older backup never resumes them automatically.

### Restoring under Docker

1. Stop the service: `docker compose --env-file .env -f deploy/compose.yaml stop`.
2. Copy the backup zip into `deploy/state/`, which the container sees as `/data/state/`.
3. Restore it into a new folder there:

   ```sh
   docker compose --env-file .env -f deploy/compose.yaml run --rm mangacouch restore /data/state/backup.zip /data/state/restored
   ```

4. In `deploy/state/`, delete `library.sqlite`, `library.sqlite-wal`, `library.sqlite-shm`, `library.sqlite.sources.key` and `runtime.json`, then move everything from `deploy/state/restored/` up into `deploy/state/`. Delete the old `-wal` and `-shm` files even when they look empty: SQLite would apply them to the restored database. Delete the old `.sources.key` even when the backup brings none, so a stale key is not reused. If stored source credentials cannot be decrypted because their key is missing or mismatched, the server still starts and local books remain usable; only online sources are disabled (see [Lost source key](#lost-source-key)).
5. Start the service again: `docker compose --env-file .env -f deploy/compose.yaml up -d`.

### Restoring with the desktop launcher

1. Choose **Stop Server** in the tray menu.
2. Run `restore` with the command-line program that ships with the launcher: `mangacouch\mangacouch.exe` from the Windows archive, or `/Applications/MangaCouch.app/Contents/MacOS/mangacouch` on macOS:

   ```sh
   /Applications/MangaCouch.app/Contents/MacOS/mangacouch restore ~/Downloads/backup.zip ~/MangaCouch/restored-state
   ```

3. Open **Settings…**, choose the new folder as the data folder and save. The launcher starts the restored library; the old data folder stays as it was.

### Repairing a changed book

A book whose file was modified on disk is shown as changed and cannot be read. To repair it, import the original file again: upload it, `import` it from the command line or put it in `import/` and scan. When its content matches what the library recorded, the modified file is renamed to `<name>.changed` (`<name>.2.changed` and so on if that name is taken) and the original takes its place; the book's metadata and reading state stay as they were. MangaCouch never deletes `.changed` files and ignores them otherwise; delete them yourself once they are no longer needed. `index` does not repair a book; it refuses a copy of a changed book's original.

### Recycle bin

Removed books whose files were kept can be restored from **Settings → Maintenance & recovery → Record recycle bin** for seven days. Choosing **Delete files** requires the archive to still hold the book's content: when its size or modification time changed since it was last verified, the removal preview hashes it again, and only a file whose content differs is refused. Changed or unreadable archives can still have their records removed while keeping the files. Completed file deletion cannot be undone. The recycle bin checks files when you request restoration, so its restore action does not guarantee that a file is still present. Finished import jobs, bulk operations and removal receipts older than seven days are cleaned up automatically, and finished downloads leave the download history after 90 days; running operations and pending file deletions are kept.

### Lost source key

Source credentials are encrypted with `library.sqlite.sources.key`, kept next to the database. If that file is missing or does not match the database, the library still starts and stays fully usable, but online sources (downloads and source checks) are turned off; `serve --check` and `doctor` report it as `source_key_lost`.

If the key cannot be recovered, choose **Reset source credentials** in **Settings → Online sources**. It deletes the stored credentials, pauses downloads that hold a saved remote download link (the link is dropped), and writes a new key; settings and local books are kept. Online requests remain paused after the reset, including when the old proxy could not be decrypted. Re-enter the cookies and proxy, check the settings, then choose **Resume online sources** explicitly.

Without the web app, stop the server and run the same reset from the command line, then start it again:

```sh
mangacouch --db <database> sources-reset --confirm
```

## Busy library

Database writes from requests wait up to 8 seconds before returning 503 `database_busy`. Background threads can wait up to 30 seconds. A busy database leaves scan items and metadata writes pending instead of marking them permanently failed. Source checks retry local contention without spending their network retry allowance. Requests that wait for library file operations have a 25-second cooperative deadline and return 503 `library_busy` with `Retry-After: 5` if it expires; hashing and copying also check that deadline. Two kinds of work get 30 minutes instead: importing a web upload and building a web backup (**Download library backup**). Background operation-lock waits are cancelled during shutdown.

## Crash safety

- If a download imports successfully but source details cannot be saved, the download is completed with a pending-details notice and the source metadata is queued for a later refresh.
- Download retries reuse their persisted import task. Cancelling or clearing a download abandons its unpublished import, so later recovery cannot add that cancelled book. An already committed book wins over cancellation and the download is completed.
- Failed or interrupted imports, including ready imports left by a crash, are failed and their staging files and unpublished archives are removed when storage is available. They never publish during hourly maintenance. Upload again or retry the original source explicitly. `mangacouch recover` performs this cleanup manually; it does not resume local imports. Approved source submissions with an unknown payment result have their separate download recovery workflow.
- Files are validated before being registered: copy into a temporary file, verify, then publish without replacing an existing destination. On filesystems lacking atomic no-replace rename and hard links, an incomplete exclusive copy is tracked for cleanup and never registered as a book. Hard links are an optional optimization used only for application-owned temporary imports; scanned user files are always copied.
- Filesystems without a no-replace rename (Docker Desktop and colima shared folders on macOS, NFS, SMB and other FUSE mounts) are supported: publishing falls back to a hard link, or, without hard links, copies into an exclusively opened destination without replacing its directory entry, so an existing file is still never replaced. Directory syncs those filesystems cannot do are skipped.
- State and library storage must support file locking. Keep the state folder (the database) on a local disk: SQLite's write-ahead log is not safe on network shares. The books folder may be a network share.
- `.mangacouch-root.json` in the books folder identifies the library; do not delete it.
