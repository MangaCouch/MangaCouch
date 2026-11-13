// An in-memory MangaCouch backend for the DOM tests: `window.fetch` answers like the
// HTTP API and `window.fixture` holds its state for tests to seed and inspect.
(() => {
  const names = [
    "山间来信",
    "The Quiet Observatory",
    "海边的慢日子",
    "雨过天晴之后",
    "小小宇宙旅行记",
    "A Walk in November",
  ];
  const books = names.map((title, index) => ({
    id: `fixture-${index}`,
    metadata: {
      title,
      original_title: null,
      language: index === 1 ? "English" : "中文",
      translated: null,
      work_type: null,
      tags: [],
      source: null,
      uploader: null,
      uploaded_at: null,
      source_rating: null,
      declared_page_count: null,
      declared_byte_size: null,
      parent: null,
      newer_versions: [],
      field_sources: {},
      protected_fields: [],
    },
    metadata_revision: 1,
    thumbnail_revision: 0,
    personal_revision: 1,
    content: {
      sha256: "0".repeat(64),
      original_filename: `story-${index}.cbz`,
      relative_path: `story-${index}.cbz`,
      format: "cbz",
      byte_size: 1024 * 600,
      page_count: 4,
    },
    personal: {
      rating: index === 1 ? 4.5 : null,
      resume_page: null,
      completed: false,
    },
    review_state: "needs_review",
    availability: "available",
  }));
  const fixture = (window.fixture = {
    books,
    /** Metadata file sync state by book id (the server keeps it beside the book). */
    sidecars: Object.fromEntries(
      books.map((book) => [
        book.id,
        { pending_revision: 1, synced_revision: 0, last_error: null },
      ]),
    ),
    cache: {
      cache: { budget_bytes: 20 * 1024 ** 3, used_bytes: 3 * 1024 ** 2 },
      covers: {
        state: "idle",
        done: 6,
        total: 6,
        rendered: 6,
        failed: 0,
        full: false,
        finished_at: 1700000000,
      },
      thumbnails: {
        state: "idle",
        done: 6,
        total: 6,
        rendered: 120,
        failed: 0,
        full: false,
        finished_at: 1700000000,
      },
    },
    warmRequests: { covers: 0, thumbnails: 0 },
    preferences: {
      revision: 0,
      settings: {
        title_preference: "title",
      },
    },
    preferencesDelay: 0,
    preferencesFailure: false,
    preferencesLoseResponse: false,
    syncCalls: [],
    syncOutcomes: {},
    syncDelay: 0,
    syncLoseResponse: false,
    syncStatusFailure: false,
    syncFailure: null,
    syncPageSize: 50,
    syncBatchCap: Infinity,
    keySequence: 0,
    keyCreateLoseResponse: false,
    keyCreateDelay: 0,
    keyCreateFailure: null,
    credentialListFailure: false,
    keyRevokeLoseResponse: false,
    passcodeLoseResponse: false,
    passcodeFailure: null,
    passcodeChanges: 0,
    diagnosticsFailure: false,
    snapshotFailure: null,
    /** A promise the snapshot build waits for. */
    snapshotBuild: null,
    reviewProofs: {},
    reviewChecks: [],
    failReview: false,
    credentials: [
      {
        id: "fixture-session",
        kind: "session",
        label: "Web browser",
        created_at: Math.floor(Date.now() / 1000) - 3600,
        expires_at: Math.floor(Date.now() / 1000) + 86400 * 7,
        revoked_at: null,
      },
      {
        id: "fixture-second",
        kind: "session",
        label: "Living room tablet",
        created_at: Math.floor(Date.now() / 1000) - 7200,
        expires_at: Math.floor(Date.now() / 1000) + 86400 * 7,
        revoked_at: null,
      },
      {
        id: "fixture-api",
        kind: "management",
        label: "Automation",
        created_at: Math.floor(Date.now() / 1000),
        expires_at: null,
        revoked_at: null,
      },
    ],
    collections: [
      {
        id: "fixture-series",
        name: "周末慢读",
        revision: 1,
        book_ids: books.slice(0, 3).map((book) => book.id),
      },
    ],
    scan: null,
    scanOffline: false,
    maintenanceStates: {},
    maintenanceDelay: 0,
    maintenanceOffline: false,
    maintenanceBatchSize: 100,
    maintenanceFailureAfter: null,
    maintenanceChecks: 0,
    removalPlans: {},
    removals: [],
    sidecarConflicts: [],
    bulkJobs: {},
    removalPresence: "present",
    removalBlocked: [],
    removalConflict: false,
    removalDelay: 0,
    removalLoseResponse: false,
    removalBackupFailure: false,
    sourceConfig: {
      settings: {
        online_paused: false,
        download_limit_bytes: 34359738368,
        download_reserve_bytes: 10 * 1024 ** 3,
        allow_fake_ip: false,
        comments: true,
        background_comments: false,
        eh_budget_capacity: 1100,
        eh_budget_leak_per_min: 13,
        eh_max_concurrency: 4,
        user_agent: "fixture",
        auto_free: false,
        default_quality: "org",
      },
      configured: { eh_cookie: true },
      revision: 2,
      key_lost: false,
      rate_events: [],
    },
    sourceResets: 0,
    sourceBudget: {
      ehentai: {
        level: 308,
        capacity: 1100,
        leak_per_min: 13,
        remaining: 0.72,
        empty_in: 1422,
        penalty: 1,
        penalty_until: null,
      },
    },
    sourceSaves: [],
    sourceCooldowns: {},
    downloads: [],
    downloadActions: [],
    downloadListFailure: false,
    duplicatePairs: [],
    duplicateComparing: { done: 5, total: 5 },
    ignoredPairs: [],
    aliases: [],
    aliasSaves: [],
    authenticated: false,
    saves: [],
    requests: [],
    failSave: false,
    failPage: null,
    pageDelay: 0,
    // Reading events as the server records them: { id, book_id, created_at, page,
    // completed (0 or 1), kind ("progress", "completed" or "reset") }.
    readingEvents: [],
    activityFailure: false,
  });
  /** A book's metadata file sync state; books added later start with an unsynced file. */
  const sidecarOf = (book) =>
    (fixture.sidecars[book.id] ??= {
      pending_revision: 1,
      synced_revision: 0,
      last_error: null,
    });
  /** Page images are opaque bytes to the client; their content never matters here. */
  const page = (book, index) =>
    new Blob([`${book.id}/${index}`], { type: "image/png" });
  const json = (data, status = 200) =>
    new Response(
      JSON.stringify(
        status >= 400
          ? {
              error:
                {
                  401: "invalid_credentials",
                  403: "read_only",
                  404: "not_found",
                  409: "revision_conflict",
                  422: "invalid_input",
                  429: "rate_limited",
                  503: "root_unavailable",
                }[status] ?? "internal_error",
              message: "Fixture request failed",
              ...data,
            }
          : data,
      ),
      {
        status,
        headers: { "Content-Type": "application/json" },
      },
    );
  const noContent = () => new Response(null, { status: 204 });
  // Page cursors are opaque to clients; these need URL encoding like the server's.
  const cursor = (value) => JSON.stringify([value]);
  const uncursor = (text, fallback) => (text ? JSON.parse(text)[0] : fallback);
  /**
   * The local calendar day of a timestamp in `zone`: an IANA time zone name, as the page
   * sends, or minutes east of UTC (`utc_offset`) when there is none.
   */
  const localDay = (seconds, zone) => {
    if (typeof zone === "number")
      return new Date((seconds + zone * 60) * 1000).toISOString().slice(0, 10);
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("en-US", {
        timeZone: zone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      })
        .formatToParts(new Date(seconds * 1000))
        .map((part) => [part.type, part.value]),
    );
    return `${parts.year}-${parts.month}-${parts.day}`;
  };
  /** The zone of a statistics or history request; null where the server answers 422. */
  function requestZone(url) {
    const name = url.searchParams.get("timezone");
    if (!name) return Number(url.searchParams.get("utc_offset") ?? 0);
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: name });
      return name;
    } catch {
      return null;
    }
  }
  /** The calendar day `delta` days after the ISO date `day`. */
  const shiftDay = (day, delta) =>
    new Date(Date.parse(`${day}T00:00:00Z`) + delta * 86400_000)
      .toISOString()
      .slice(0, 10);
  /** Schema 21: a manual completion (`is_reading` 0) is not reading; other events are. */
  const isReading = (event) => (event.is_reading ?? 1) === 1;
  /** GET /api/statistics: library totals and the reading of the last `days` local days. */
  function statistics(days, zone) {
    const today = localDay(Math.floor(Date.now() / 1000), zone);
    const first = shiftDay(today, 1 - days);
    const counted = fixture.readingEvents.filter(
      (event) =>
        localDay(event.created_at, zone) >= first && event.kind !== "reset",
    );
    const reading = counted.filter(isReading);
    const perDay = new Map();
    for (const event of reading) {
      const day = localDay(event.created_at, zone);
      const entry = perDay.get(day) ?? { books: new Set(), updates: 0 };
      entry.books.add(event.book_id);
      entry.updates++;
      perDay.set(day, entry);
    }
    const languages = new Map();
    for (const book of books) {
      const language = book.metadata.language || "unknown";
      languages.set(language, (languages.get(language) ?? 0) + 1);
    }
    return {
      library: {
        books: books.length,
        pages: books.reduce((sum, book) => sum + book.content.page_count, 0),
        bytes: books.reduce((sum, book) => sum + book.content.byte_size, 0),
        completed: books.filter((book) => book.personal.completed).length,
        in_progress: books.filter(
          (book) =>
            !book.personal.completed && book.personal.resume_page != null,
        ).length,
        rated: books.filter((book) => book.personal.rating != null).length,
      },
      period: {
        days,
        books: new Set(reading.map((event) => event.book_id)).size,
        active_days: perDay.size,
        // Manual completions count as finished books, not as reading.
        completed: new Set(
          counted
            .filter((event) => event.kind === "completed")
            .map((event) => event.book_id),
        ).size,
        updates: reading.length,
      },
      daily: Array.from({ length: days }, (_, index) => {
        const day = shiftDay(first, index);
        const entry = perDay.get(day);
        return {
          day,
          books: entry?.books.size ?? 0,
          updates: entry?.updates ?? 0,
        };
      }),
      languages: [...languages]
        .map(([language, count]) => ({ language, books: count }))
        .sort(
          (a, b) => b.books - a.books || a.language.localeCompare(b.language),
        )
        .slice(0, 20),
    };
  }
  /**
   * GET /api/reading-history: reading events newest first, each with its book; manual
   * completions and removed books are skipped.
   */
  function readingHistory(after, limit) {
    const events = fixture.readingEvents
      .filter(isReading)
      .sort((a, b) => b.created_at - a.created_at || b.id - a.id);
    const start = after
      ? events.findIndex((event) => event.id === uncursor(after)) + 1
      : 0;
    const page = events.slice(start, start + limit);
    return {
      items: page.flatMap(({ is_reading: _, ...event }) => {
        const book = books.find((item) => item.id === event.book_id);
        return book ? [{ ...event, book }] : [];
      }),
      after: start + limit < events.length ? cursor(page.at(-1).id) : null,
    };
  }
  window.fetch = async (input, options = {}) => {
    const url = new URL(input, "http://fixture.local"),
      method = options.method || "GET";
    fixture.requests.push({
      path: url.pathname,
      method,
      query: url.searchParams,
    });
    options.signal?.throwIfAborted();
    if (url.pathname === "/api/auth/login") {
      fixture.authenticated = true;
      return json({ kind: "session" });
    }
    if (!fixture.authenticated)
      return json({ error: "invalid_credentials" }, 401);
    if (url.pathname === "/api/auth/me")
      return json({
        id: "fixture-session",
        kind: "session",
        rotation_due: false,
        label: "Fixture browser",
        expires_at: Math.floor(Date.now() / 1000) + 86400,
      });
    if (url.pathname === "/api/auth/rotate") return json({ rotated: false });
    if (url.pathname === "/api/auth/logout") {
      fixture.authenticated = false;
      return noContent();
    }
    const body = options.body ? JSON.parse(options.body) : {};
    if (url.pathname === "/api/books/selection-state") {
      const target = new URL("/api/books", "http://fixture");
      for (const [key, value] of Object.entries({
        ...body.options,
        query: body.query ?? "",
        limit: 100000,
      }))
        target.searchParams.set(key, String(value));
      const response = await window.fetch(target.href);
      const { items } = await response.json();
      return json({
        unfinished: items.some(
          (book) =>
            !(body.exclude ?? []).includes(book.id) &&
            (!body.book_ids || body.book_ids.includes(book.id)) &&
            !book.personal.completed,
        ),
      });
    }
    if (url.pathname === "/api/thumbnails/refresh") {
      const selected = books.filter((book) => body.book_ids.includes(book.id));
      for (const book of selected) book.thumbnail_revision++;
      return json({ refreshed: selected.length });
    }
    const expectedRevision = url.searchParams.has("expected_revision")
      ? Number(url.searchParams.get("expected_revision"))
      : null;
    if (url.pathname === "/api/preferences") {
      if (fixture.preferencesDelay)
        await new Promise((resolve) =>
          setTimeout(resolve, fixture.preferencesDelay),
        );
      options.signal?.throwIfAborted();
      if (fixture.preferencesFailure)
        return json({ error: "storage_unavailable" }, 503);
      if (method === "PUT") {
        if (expectedRevision !== fixture.preferences.revision)
          return json({ error: "revision_conflict" }, 409);
        if (
          JSON.stringify(body.settings) !==
          JSON.stringify(fixture.preferences.settings)
        ) {
          fixture.preferences = {
            revision: fixture.preferences.revision + 1,
            settings: structuredClone(body.settings),
          };
        }
        if (fixture.preferencesLoseResponse) {
          fixture.preferencesLoseResponse = false;
          throw new TypeError("mock lost response");
        }
      }
      return json(fixture.preferences);
    }
    if (url.pathname === "/api/tasks") {
      const open = fixture.downloads.filter(
        (job) => !["done", "cancelled"].includes(job.state),
      );
      return json({
        downloads: {
          blocked: fixture.sourceBlocked ?? null,
          running: open.filter((job) =>
            ["inspecting", "submitting", "downloading", "importing"].includes(
              job.state,
            ),
          ).length,
          // Like the server: a job retrying more than a minute from now (backoff, a GP
          // top-up) is not about to run, so it is not "waiting".
          waiting: open.filter(
            (job) =>
              job.state === "queued" &&
              (job.retry_at ?? 0) <= Math.floor(Date.now() / 1000) + 60,
          ).length,
          needs_you: fixture.downloads.filter((job) =>
            ["needs_approval", "verification_required", "error"].includes(
              job.state,
            ),
          ).length,
          completed: fixture.downloads.filter((job) => job.state === "done")
            .length,
          total: fixture.downloads.length,
          items: [...open]
            .sort(
              (a, b) =>
                Number(
                  [
                    "inspecting",
                    "submitting",
                    "downloading",
                    "importing",
                  ].includes(b.state),
                ) -
                Number(
                  [
                    "inspecting",
                    "submitting",
                    "downloading",
                    "importing",
                  ].includes(a.state),
                ),
            )
            .slice(0, 8)
            .map((job) => ({
              id: job.id,
              state: job.state,
              input_url: job.input_url,
              received: job.received,
              total: job.total,
              title: null,
              retry_at: job.retry_at,
              attempts: job.attempts,
              error_code: job.error_code,
            })),
        },
        scan: null,
        source_checks: {},
        cooldowns: fixture.sourceCooldowns,
        covers: fixture.cache.covers,
        thumbnails: fixture.cache.thumbnails,
      });
    }
    if (url.pathname === "/api/settings/summary")
      return json({
        version: "main-0123456",
        review: 3,
        sync: { pending: 2 },
        indexed_pages: 8,
        total_pages: 20,
        translations: 1,
        devices: 1,
        recycle: 0,
        cache: fixture.cache,
      });
    if (url.pathname === "/api/diagnostics") {
      if (fixture.diagnosticsFailure) return json({}, 503);
      return json({
        ready: !fixture.scanOffline,
        books: books.length,
        database: "/library/state/library.sqlite",
        library_root: "/library/漫画与图像小说",
        availability_last_known: { available: books.length },
        imports: { done: books.length, ready: 1 },
        sidecars: { pending: books.length, errors: 0 },
        conflicts: 0,
        scan: fixture.scan
          ? { id: fixture.scan.id, state: fixture.scan.state }
          : null,
      });
    }
    const removalToken = (book) =>
      JSON.stringify({
        metadata: book.metadata_revision,
        personal: book.personal_revision,
        collections: fixture.collections
          .filter((item) => item.book_ids.includes(book.id))
          .map((item) => [item.id, item.revision]),
        presence: fixture.removalPresence,
        blocked: fixture.removalBlocked,
      });
    const removalPreview = (book) => ({
      id: crypto.randomUUID(),
      book_id: book.id,
      token: removalToken(book),
      title: book.metadata.title,
      filename: book.content.original_filename,
      relative_path: book.content.relative_path,
      presence: fixture.removalPresence,
      personal: structuredClone(book.personal),
      collections: fixture.collections.filter((item) =>
        item.book_ids.includes(book.id),
      ),
      unsynced_metadata:
        sidecarOf(book).pending_revision > sidecarOf(book).synced_revision,
      pending_conflicts: 0,
      blocked: [...fixture.removalBlocked],
    });
    if (url.pathname === "/api/removals/check") {
      if (fixture.maintenanceDelay)
        await new Promise((resolve) =>
          setTimeout(resolve, fixture.maintenanceDelay),
        );
      options.signal?.throwIfAborted();
      fixture.maintenanceChecks++;
      if (
        fixture.maintenanceOffline ||
        (fixture.maintenanceFailureAfter !== null &&
          fixture.maintenanceChecks > fixture.maintenanceFailureAfter)
      )
        return json({}, 503);
      const sorted = [...books].sort((a, b) => a.id.localeCompare(b.id));
      const through = body.through_id ?? sorted.at(-1)?.id ?? "";
      const remaining = sorted.filter(
        (book) => book.id > uncursor(body.after, "") && book.id <= through,
      );
      const chunk = remaining.slice(
        0,
        Math.min(body.limit || 100, fixture.maintenanceBatchSize),
      );
      return json({
        checked: chunk.length,
        through_id: through,
        after: remaining.length > chunk.length ? cursor(chunk.at(-1).id) : null,
        items: chunk
          .filter(
            (book) =>
              fixture.maintenanceStates[book.id] &&
              fixture.maintenanceStates[book.id] !== "available",
          )
          .map((book) => ({
            book_id: book.id,
            title: book.metadata.title,
            relative_path: book.content.relative_path,
            state: fixture.maintenanceStates[book.id],
          })),
      });
    }
    const selectedToken = (ids) =>
      JSON.stringify(
        ids
          .map((id) => books.find((book) => book.id === id))
          .filter(Boolean)
          .map(removalToken),
      );
    if (
      url.pathname === "/api/removals/selected/preview" &&
      method === "POST"
    ) {
      const ids = [...new Set(body.book_ids)];
      const id = crypto.randomUUID();
      const plan = {
        id,
        book_ids: ids,
        count: ids.length,
        token: selectedToken(ids),
        blocked: [...fixture.removalBlocked],
        items: ids
          .map((id) => books.find((book) => book.id === id))
          .filter(Boolean)
          .map(removalPreview),
      };
      fixture.removalPlans[id] = plan;
      return json(plan);
    }
    if (url.pathname === "/api/removals/selected" && method === "POST") {
      const old = fixture.removals.find((item) => item.id === body.id);
      if (old) return json(old);
      const plan = fixture.removalPlans[body.id];
      if (
        !plan ||
        plan.token !== body.token ||
        selectedToken(body.book_ids) !== body.token
      )
        return json({}, 409);
      if (!body.confirmed || plan.blocked.length) return json({}, 422);
      const saved = books
        .filter((book) => body.book_ids.includes(book.id))
        .map((book) => ({
          book: structuredClone(book),
          collections: fixture.collections
            .filter((item) => item.book_ids.includes(book.id))
            .map((item) => item.id),
        }));
      for (const item of saved) {
        books.splice(
          books.findIndex((book) => book.id === item.book.id),
          1,
        );
        for (const collection of fixture.collections)
          collection.book_ids = collection.book_ids.filter(
            (id) => id !== item.book.id,
          );
      }
      const receipt = {
        id: body.id,
        state: "removed",
        created_at: Math.floor(Date.now() / 1000),
        cursor: fixture.removals.length + 1,
        restored: false,
        delete_pending: false,
        files_kept: !body.delete_file,
        items: saved.map(({ book }) => ({
          book_id: book.id,
          title: book.metadata.title,
          filename: book.content.original_filename,
          relative_path: book.content.relative_path,
          file: body.delete_file ? "deleted" : "kept",
          restored: false,
        })),
        saved,
      };
      fixture.removals.push(receipt);
      return json(receipt);
    }
    if (url.pathname === "/api/removals/preview") {
      const book = books.find((item) => item.id === body.book_id);
      if (!book) return json({}, 404);
      const preview = removalPreview(book);
      fixture.removalPlans[preview.id] = preview;
      return json(preview);
    }
    if (url.pathname === "/api/removals") {
      if (method === "GET") {
        const limit = Number(url.searchParams.get("limit") || 50),
          before = uncursor(url.searchParams.get("after"), Infinity);
        const items = [...fixture.removals]
          .reverse()
          .filter((item) => item.cursor < before)
          .slice(0, limit);
        return json({
          items,
          after: items.length === limit ? cursor(items.at(-1).cursor) : null,
        });
      }
      const existing = fixture.removals.find((item) => item.id === body.id);
      if (existing) return json(existing);
      if (fixture.removalDelay)
        await new Promise((resolve) =>
          setTimeout(resolve, fixture.removalDelay),
        );
      const plan = fixture.removalPlans[body.id],
        book = books.find((item) => item.id === body.book_id);
      if (
        !plan ||
        !book ||
        fixture.removalConflict ||
        body.token !== removalToken(book)
      )
        return json({}, 409);
      if (body.confirmed !== true || plan.blocked.length) return json({}, 422);
      if (fixture.removalBackupFailure) return json({}, 503);
      books.splice(books.indexOf(book), 1);
      for (const collection of fixture.collections)
        if (collection.book_ids.includes(book.id)) {
          collection.book_ids = collection.book_ids.filter(
            (id) => id !== book.id,
          );
          collection.revision++;
        }
      const receipt = {
        id: body.id,
        book_id: book.id,
        title: plan.title,
        filename: plan.filename,
        created_at: Math.floor(Date.now() / 1000),
        files_kept: !body.delete_file,
        delete_pending: false,
        restored: false,
        items: [
          {
            book_id: book.id,
            title: plan.title,
            filename: plan.filename,
            relative_path: plan.relative_path,
            file: body.delete_file ? "deleted" : "kept",
            restored: false,
          },
        ],
        saved: [
          {
            book: structuredClone(book),
            collections: plan.collections.map((item) => item.id),
          },
        ],
        state: "removed",
        cursor: fixture.removals.length + 1,
      };
      fixture.removals.push(receipt);
      if (fixture.removalLoseResponse) {
        fixture.removalLoseResponse = false;
        throw new TypeError("mock lost response");
      }
      return json(receipt);
    }
    if (url.pathname.startsWith("/api/removals/")) {
      const receipt = fixture.removals.find(
        (item) => item.id === url.pathname.split("/")[3],
      );
      if (!receipt) return json({}, 404);
      const action = url.pathname.split("/")[4];
      if (action === "cancel-deletion" && method === "POST")
        receipt.delete_pending = false;
      else if (action === "restore" && method === "POST") {
        if (receipt.delete_pending || !receipt.files_kept) return json({}, 409);
        for (const item of receipt.saved ?? [])
          if (!books.some((book) => book.id === item.book.id)) {
            books.push(structuredClone(item.book));
            for (const id of item.collections) {
              const collection = fixture.collections.find((c) => c.id === id);
              if (collection && !collection.book_ids.includes(item.book.id))
                collection.book_ids.push(item.book.id);
            }
          }
        receipt.restored = true;
        for (const item of receipt.items) item.restored = true;
      } else if (action || method !== "GET") return json({}, 404);
      return json(receipt);
    }
    if (url.pathname === "/api/duplicates")
      return json({
        items: fixture.duplicatePairs.map(([left_id, right_id]) => ({
          left_id,
          right_id,
          books: books.filter((book) => [left_id, right_id].includes(book.id)),
          matched_pages: 4,
          coverage: 1,
        })),
        comparing: fixture.duplicateComparing,
        indexed_pages: 20,
        total_pages: 20,
      });
    if (url.pathname === "/api/duplicates/ignore") {
      fixture.ignoredPairs.push([body.left_id, body.right_id]);
      fixture.duplicatePairs = fixture.duplicatePairs.filter(
        ([left]) => left !== body.left_id,
      );
      return noContent();
    }
    if (url.pathname === "/api/translations")
      return json({ updated_at: null, count: 0 });
    if (url.pathname === "/api/tag-aliases") {
      if (body?.translation !== undefined)
        body.translation = body.translation.trim();
      if (method === "PUT") {
        fixture.aliasSaves.push(body);
        const index = fixture.aliases.findIndex(
          (item) =>
            item.namespace === body.tag.namespace &&
            item.value === body.tag.value,
        );
        if (index >= 0 && fixture.aliases[index].translation !== body.expected)
          return json({ error: "revision_conflict" }, 409);
        if (!body.translation) {
          fixture.aliases.splice(index, 1);
          return noContent();
        }
        if (index >= 0) fixture.aliases[index].translation = body.translation;
        else
          fixture.aliases.push({ ...body.tag, translation: body.translation });
        return json({ ...body.tag, translation: body.translation });
      }
      return json({ items: structuredClone(fixture.aliases), after: null });
    }
    if (url.pathname === "/api/sources/settings") {
      if (method === "PUT") {
        if (expectedRevision !== fixture.sourceConfig.revision)
          return json(
            { error: "revision_conflict", message: "Settings changed" },
            409,
          );
        fixture.sourceSaves.push({
          ...body,
          expected_revision: expectedRevision,
        });
        const configured = { ...fixture.sourceConfig.configured };
        for (const [key, value] of Object.entries(body.secrets || {}))
          configured[key] = !!value;
        fixture.sourceConfig = {
          settings: structuredClone(body.settings),
          configured,
          revision: fixture.sourceConfig.revision + 1,
          key_lost: fixture.sourceConfig.key_lost,
          rate_events: fixture.sourceConfig.rate_events,
        };
      }
      return json({
        ...fixture.sourceConfig,
        budget: fixture.sourceBudget,
        cooldowns: fixture.sourceCooldowns,
      });
    }
    if (url.pathname === "/api/sources/reset-credentials") {
      fixture.sourceResets++;
      fixture.sourceConfig = {
        ...fixture.sourceConfig,
        settings: { ...fixture.sourceConfig.settings, online_paused: true },
        configured: {},
        revision: fixture.sourceConfig.revision + 1,
        key_lost: false,
      };
      return json({
        ...fixture.sourceConfig,
        budget: fixture.sourceBudget,
        cooldowns: fixture.sourceCooldowns,
      });
    }
    if (
      url.pathname === "/api/source-checks/retry-failed" &&
      method === "POST"
    ) {
      const failed = books.filter(
        (book) =>
          book.source_check?.state === "needs_review" &&
          book.source_check.error_code &&
          book.metadata.source,
      );
      for (const book of failed)
        Object.assign(book.source_check, {
          state: "queued",
          error_code: null,
          generation: book.source_check.generation + 1,
        });
      return json({ queued: failed.length });
    }
    if (
      /^\/api\/sources\/verify\/(ehentai|exhentai)$/.test(url.pathname) &&
      method === "POST"
    )
      return json({
        reachable: true,
        authenticated: !fixture.verifyFailure,
        detail: fixture.verifyFailure ? "source_login_required" : null,
      });
    if (url.pathname === "/api/translations/update" && method === "POST")
      return json({ updated: 0 });
    if (url.pathname === "/api/client-cache/clear" && method === "POST")
      return noContent();
    if (url.pathname === "/api/tag-suggestions") {
      const query = (url.searchParams.get("query") ?? "").toLowerCase();
      const tags = [
        ...new Map(
          books
            .flatMap((b) => b.metadata.tags)
            .map((tag) => [`${tag.namespace}:${tag.value}`, tag]),
        ).values(),
      ];
      return json({
        items: tags
          .filter((tag) =>
            `${tag.namespace}:${tag.value}`.toLowerCase().includes(query),
          )
          .slice(0, Number(url.searchParams.get("limit") ?? 20))
          .map((tag) => ({
            ...tag,
            translation:
              fixture.aliases.find(
                (item) =>
                  item.namespace === tag.namespace && item.value === tag.value,
              )?.translation ?? null,
            count: books.filter((b) =>
              b.metadata.tags.some(
                (x) => x.namespace === tag.namespace && x.value === tag.value,
              ),
            ).length,
          })),
      });
    }
    if (
      ["/api/bulk-selection", "/api/bulk-search"].includes(url.pathname) &&
      method === "POST"
    ) {
      let selected;
      if (body.book_ids)
        selected = books.filter((book) => body.book_ids.includes(book.id));
      else {
        const params = new URLSearchParams({
          ...body.options,
          query: body.query ?? "",
          limit: 100000,
        });
        const result = await (
          await window.fetch(`/api/books?${params}`)
        ).json();
        if (result.total !== body.expected_count) return json({}, 409);
        selected = books.filter(
          (book) =>
            result.items.some((item) => item.id === book.id) &&
            !(body.exclude ?? []).includes(book.id),
        );
      }
      const existing = fixture.bulkJobs[body.id];
      if (existing) return json(existing);
      if (body.action.kind === "add_collection") {
        const collection = fixture.collections.find(
          (item) => item.id === body.action.target_id,
        );
        if (!collection) return json({ error: "collection_not_found" }, 404);
        if (collection.rule) return json({ error: "dynamic_collection" }, 422);
        if (collection.revision !== body.action.target_revision)
          return json({}, 409);
        collection.book_ids = [
          ...new Set([
            ...collection.book_ids,
            ...selected.map((book) => book.id),
          ]),
        ];
        collection.revision++;
      } else
        for (const book of selected) {
          if (body.action.kind === "rating")
            book.personal.rating = body.action.rating;
          if (body.action.kind === "completed") {
            book.personal.completed = body.action.completed;
            if (!body.action.completed) book.personal.resume_page = null;
          }
          if (body.action.kind === "source" && book.metadata.source)
            book.source_check = {
              state:
                book.metadata.source?.site === "nhentai" ? "ignored" : "queued",
              generation: 1,
            };
          if (["rating", "completed"].includes(body.action.kind))
            book.personal_revision++;
        }
      const job = {
        id: body.id,
        state: "done",
        counts: {
          done: selected.length,
          pending: 0,
          cancelled: 0,
          error: 0,
          missing: 0,
          conflict: 0,
        },
        items: [],
        after: null,
      };
      if (body.action.kind === "source") {
        const errors = selected.filter((book) => !book.metadata.source).length;
        job.counts.error = errors;
        job.counts.done -= errors;
        const ignored = selected.filter(
          (book) => book.metadata.source?.site === "nhentai",
        ).length;
        if (ignored) {
          job.counts.ignored = ignored;
          job.counts.done -= ignored;
        }
      }
      fixture.bulkJobs[body.id] = job;
      return json(job);
    }
    if (
      /^\/api\/bulk\/[^/]+\/cancel$/.test(url.pathname) &&
      method === "POST"
    ) {
      const job = fixture.bulkJobs[url.pathname.split("/").at(-2)];
      if (!job) return json({}, 404);
      if (job.counts.pending) {
        job.counts.cancelled = job.counts.pending;
        job.counts.pending = 0;
        job.state = "cancelled";
      }
      return json(job);
    }
    if (/^\/api\/bulk\/[^/]+$/.test(url.pathname)) {
      const job = fixture.bulkJobs[url.pathname.split("/").at(-1)];
      return job ? json(job) : json({}, 404);
    }
    if (url.pathname === "/api/source-checks") {
      const failed = books.filter(
        (book) => book.source_check?.state === "needs_review",
      );
      return json({ items: structuredClone(failed), total: failed.length });
    }
    if (url.pathname === "/api/sources/budget")
      return json({
        budget: fixture.sourceBudget,
        cooldowns: fixture.sourceCooldowns,
      });
    const transition = (job, action, mode = "org", target = null) => {
      if (
        action !== "cancel" &&
        !["ehentai", "exhentai"].includes(job.source?.site)
      )
        return "source_retired";
      const active = ["inspecting", "submitting", "downloading", "importing"],
        state = job.state;
      let next;
      if (action === "cancel" && state !== "done")
        next = [...active, "cancelling", "pausing"].includes(state)
          ? "cancelling"
          : "cancelled";
      else if (
        action === "pause" &&
        [...active, "queued", "pausing"].includes(state)
      )
        next = state === "queued" ? "paused" : "pausing";
      else if (action === "redownload" && ["done", "cancelled"].includes(state))
        next = "queued";
      else if (
        ["resume", "recover"].includes(action) &&
        ["paused", "error", "verification_required"].includes(state)
      )
        next = "queued";
      else if (action === "approve" && state === "needs_approval") {
        if (
          !["org", "res", "reuse"].includes(mode) ||
          (mode === "reuse" && !job.details.reusable)
        )
          return "invalid_mode";
        const price =
          mode === "reuse"
            ? 0
            : job.details.quote?.[
                mode === "org" ? "original_gp" : "resample_gp"
              ];
        if (price == null) return "price_unavailable";
        job.details.approved_gp = price;
        job.details.mode = mode;
        next = "queued";
      }
      if (!next) return "action_unavailable";
      job.state = next;
      job.revision++;
      return null;
    };
    const terminalDownload = ["done", "cancelled"];
    if (url.pathname === "/api/downloads/batch") {
      const items = body.urls.map((input) => {
        const match = input.match(
          /^https:\/\/(e-hentai\.org|exhentai\.org)\/g\/(\d+)\/([a-f0-9]{1,64})\/?/,
        );
        if (!match)
          return {
            url: input,
            job: null,
            error: /^https?:\/\//.test(input)
              ? "unsupported_source"
              : "invalid_url",
          };
        const canonical = match[0].replace(/\/?$/, "/");
        // Like the server: one job per gallery, whichever E(x)Hentai host the link names.
        const gallery = (link) =>
          link.replace(/^https:\/\/exhentai\.org\//, "https://e-hentai.org/");
        let job = fixture.downloads.find(
          (item) => gallery(item.input_url) === gallery(canonical),
        );
        let outcome = "existing";
        if (!job) {
          outcome = "created";
          const now = Math.floor(Date.now() / 1000);
          job = {
            id: `fixture-job-${fixture.downloads.length + 1}`,
            input_url: canonical,
            source: {
              site: { "e-hentai.org": "ehentai", "exhentai.org": "exhentai" }[
                match[1]
              ],
              gallery_id: Number(match[2]),
              gallery_token: match[3] ?? null,
            },
            state: "queued",
            revision: 1,
            received: 0,
            total: null,
            book_id: null,
            error_code: null,
            retry_at: 0,
            attempts: 0,
            created_at: now,
            updated_at: now,
            details: {},
          };
          fixture.downloads.push(job);
        }
        return {
          url: input,
          job: { ...structuredClone(job), outcome },
          error: null,
        };
      });
      return json({ items });
    }
    if (url.pathname === "/api/downloads" && method === "POST") {
      if (Object.keys(body).some((key) => key !== "url"))
        return json({ error: "invalid_input" }, 422);
      const result = await (
        await window.fetch("/api/downloads/batch", {
          method: "POST",
          body: JSON.stringify({ urls: [body.url] }),
        })
      ).json();
      const item = result.items[0];
      return item.job ? json(item.job) : json({ error: item.error }, 422);
    }
    if (url.pathname === "/api/downloads") {
      if (fixture.downloadListFailure) return json({}, 503);
      let scope = url.searchParams.get("scope") || "all";
      const focus = fixture.downloads.find(
        (job) => job.id === url.searchParams.get("focus"),
      );
      if (focus)
        scope = terminalDownload.includes(focus.state) ? "history" : "active";
      const through = focus
        ? fixture.downloads.length
        : Number(url.searchParams.get("through") ?? fixture.downloads.length);
      const items = [...fixture.downloads]
        .slice(0, through)
        .reverse()
        .filter(
          (job) =>
            scope === "all" ||
            (scope === "history") === terminalDownload.includes(job.state),
        );
      const limit = Number(url.searchParams.get("limit") || 20);
      const page = Math.max(
        1,
        Math.min(
          focus
            ? Math.floor(
                items.findIndex((job) => job.id === focus.id) / limit,
              ) + 1
            : Number(url.searchParams.get("page") || 1),
          Math.ceil(items.length / limit),
        ),
      );
      const offset = url.searchParams.has("page")
        ? (page - 1) * limit
        : Number(url.searchParams.get("after") || 0);
      return json({
        items: structuredClone(items.slice(offset, offset + limit)),
        after: offset + limit < items.length ? String(offset + limit) : null,
        total: items.length,
        page,
        scope,
        through,
      });
    }
    if (url.pathname === "/api/downloads/actions") {
      const results = body.items.map((item) => {
        const job = fixture.downloads.find((job) => job.id === item.id);
        if (!job)
          return { id: item.id, ok: false, error: "download_not_found" };
        fixture.downloadActions.push({ ...item, action: body.action });
        if (item.expected_revision !== job.revision)
          return {
            id: item.id,
            ok: false,
            error: "revision_conflict",
            job: structuredClone(job),
          };
        if (body.action === "delete") {
          if (![...terminalDownload, "error"].includes(job.state))
            return { id: item.id, ok: false, error: "download_active" };
          fixture.downloads.splice(fixture.downloads.indexOf(job), 1);
          return { id: item.id, ok: true };
        }
        const failure = transition(job, body.action, item.mode ?? "org");
        if (failure)
          return {
            id: item.id,
            ok: false,
            error: failure,
            job: structuredClone(job),
          };
        return { id: item.id, ok: true, job: structuredClone(job) };
      });
      return json({ results });
    }
    if (url.pathname === "/api/sources/balance") {
      return json({
        balance_gp: fixture.sourceBalance ?? 5000,
        checked_at: Math.floor(Date.now() / 1000),
        released: 0,
      });
    }
    const downloadMatch = url.pathname.match(
      /^\/api\/downloads\/([^/]+)(\/action)?$/,
    );
    if (downloadMatch) {
      const job = fixture.downloads.find(
        (item) => item.id === downloadMatch[1],
      );
      if (!job) return json({ error: "download_not_found" }, 404);
      if (!downloadMatch[2] && method === "DELETE") {
        fixture.downloadActions.push({ id: job.id, action: "delete" });
        if (![...terminalDownload, "error"].includes(job.state))
          return json({ error: "download_active" }, 422);
        fixture.downloads.splice(fixture.downloads.indexOf(job), 1);
        return noContent();
      }
      if (downloadMatch[2]) {
        fixture.downloadActions.push({
          id: job.id,
          ...body,
          expected_revision: expectedRevision,
        });
        if (expectedRevision !== job.revision && body.action !== "cancel")
          return json({ error: "revision_conflict" }, 409);
        const failure = transition(
          job,
          body.action,
          body.mode ?? "org",
          body.url,
        );
        if (failure) return json({ error: failure }, 422);
      }
      return json(structuredClone(job));
    }
    if (url.pathname === "/api/auth/credentials")
      return fixture.credentialListFailure
        ? json({}, 503)
        : json({ items: fixture.credentials });
    if (url.pathname === "/api/auth/keys") {
      if (fixture.keyCreateFailure) return json({}, fixture.keyCreateFailure);
      const sequence = ++fixture.keySequence;
      const id = `fixture-key-${sequence}`;
      fixture.credentials.push({
        id,
        kind: body.kind,
        label: body.label,
        created_at: Math.floor(Date.now() / 1000),
        expires_at: null,
        revoked_at: null,
      });
      if (fixture.keyCreateDelay)
        await new Promise((resolve) =>
          setTimeout(resolve, fixture.keyCreateDelay),
        );
      if (fixture.keyCreateLoseResponse) {
        fixture.keyCreateLoseResponse = false;
        throw new TypeError("fixture key response lost");
      }
      return json({
        id,
        kind: body.kind,
        key: `mc${{ management: "m", downloads: "d" }[body.kind] ?? "r"}_fixture-only-not-a-real-key-${sequence}`,
      });
    }
    if (url.pathname === "/api/auth/passcode") {
      if (fixture.passcodeFailure) return json({}, fixture.passcodeFailure);
      fixture.passcodeChanges++;
      fixture.credentials
        .filter((item) => body.revoke_keys || item.kind === "session")
        .forEach((item) => (item.revoked_at = Math.floor(Date.now() / 1000)));
      fixture.authenticated = false;
      if (fixture.passcodeLoseResponse)
        throw new TypeError("fixture passcode response lost");
      return noContent();
    }
    if (
      url.pathname.startsWith("/api/auth/credentials/") &&
      method === "DELETE"
    ) {
      const item = fixture.credentials.find(
        (item) => item.id === url.pathname.split("/").at(-1),
      );
      if (!item) return json({}, 404);
      item.revoked_at = Math.floor(Date.now() / 1000);
      if (fixture.keyRevokeLoseResponse) {
        fixture.keyRevokeLoseResponse = false;
        throw new TypeError("fixture revocation response lost");
      }
      return noContent();
    }
    const advanceScan = () => {
      const run = fixture.scan;
      if (!run || run.state !== "active") return;
      if (fixture.scanOffline) {
        run.state = "paused";
        run.error_code = "root_unavailable";
        return;
      }
      const item = run.entries.find((item) => item.state === "pending");
      if (!item) {
        run.state = "done";
        return;
      }
      if (item.kind === "directory") {
        item.state = "expanded";
        run.entries.push(
          ...["新书.cbz", "已有.cbz", "重复.cbz", "损坏.pdf"].map(
            (path, index) => ({
              id: index + 2,
              path,
              kind: "archive",
              state: "pending",
              book_id: null,
              error_code: null,
              warnings: [],
            }),
          ),
        );
      } else {
        item.state = ["indexed", "unchanged", "duplicate", "error"][
          item.id - 2
        ];
        if (item.state === "error") {
          item.error_code = "invalid_archive";
          item.error_message = "PDF has no readable pages";
        } else item.book_id = books[[3, 1, 2][item.id - 2]].id;
      }
    };
    const scanResult = () => {
      const run = fixture.scan;
      if (!run) return null;
      const counts = Object.fromEntries(
        [
          "pending",
          "expanded",
          "indexed",
          "unchanged",
          "duplicate",
          "error",
        ].map((state) => [
          state,
          run.entries.filter((item) => item.state === state).length,
        ]),
      );
      const rows = run.entries.filter(
        (item) =>
          (item.kind === "archive" || item.state === "error") &&
          (!url.searchParams.get("state") ||
            item.state === url.searchParams.get("state")) &&
          item.id > Number(url.searchParams.get("after") || 0),
      );
      const limit = Number(url.searchParams.get("limit") || 50),
        items = rows.slice(0, limit);
      return {
        id: run.id,
        state: run.state,
        keep_source: run.keep_source,
        created_at: run.created_at,
        ignored: run.ignored,
        error_code: run.error_code,
        counts,
        with_metadata: counts.indexed,
        without_metadata: 0,
        archives: run.entries.filter((item) => item.kind === "archive").length,
        processed: run.entries.filter(
          (item) => item.kind === "archive" && item.state !== "pending",
        ).length,
        items: items.map(({ previous_books, ...item }) => item),
        after: items.length === limit ? items.at(-1).id : null,
      };
    };
    if (url.pathname === "/api/scans") {
      if (method === "GET") {
        advanceScan();
        return json({ scan: scanResult(), root: "/library/import" });
      }
      if (fixture.scanOffline) return json({}, 503);
      if (!fixture.scan || fixture.scan.state === "done")
        fixture.scan = {
          id: crypto.randomUUID(),
          state: "active",
          keep_source: url.searchParams.get("keep_source") === "true",
          created_at: Math.floor(Date.now() / 1000),
          ignored: 0,
          error_code: null,
          entries: [
            {
              id: 1,
              path: "",
              kind: "directory",
              state: "pending",
              book_id: null,
              error_code: null,
              warnings: [],
            },
          ],
        };
      return json(scanResult());
    }
    const scanMatch = url.pathname.match(/^\/api\/scans\/([^/]+)(.*)$/);
    if (scanMatch) {
      if (!fixture.scan || fixture.scan.id !== scanMatch[1])
        return json({}, 404);
      const run = fixture.scan,
        action = scanMatch[2];
      const itemMatch = action.match(/^\/items\/(\d+)$/);
      if (itemMatch && method === "GET") {
        const item = run.entries.find(
          (item) => item.id === Number(itemMatch[1]),
        );
        return item ? json(item) : json({}, 404);
      }
      if (!["", "/pause", "/resume"].includes(action)) return json({}, 404);
      if (action === "/pause" && run.state === "active") run.state = "paused";
      if (action === "/resume") {
        if (fixture.scanOffline) return json({}, 503);
        if (run.state === "paused") {
          run.state = "active";
          run.error_code = null;
        }
      }
      return json(scanResult());
    }
    const refreshDynamic = async (collection) => {
      if (!collection.rule) return;
      const params = new URLSearchParams({
        query: collection.rule.query ?? "",
        ...collection.rule.options,
        limit: "500",
      });
      const found = [];
      let after;
      do {
        if (after) params.set("after", after);
        const result = await (
          await window.fetch(`/api/books?${params}`)
        ).json();
        found.push(...result.items.map((book) => book.id));
        after = result.after;
      } while (after);
      collection.book_ids = found;
    };
    if (url.pathname.startsWith("/api/collections"))
      for (const collection of fixture.collections)
        await refreshDynamic(collection);
    const summary = (collection) => ({
      id: collection.id,
      kind: collection.rule ? "dynamic" : "static",
      rule: collection.rule ?? null,
      name: collection.name,
      revision: collection.revision,
      count: collection.book_ids.length,
    });
    const detail = (collection) => {
      const offset = uncursor(url.searchParams.get("after"), -1) + 1,
        limit = Number(url.searchParams.get("limit") || 50);
      const items = collection.book_ids
        .slice(offset, offset + limit)
        .map((book_id, index) => ({
          book_id,
          position: offset + index,
          book: books.find((book) => book.id === book_id),
        }));
      return {
        ...summary(collection),
        items,
        after: items.length === limit ? cursor(items.at(-1).position) : null,
      };
    };
    if (url.pathname === "/api/collections") {
      if (method === "POST") {
        const collection = {
          id: crypto.randomUUID(),
          name: body.name.trim(),
          revision: 1,
          book_ids: [],
          rule: body.rule ?? null,
        };
        fixture.collections.push(collection);
        await refreshDynamic(collection);
        return json(detail(collection));
      }
      const all = fixture.collections
        .filter(
          (item) =>
            !url.searchParams.get("kind") ||
            (item.rule ? "dynamic" : "static") === url.searchParams.get("kind"),
        )
        .sort((a, b) => a.id.localeCompare(b.id));
      const after = uncursor(url.searchParams.get("after"), "");
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const items = all
        .filter((item) => !after || item.id > after)
        .slice(0, limit);
      return json({
        items: items.map(summary),
        after: items.length === limit ? cursor(items.at(-1).id) : null,
      });
    }
    const collectionMatch = url.pathname.match(
      /^\/api\/collections\/([^/]+)(.*)$/,
    );
    if (collectionMatch) {
      const collection = fixture.collections.find(
        (item) => item.id === collectionMatch[1],
      );
      if (!collection) return json({}, 404);
      const action = collectionMatch[2];
      if (expectedRevision !== null && expectedRevision !== collection.revision)
        return json({}, 409);
      if (action === "/freeze" && method === "POST") {
        if (collection.rule) {
          await refreshDynamic(collection);
          collection.rule = null;
          collection.revision++;
        }
        return json(detail(collection));
      }
      if (action === "/rule" && method === "PUT") {
        if (!collection.rule) return json({ error: "dynamic_collection" }, 422);
        collection.rule = structuredClone(body.rule);
        await refreshDynamic(collection);
        collection.revision++;
        return json(detail(collection));
      }
      if (collection.rule && method !== "GET" && action.startsWith("/items"))
        return json({ error: "dynamic_collection" }, 422);
      if (action === "/context") {
        const bookId = url.searchParams.get("book_id"),
          index = collection.book_ids.indexOf(bookId);
        if (bookId && index < 0) return json({}, 404);
        const unfinished = collection.book_ids.find(
          (id) => !books.find((book) => book.id === id).personal.completed,
        );
        return json({
          ...summary(collection),
          continue_id: unfinished || collection.book_ids[0] || null,
          continue_state: unfinished
            ? books.find((book) => book.id === unfinished).personal
                .resume_page != null
              ? "reading"
              : "unread"
            : "completed",
          all_completed: collection.book_ids.length > 0 && !unfinished,
          ...(bookId
            ? {
                index,
                previous_id: collection.book_ids[index - 1] || null,
                next_id: collection.book_ids[index + 1] || null,
              }
            : {}),
        });
      }
      if (!action && method === "DELETE") {
        fixture.collections = fixture.collections.filter(
          (item) => item !== collection,
        );
        return noContent();
      }
      if (!action && method === "PUT") {
        if (collection.name !== body.name.trim()) {
          collection.name = body.name.trim();
          collection.revision++;
        }
      }
      if (
        action === "/items" &&
        method === "POST" &&
        !collection.book_ids.includes(body.book_id)
      ) {
        collection.book_ids.push(body.book_id);
        collection.revision++;
      }
      if (action.startsWith("/items/")) {
        const bookId = action.split("/").at(-1);
        if (!collection.book_ids.includes(bookId)) return json({}, 404);
        if (method === "DELETE") {
          collection.book_ids = collection.book_ids.filter(
            (id) => id !== bookId,
          );
          collection.revision++;
        }
        if (method === "PUT" && body.before_id !== bookId) {
          const next = collection.book_ids.filter((id) => id !== bookId);
          next.splice(
            body.before_id ? next.indexOf(body.before_id) : next.length,
            0,
            bookId,
          );
          if (next.join() !== collection.book_ids.join()) {
            collection.book_ids = next;
            collection.revision++;
          }
        }
      }
      return !action || action === "/items" || action.startsWith("/items/")
        ? json(detail(collection))
        : json({}, 404);
    }
    if (url.pathname === "/api/statistics") {
      if (fixture.activityFailure) return json({}, 503);
      const zone = requestZone(url);
      if (zone === null) return json({ error: "invalid_input" }, 422);
      return json(statistics(Number(url.searchParams.get("days") ?? 30), zone));
    }
    if (url.pathname === "/api/reading-history") {
      if (fixture.activityFailure) return json({}, 503);
      if (requestZone(url) === null)
        return json({ error: "invalid_input" }, 422);
      return json(
        readingHistory(
          url.searchParams.get("after"),
          Number(url.searchParams.get("limit") ?? 50),
        ),
      );
    }
    if (url.pathname === "/api/snapshots" && method === "POST") {
      await fixture.snapshotBuild;
      if (fixture.snapshotFailure)
        return json({ error: fixture.snapshotFailure }, 507);
      return json({ id: "prepared-1", size: 22 });
    }
    if (url.pathname === "/api/settings")
      return json({ max_upload_bytes: 2 * 1024 ** 3 });
    if (url.pathname === "/api/cache") return json(fixture.cache);
    const warm = url.pathname.match(
      /^\/api\/cache\/(covers|thumbnails)\/warm$/,
    );
    if (warm && method === "POST") {
      fixture.cache[warm[1]] = {
        ...fixture.cache[warm[1]],
        state: "active",
        done: 0,
        total: books.length,
      };
      fixture.warmRequests[warm[1]]++;
      return json(fixture.cache);
    }
    if (url.pathname === "/api/books") {
      const p = url.searchParams;
      const terms =
        (p.get("query") || "")
          .normalize("NFKC")
          .toLowerCase()
          .match(/(?:[^\s"]|"[^"]*")+/g) ?? [];
      let matches = books.filter((book) => {
        const text = [
          book.metadata.title,
          book.metadata.original_title,
          book.content.original_filename,
          book.metadata.language,
          ...book.metadata.tags.map((tag) => `${tag.namespace}:${tag.value}`),
        ]
          .join("\n")
          .normalize("NFKC")
          .toLowerCase();
        if (
          !terms.every((term) => {
            const exact = term.match(/^([^:]+):"(.*)"$/);
            return exact
              ? book.metadata.tags.some(
                  (tag) =>
                    tag.namespace.normalize("NFKC").toLowerCase() ===
                      exact[1] &&
                    tag.value.normalize("NFKC").toLowerCase() === exact[2],
                )
              : text.includes(term.replaceAll('"', ""));
          })
        )
          return false;
        if (
          p.get("reading") === "unread" &&
          (book.personal.completed || book.personal.resume_page !== null)
        )
          return false;
        if (
          p.get("work_type") &&
          (book.metadata.work_type ?? "").toLowerCase() !== p.get("work_type")
        )
          return false;
        if (
          p.get("language") &&
          (book.metadata.language ?? "").toLowerCase() !== p.get("language")
        )
          return false;
        const source = book.metadata.source?.site ?? "none",
          wanted = p.get("source");
        if (
          wanted &&
          (wanted === "eh"
            ? !["ehentai", "exhentai"].includes(source)
            : source !== wanted)
        )
          return false;
        if (p.get("reading") === "completed" && !book.personal.completed)
          return false;
        if (
          p.get("reading") === "in_progress" &&
          (book.personal.completed || book.personal.resume_page === null)
        )
          return false;
        if (
          p.get("review") &&
          p.get("review") !== "all" &&
          (book.review_state === "confirmed" ? "confirmed" : "needs_review") !==
            p.get("review")
        )
          return false;
        if (p.get("rating") === "unrated" && book.personal.rating !== null)
          return false;
        if (p.get("rating") === "rated" && book.personal.rating === null)
          return false;
        if (
          p.has("min_rating") &&
          (book.personal.rating === null ||
            book.personal.rating < Number(p.get("min_rating")))
        )
          return false;
        if (
          p.has("source_rating_min") &&
          (book.metadata.source_rating == null ||
            book.metadata.source_rating.average <
              Number(p.get("source_rating_min")))
        )
          return false;
        return true;
      });
      const key = async (id) => {
        if (p.get("order") === "recent")
          return (
            String(
              999999999999999 -
                (books.find((book) => book.id === id).recent_order ?? 0),
            ).padStart(15, "0") + id
          );
        if (p.get("order") !== "random") return id;
        const bytes = Uint8Array.from(`${p.get("seed")}:${id}`, (value) =>
          value.charCodeAt(0),
        );
        const digest = await crypto.subtle.digest("SHA-256", bytes);
        return (
          Array.from(new Uint8Array(digest), (x) =>
            x.toString(16).padStart(2, "0"),
          ).join("") + id
        );
      };
      const ranked = await Promise.all(
        matches.map(async (book) => ({ book, key: await key(book.id) })),
      );
      ranked.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
      const after = uncursor(p.get("after"), "");
      const boundary = after ? await key(after) : "";
      const offset = p.has("page")
        ? (Number(p.get("page")) - 1) * Number(p.get("limit") || 50)
        : 0;
      const limit = Number(p.get("limit") || 50);
      matches = ranked
        .filter((item) => item.key > boundary)
        .slice(offset, offset + limit)
        .map((item) => item.book);
      return json({
        items: matches.map((book) => ({
          ...book,
          ...(p.has("collection_id")
            ? {
                in_collection:
                  fixture.collections
                    .find((item) => item.id === p.get("collection_id"))
                    ?.book_ids.includes(book.id) ?? false,
              }
            : {}),
        })),
        ...(p.get("include_total") !== "false" && !p.get("after")
          ? { total: ranked.length }
          : { total: null }),
        after: matches.length === limit ? cursor(matches.at(-1).id) : null,
      });
    }
    if (url.pathname === "/api/sidecar/conflicts")
      return json({
        items: fixture.sidecarConflicts.filter(
          (item) => item.state !== "resolved",
        ),
        after: null,
      });
    const conflictMatch = url.pathname.match(
      /^\/api\/sidecar\/conflicts\/([^/]+)\/resolve$/,
    );
    if (conflictMatch && method === "POST") {
      const conflict = fixture.sidecarConflicts.find(
        (item) => item.id === conflictMatch[1],
      );
      if (!conflict) return json({}, 404);
      const book = books.find((book) => book.id === conflict.book_id);
      if (
        !book ||
        (conflict.metadata_revision !== undefined &&
          conflict.metadata_revision !== book.metadata_revision)
      )
        return json({}, 409);
      const file =
        conflict.file_metadata ?? conflict.file?.metadata ?? conflict.file;
      if (body.choice === "file") {
        if (!file) return json({ error: "invalid_input" }, 422);
        book.metadata = structuredClone(file);
        book.metadata_revision++;
      } else if (body.fields) {
        for (const [field, choice] of Object.entries(body.fields)) {
          if (
            !["library", "file"].includes(choice) ||
            !(field in book.metadata)
          )
            return json({ error: "invalid_input" }, 422);
          if (choice === "file")
            book.metadata[field] = structuredClone(file[field]);
        }
        book.metadata_revision++;
      } else if (body.choice && body.choice !== "library")
        return json({ error: "invalid_input" }, 422);
      conflict.state = "resolved";
      sidecarOf(book).last_error = null;
      return json({ book, sidecar_sync: { state: "synced" } });
    }
    if (url.pathname === "/api/sidecar/status") {
      if (fixture.syncStatusFailure) return json({}, 503);
      const state = (book) =>
        sidecarOf(book).last_error === "external_conflict"
          ? "conflict"
          : sidecarOf(book).last_error
            ? "error"
            : sidecarOf(book).pending_revision > sidecarOf(book).synced_revision
              ? "pending"
              : "synced";
      const counts = {
        pending: 0,
        error: 0,
        conflict: 0,
        synced: 0,
      };
      books.forEach((book) => counts[state(book)]++);
      const after = uncursor(url.searchParams.get("after"), "");
      const pending = [...books]
        .sort((a, b) => a.id.localeCompare(b.id))
        .filter((book) => state(book) !== "synced" && book.id > after);
      const limit = Math.min(
        fixture.syncPageSize,
        Number(url.searchParams.get("limit") || 50),
      );
      return json({
        counts,
        items: pending.slice(0, limit).map((book) => ({
          id: book.id,
          metadata: book.metadata,
          content: book.content,
          state: state(book),
          pending_revision: sidecarOf(book).pending_revision,
          synced_revision: sidecarOf(book).synced_revision,
          error: sidecarOf(book).last_error,
        })),
        after: pending.length > limit ? cursor(pending[limit - 1].id) : null,
      });
    }
    if (url.pathname === "/api/sidecar/sync") {
      const body = JSON.parse(options.body);
      fixture.syncCalls.push(body);
      if (fixture.syncFailure) return json({}, fixture.syncFailure);
      const sync = (book) => {
        // A damaged file is rewritten only when that is asked for explicitly.
        const state =
          sidecarOf(book).last_error === "sidecar_invalid" &&
          !body.replace_invalid
            ? "error"
            : body.replace_invalid
              ? "synced"
              : fixture.syncOutcomes[book.id] || "synced";
        if (
          state === "error" &&
          sidecarOf(book).last_error === "sidecar_invalid"
        )
          return { state, error: "sidecar_invalid" };
        sidecarOf(book).last_error =
          state === "conflict"
            ? "external_conflict"
            : state === "error"
              ? "storage_error"
              : null;
        if (state === "synced" || state === "unchanged")
          sidecarOf(book).synced_revision = book.metadata_revision;
        return { state };
      };
      let result;
      if (body.book_id)
        result = sync(books.find((book) => book.id === body.book_id));
      else {
        const through =
          body.through_id ??
          [...books]
            .map((b) => b.id)
            .sort()
            .at(-1) ??
          "";
        const pending = [...books]
          .sort((a, b) => a.id.localeCompare(b.id))
          .filter(
            (book) =>
              book.id > uncursor(body.after, "") &&
              book.id <= through &&
              (body.mode !== "pending" ||
                sidecarOf(book).pending_revision >
                  sidecarOf(book).synced_revision ||
                sidecarOf(book).last_error),
          );
        // syncBatchCap lets tests force several batches out of the six fixture books.
        const limit = Math.min(body.limit || 100, fixture.syncBatchCap);
        result = {
          items: pending
            .slice(0, limit)
            .map((book) => ({ book_id: book.id, ...sync(book) })),
          after: pending.length > limit ? cursor(pending[limit - 1].id) : null,
          through_id: through,
        };
      }
      if (fixture.syncDelay)
        await new Promise((resolve) => setTimeout(resolve, fixture.syncDelay));
      if (fixture.syncLoseResponse) {
        fixture.syncLoseResponse = false;
        throw new TypeError("fixture lost sync response");
      }
      return json(result);
    }
    const match = url.pathname.match(/^\/api\/books\/([^/]+)(.*)$/);
    if (!match) return json({}, 404);
    const book = books.find((book) => book.id === match[1]);
    if (!book) return json({}, 404);
    const action = match[2];
    if (action === "/source-check" && method === "POST") {
      const check = book.source_check;
      if (!check) return json({ error: "not_found" }, 404);
      if (
        Number(url.searchParams.get("expected_generation")) !== check.generation
      )
        return json({ error: "revision_conflict" }, 409);
      const { action: command } = JSON.parse(options.body);
      if (command === "retry" && !book.metadata.source)
        return json({ error: "source_absent" }, 422);
      if (
        command === "retry" &&
        !["ehentai", "exhentai"].includes(book.metadata.source?.site)
      )
        return json({ error: "source_retired" }, 422);
      book.source_check = {
        ...check,
        state: command === "skip" ? "ignored" : "queued",
        generation: check.generation + 1,
        error_code: null,
        reasons: [],
        retry_at: 0,
      };
      return json(book.source_check);
    }
    if (action === "/source-comments")
      return json({
        notes: [],
        comments: [],
        ...(options.method === "POST" ? { comments_error: null } : {}),
      });
    if (action.startsWith("/pages/")) {
      const index = Number(action.split("/")[2]);
      if (fixture.pageDelay)
        await new Promise((resolve) => setTimeout(resolve, fixture.pageDelay));
      options.signal?.throwIfAborted();
      if (fixture.failPage === index) return json({}, 503);
      return new Response(page(book, index), {
        headers: { "Content-Type": "image/png" },
      });
    }
    if (action === "/personal") {
      if (fixture.failSave) return json({}, 409);
      const data = JSON.parse(options.body);
      if (expectedRevision !== book.personal_revision) {
        // Only a snapshot based on a revision the server had rebases (database.save_personal).
        if (!data.base_state || expectedRevision > book.personal_revision)
          return json({ error: "revision_conflict" }, 409);
        const changes = Object.fromEntries(
          Object.entries(data.state).filter(
            ([key, value]) => value !== data.base_state[key],
          ),
        );
        for (const [key, value] of Object.entries(changes))
          if (
            key !== "resume_page" &&
            book.personal[key] !== data.base_state[key] &&
            book.personal[key] !== value
          )
            return json({ error: "revision_conflict" }, 409);
        data.state = { ...book.personal, ...changes };
      }
      // A finished book keeps its page while it stays finished (database.write_personal).
      if (book.personal.completed && data.state.completed)
        data.state = { ...data.state, resume_page: book.personal.resume_page };
      book.personal = data.state;
      book.personal_revision++;
      fixture.saves.push(structuredClone(book.personal));
      return json(book);
    }
    if (action === "/metadata") {
      const data = JSON.parse(options.body);
      if (expectedRevision !== book.metadata_revision) return json({}, 409);
      book.metadata = data.metadata;
      book.metadata_revision++;
      sidecarOf(book).pending_revision = book.metadata_revision;
      book.review_state = "needs_review";
      book.confirmation = null;
      if (fixture.reviewProofs[book.id] === JSON.stringify(book.metadata)) {
        book.review_state = "confirmed";
        book.confirmation = {
          action: "auto_confirm",
          rule: "local-confirmation/1",
          acknowledged_issues: ["source_absent"],
          evidence_event_id: 1,
        };
      }
      // The server writes the sidecar file with the save and reports how that went;
      // a failed write leaves it pending for the background retry.
      if (fixture.syncFailure)
        return json({
          ...book,
          sidecar_sync: { state: "pending", error: "storage_error" },
        });
      const state = fixture.syncOutcomes[book.id] || "synced";
      sidecarOf(book).last_error =
        state === "conflict" ? "external_conflict" : null;
      if (state === "synced")
        sidecarOf(book).synced_revision = book.metadata_revision;
      return json({
        ...book,
        sidecar_sync: { state, revision: book.metadata_revision },
      });
    }
    const reviewToken = () =>
      `${book.metadata_revision}:${book.reviewEventId ?? 0}`;
    if (action === "/review")
      return json({
        token: reviewToken(),
        state: book.review_state,
        metadata: book.metadata,
        candidates: book.reviewCandidates || {},
        issues: [
          "source_absent",
          ...(Object.values(book.reviewCandidates || {}).some(
            (candidate) => candidate.fields.length && !candidate.decision,
          )
            ? ["candidates_unresolved"]
            : []),
        ],
        import_sources: book.importSources || [],
        warnings: book.importWarnings || [],
        ignored_fields: book.ignoredFields || [],
        automatic: {
          rule: "local-confirmation/1",
          eligible:
            fixture.reviewProofs[book.id] === JSON.stringify(book.metadata),
          reasons:
            fixture.reviewProofs[book.id] === JSON.stringify(book.metadata)
              ? []
              : ["local_confirmation_missing"],
          evidence_event_id: fixture.reviewProofs[book.id] ? 1 : null,
        },
        confirmation:
          book.review_state === "confirmed"
            ? book.confirmation || {
                action: "manual_confirm",
                acknowledged_issues: ["source_absent"],
              }
            : null,
      });
    if (action === "/review/candidate") {
      if (body.token !== reviewToken()) return json({}, 409);
      const candidate = book.reviewCandidates?.[body.candidate];
      if (!candidate) return json({ error: "invalid_input" }, 422);
      if (body.choices) {
        if (
          Object.keys(body.choices).length !== candidate.fields.length ||
          candidate.fields.some(
            (field) => !["current", "candidate"].includes(body.choices[field]),
          )
        )
          return json({ error: "invalid_input" }, 422);
        for (const [field, choice] of Object.entries(body.choices))
          if (choice === "candidate")
            book.metadata[field] = structuredClone(candidate.metadata[field]);
        book.metadata_revision++;
        candidate.decision = "selected";
        candidate.fields = [];
      } else candidate.decision = "skipped";
      book.reviewEventId = (book.reviewEventId ?? 0) + 1;
      return json(book);
    }
    if (action === "/review/automatic") {
      fixture.reviewChecks.push(book.id);
      if (fixture.failReview) return json({}, 409);
      if (JSON.parse(options.body).token !== reviewToken())
        return json({}, 409);
      book.reviewEventId = (book.reviewEventId ?? 0) + 1;
      const reasons =
        fixture.reviewProofs[book.id] === JSON.stringify(book.metadata)
          ? []
          : ["local_confirmation_missing"];
      if (!reasons.length) {
        book.review_state = "confirmed";
        book.confirmation = {
          action: "auto_confirm",
          rule: "local-confirmation/1",
          acknowledged_issues: ["source_absent"],
          evidence_event_id: 1,
        };
      }
      return json({
        book,
        outcome: reasons.length ? "needs_review" : "confirmed",
        reasons,
      });
    }
    if (action === "/review/complete") {
      if (body.token !== reviewToken()) return json({}, 409);
      if (!(body.acknowledged_issues ?? []).includes("source_absent"))
        return json({ error: "acknowledgement_required" }, 422);
      if (
        Object.values(book.reviewCandidates ?? {}).some(
          (candidate) => candidate.fields.length && !candidate.decision,
        )
      )
        return json({ error: "invalid_input" }, 422);
      book.review_state = "confirmed";
      book.reviewEventId = (book.reviewEventId ?? 0) + 1;
      fixture.reviewProofs[book.id] = JSON.stringify(book.metadata);
      book.confirmation = {
        action: "manual_confirm",
        acknowledged_issues: ["source_absent"],
      };
      return json(book);
    }
    return !action && method === "GET" ? json(book) : json({}, 404);
  };
})();
