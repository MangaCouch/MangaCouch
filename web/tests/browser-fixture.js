(() => {
  const names = [
    "山间来信",
    "The Quiet Observatory",
    "海边的慢日子",
    "雨过天晴之后",
    "小小宇宙旅行记",
    "A Walk in November",
  ];
  const colors = [
    "#81937a",
    "#526b79",
    "#c18c6e",
    "#a5ae94",
    "#8b7d99",
    "#b79f70",
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
      bookmarks: [],
    },
    review_state: "needs_review",
    availability: "available",
    sidecar: { pending_revision: 1, synced_revision: 0, last_error: null },
  }));
  const fixture = (window.fixture = {
    books,
    preferences: {
      revision: 0,
      settings: {
        title_preference: "title",
        device_defaults: {
          reading_mode: "single",
          fit: "height",
          direction: "ltr",
          cover_solo: true,
          autoplay_seconds: 8,
        },
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
    removalPresence: "present",
    removalBlocked: [],
    removalConflict: false,
    removalDelay: 0,
    removalLoseResponse: false,
    removalBackupFailure: false,
    // Online sources and downloads (contracts 6-8).
    sourceConfig: {
      settings: {
        download_limit_bytes: 34359738368,
        comments: true,
        user_agent: "fixture",
        auto_free: false,
        default_quality: "org",
      },
      configured: { eh_cookie: true },
      revision: 2,
    },
    sourceSaves: [],
    sourceCooldowns: {},
    downloads: [],
    downloadActions: [],
    downloadListFailure: false,
    duplicatePairs: [],
    ignoredPairs: [],
    aliases: [],
    aliasSaves: [],
    authenticated: false,
    saves: [],
    requests: [],
    failSave: false,
    failPage: null,
    pageDelay: 0,
  });
  const images = new Map();
  function page(book, index) {
    const key = book.id + index;
    if (images.has(key)) return images.get(key);
    const canvas = document.createElement("canvas");
    canvas.width = book.id === "fixture-2" && index === 1 ? 1400 : 720;
    canvas.height =
      book.id === "fixture-2" && index === 1 ? 800 : index === 2 ? 2200 : 1000;
    const ctx = canvas.getContext("2d");
    const n = books.indexOf(book);
    ctx.fillStyle = colors[n];
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#ffffff18";
    ctx.beginPath();
    ctx.arc(600, 230, 310, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#1f372a25";
    ctx.beginPath();
    ctx.moveTo(0, 810);
    ctx.lineTo(300, 400);
    ctx.lineTo(720, 900);
    ctx.lineTo(720, 1000);
    ctx.lineTo(0, 1000);
    ctx.fill();
    ctx.fillStyle = "#dce3cf66";
    ctx.beginPath();
    ctx.moveTo(0, 900);
    ctx.lineTo(500, 500);
    ctx.lineTo(720, 780);
    ctx.lineTo(720, 1000);
    ctx.lineTo(0, 1000);
    ctx.fill();
    ctx.fillStyle = "#fff9e9";
    ctx.font = "20px sans-serif";
    ctx.fillText("THE SLOW READING SERIES", 60, 100);
    ctx.font = "48px sans-serif";
    const words = book.metadata.title.split(" ");
    if (words.length > 2) {
      ctx.fillText(words.slice(0, 2).join(" "), 60, 230);
      ctx.fillText(words.slice(2).join(" "), 60, 295);
    } else ctx.fillText(book.metadata.title, 60, 235);
    ctx.font = "18px sans-serif";
    ctx.fillText(
      index === 0
        ? "A STORY TO SPEND TIME WITH"
        : `CHAPTER ${index} · A QUIET MOMENT`,
      60,
      370,
    );
    ctx.fillRect(60, 415, 60, 3);
    ctx.font = "16px sans-serif";
    ctx.fillText(
      `MANGACOUCH / VISUAL FIXTURE                                     ${index + 1} / 4`,
      60,
      canvas.height - 60,
    );
    const blob = new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    images.set(key, blob);
    return blob;
  }
  const json = (data, status = 200) =>
    new Response(JSON.stringify(data), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  // Native lazy images do not go through the mocked fetch function.
  if (typeof MutationObserver !== "undefined" && document.documentElement) {
    const urls = new Set();
    new MutationObserver(() => {
      for (const image of document.querySelectorAll(
        'img[src^="/api/books/"]',
      )) {
        const match = image
          .getAttribute("src")
          .match(/^\/api\/books\/([^/]+)\/cover$/);
        if (!match || image.dataset.fixtureCover) continue;
        image.dataset.fixtureCover = "loading";
        const book = books.find(
          (item) => item.id === decodeURIComponent(match[1]),
        );
        if (!book) continue;
        void page(book, 0).then((blob) => {
          if (!image.isConnected) return;
          const url = URL.createObjectURL(blob);
          urls.add(url);
          image.dataset.fixtureCover = url;
          image.src = url;
        });
      }
      for (const url of urls) {
        if (![...document.images].some((image) => image.src === url)) {
          URL.revokeObjectURL(url);
          urls.delete(url);
        }
      }
    }).observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["src"],
    });
  }
  window.fetch = async (input, options = {}) => {
    const url = new URL(input, "http://fixture.local"),
      method = options.method || "GET";
    fixture.requests.push({ path: url.pathname, method });
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
      });
    if (url.pathname === "/api/auth/rotate") return json({ rotated: false });
    if (url.pathname === "/api/auth/logout") {
      fixture.authenticated = false;
      return json({ ok: true });
    }
    const body = options.body ? JSON.parse(options.body) : {};
    if (url.pathname === "/api/preferences") {
      if (fixture.preferencesDelay)
        await new Promise((resolve) =>
          setTimeout(resolve, fixture.preferencesDelay),
        );
      options.signal?.throwIfAborted();
      if (fixture.preferencesFailure) return json({}, 503);
      if (method === "PUT") {
        if (body.expected_revision !== fixture.preferences.revision)
          return json({}, 409);
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
        (job) => !["done", "cancelled", "skipped"].includes(job.state),
      );
      return json({
        downloads: {
          running: open.filter((job) => job.state !== "queued").length,
          waiting: open.filter((job) => job.state === "queued").length,
          completed: fixture.downloads.filter((job) => job.state === "done")
            .length,
          total: fixture.downloads.length,
          items: open.map((job) => ({
            id: job.id,
            state: job.state,
            input_url: job.input_url,
            received: job.received,
            total: job.total,
            title: null,
            retry_at: job.retry_at,
            attempts: job.attempts,
          })),
        },
        scan: null,
        source_checks: {},
        cooldowns: fixture.sourceCooldowns,
      });
    }
    if (url.pathname === "/api/settings/summary")
      return json({
        review: 3,
        sync: { pending: 2 },
        indexed_pages: 8,
        total_pages: 20,
        translations: 1,
        devices: 1,
        recycle: 0,
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
        book.sidecar.pending_revision > book.sidecar.synced_revision,
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
        (book) => book.id > (body.after_id || "") && book.id <= through,
      );
      const chunk = remaining.slice(
        0,
        Math.min(body.limit || 100, fixture.maintenanceBatchSize),
      );
      return json({
        checked: chunk.length,
        through_id: through,
        after_id: remaining.length > chunk.length ? chunk.at(-1).id : null,
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
          before = Number(url.searchParams.get("before") || Infinity);
        const items = [...fixture.removals]
          .reverse()
          .filter((item) => item.cursor < before)
          .slice(0, limit);
        return json({
          items,
          before: items.length === limit ? items.at(-1).cursor : null,
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
        files_kept: true,
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
      return json(receipt);
    }
    if (url.pathname === "/api/duplicates")
      return json({
        groups: fixture.duplicatePairs.map(([left_id, right_id]) => ({
          left_id,
          right_id,
          books: books.filter((book) => [left_id, right_id].includes(book.id)),
          matched_pages: 4,
          coverage: 1,
        })),
        indexed_pages: 20,
        total_pages: 20,
      });
    if (url.pathname === "/api/duplicates/ignore") {
      fixture.ignoredPairs.push([body.left_id, body.right_id]);
      fixture.duplicatePairs = fixture.duplicatePairs.filter(
        ([left]) => left !== body.left_id,
      );
      return json({ ignored: true });
    }
    if (url.pathname === "/api/translations")
      return json({ updated_at: null, count: 0 });
    if (url.pathname === "/api/tag-aliases") {
      if (method === "PUT") {
        fixture.aliasSaves.push(body);
        const index = fixture.aliases.findIndex(
          (item) =>
            item.namespace === body.tag.namespace &&
            item.value === body.tag.value,
        );
        if (index >= 0 && fixture.aliases[index].translation !== body.expected)
          return json({ error: "revision_conflict" }, 409);
        if (!body.translation) fixture.aliases.splice(index, 1);
        else if (index >= 0)
          fixture.aliases[index].translation = body.translation;
        else
          fixture.aliases.push({ ...body.tag, translation: body.translation });
        return json({ saved: true });
      }
      return json({ aliases: structuredClone(fixture.aliases), after: null });
    }
    if (url.pathname === "/api/sources/settings") {
      if (method === "PUT") {
        if (body.expected_revision !== fixture.sourceConfig.revision)
          return json(
            { error: "revision_conflict", message: "Settings changed" },
            409,
          );
        fixture.sourceSaves.push(body);
        const configured = { ...fixture.sourceConfig.configured };
        for (const [key, value] of Object.entries(body.secrets || {}))
          configured[key] = !!value;
        fixture.sourceConfig = {
          settings: structuredClone(body.settings),
          configured,
          revision: fixture.sourceConfig.revision + 1,
        };
      }
      return json({
        ...fixture.sourceConfig,
        cooldowns: fixture.sourceCooldowns,
      });
    }
    const terminalDownload = ["done", "cancelled", "skipped"];
    if (url.pathname === "/api/downloads/batch") {
      const items = body.urls.map((input) => {
        const match = input.match(
          /^https:\/\/(e-hentai\.org|exhentai\.org|nhentai\.net)\/g\/(\d+)\/?(\w+)?/,
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
        let job = fixture.downloads.find(
          (item) => item.input_url === canonical,
        );
        if (!job) {
          const now = Math.floor(Date.now() / 1000);
          job = {
            id: `fixture-job-${fixture.downloads.length + 1}`,
            input_url: canonical,
            source: {
              site:
                { "e-hentai.org": "ehentai", "exhentai.org": "exhentai" }[
                  match[1]
                ] ?? "nhentai",
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
            details: { entry: body.entry },
          };
          fixture.downloads.push(job);
        }
        return { url: input, job: structuredClone(job), error: null };
      });
      return json({ items });
    }
    if (url.pathname === "/api/downloads") {
      if (fixture.downloadListFailure) return json({}, 503);
      const scope = url.searchParams.get("scope") || "all";
      const items = [...fixture.downloads]
        .reverse()
        .filter(
          (job) =>
            scope === "all" ||
            (scope === "history") === terminalDownload.includes(job.state),
        );
      return json({
        items: structuredClone(items),
        before: null,
        total: items.length,
      });
    }
    const downloadMatch = url.pathname.match(
      /^\/api\/downloads\/([^/]+)(\/action)?$/,
    );
    if (downloadMatch) {
      const job = fixture.downloads.find(
        (item) => item.id === downloadMatch[1],
      );
      if (!job) return json({ error: "not_found" }, 404);
      if (downloadMatch[2]) {
        fixture.downloadActions.push({ id: job.id, ...body });
        if (body.revision !== job.revision && body.action !== "cancel")
          return json({ error: "revision_conflict" }, 409);
        job.state =
          { cancel: "cancelled", pause: "paused", resume: "queued" }[
            body.action
          ] ?? job.state;
        job.revision++;
      }
      return json(structuredClone(job));
    }
    if (url.pathname === "/api/auth/credentials")
      return fixture.credentialListFailure
        ? json({}, 503)
        : json({ credentials: fixture.credentials });
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
        .filter((item) => item.kind === "session")
        .forEach((item) => (item.revoked_at = Math.floor(Date.now() / 1000)));
      fixture.authenticated = false;
      if (fixture.passcodeLoseResponse)
        throw new TypeError("fixture passcode response lost");
      return json({ sessions_revoked: true, api_keys_unchanged: true });
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
      return json({ revoked: true });
    }
    const advanceScan = () => {
      const run = fixture.scan;
      if (!run || run.state !== "active") return;
      if (fixture.scanOffline) {
        run.state = "paused";
        run.error_code = "RootUnavailable";
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
        items,
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
    const summary = (collection) => ({
      id: collection.id,
      kind: "static",
      name: collection.name,
      revision: collection.revision,
      count: collection.book_ids.length,
    });
    const detail = (collection) => {
      const offset = Number(url.searchParams.get("after_position") ?? -1) + 1,
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
        after_position: items.length === limit ? items.at(-1).position : null,
      };
    };
    if (url.pathname === "/api/collections") {
      if (method === "POST") {
        const collection = {
          id: crypto.randomUUID(),
          name: body.name.trim(),
          revision: 1,
          book_ids: [],
        };
        fixture.collections.push(collection);
        return json(detail(collection));
      }
      return json({
        collections: fixture.collections.map(summary),
        after: null,
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
      const action = collectionMatch[2],
        expected =
          body.expected_revision ??
          (url.searchParams.has("expected_revision")
            ? Number(url.searchParams.get("expected_revision"))
            : null);
      if (expected !== null && expected !== collection.revision)
        return json({}, 409);
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
        return json({ deleted: true });
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
      return json(detail(collection));
    }
    if (url.pathname === "/api/settings")
      return json({ max_upload_bytes: 512 * 1024 ** 2 });
    if (url.pathname === "/api/books") {
      const p = url.searchParams;
      const terms = (p.get("query") || "")
        .normalize("NFKC")
        .toLowerCase()
        .split(/\s+/)
        .filter(Boolean);
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
        if (!terms.every((term) => text.includes(term))) return false;
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
      const after = p.get("after_id");
      const boundary = after ? await key(after) : "";
      const offset = p.has("page")
        ? (Number(p.get("page")) - 1) * Number(p.get("limit") || 50)
        : 0;
      matches = ranked
        .filter((item) => item.key > boundary)
        .slice(offset, offset + Number(p.get("limit") || 50))
        .map((item) => item.book);
      return json({
        books: matches,
        total: ranked.length,
        after_id: matches.at(-1)?.id || null,
      });
    }
    if (url.pathname === "/api/sidecar/conflicts")
      return json({ conflicts: [] });
    if (url.pathname === "/api/sidecar/status") {
      if (fixture.syncStatusFailure) return json({}, 503);
      const state = (book) =>
        book.sidecar.last_error === "external_conflict"
          ? "conflict"
          : book.sidecar.last_error
            ? "error"
            : book.sidecar.pending_revision > book.sidecar.synced_revision
              ? "pending"
              : "synced";
      const counts = {
        pending: 0,
        error: 0,
        conflict: 0,
        synced: 0,
      };
      books.forEach((book) => counts[state(book)]++);
      const after = url.searchParams.get("after_id") || "";
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
          pending_revision: book.sidecar.pending_revision,
          synced_revision: book.sidecar.synced_revision,
          error: book.sidecar.last_error,
        })),
        after_id: pending.length > limit ? pending[limit - 1].id : null,
      });
    }
    if (url.pathname === "/api/sidecar/sync") {
      const body = JSON.parse(options.body);
      fixture.syncCalls.push(body);
      if (fixture.syncFailure) return json({}, fixture.syncFailure);
      const sync = (book) => {
        const state = fixture.syncOutcomes[book.id] || "synced";
        book.sidecar.last_error =
          state === "conflict"
            ? "external_conflict"
            : state === "error"
              ? "OSError"
              : null;
        if (state === "synced" || state === "unchanged")
          book.sidecar.synced_revision = book.metadata_revision;
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
              book.id > (body.after_id || "") &&
              book.id <= through &&
              (body.mode !== "pending" ||
                book.sidecar.pending_revision > book.sidecar.synced_revision ||
                book.sidecar.last_error),
          );
        // syncBatchCap lets tests force several batches out of the six fixture books.
        const limit = Math.min(body.limit || 100, fixture.syncBatchCap);
        result = {
          books: Object.fromEntries(
            pending.slice(0, limit).map((book) => [book.id, sync(book)]),
          ),
          after_id: pending.length > limit ? pending[limit - 1].id : null,
          through_id: through,
        };
      }
      if (fixture.syncDelay)
        await new Promise((resolve) => setTimeout(resolve, fixture.syncDelay));
      if (fixture.syncLoseResponse) {
        fixture.syncLoseResponse = false;
        throw new Error("fixture lost sync response");
      }
      return json(result);
    }
    const match = url.pathname.match(/^\/api\/books\/([^/]+)(.*)$/);
    if (!match) return json({}, 404);
    const book = books.find((book) => book.id === match[1]);
    if (!book) return json({}, 404);
    const action = match[2];
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
      return new Response(await page(book, index), {
        headers: { "Content-Type": "image/png" },
      });
    }
    if (action === "/personal") {
      if (fixture.failSave) return json({}, 409);
      const data = JSON.parse(options.body);
      if (data.expected_revision !== book.personal_revision)
        return json({}, 409);
      book.personal = data.state;
      book.personal_revision++;
      fixture.saves.push(structuredClone(book.personal));
      return json(book);
    }
    if (action === "/metadata") {
      const data = JSON.parse(options.body);
      if (data.expected_revision !== book.metadata_revision)
        return json({}, 409);
      book.metadata = data.metadata;
      book.metadata_revision++;
      book.sidecar.pending_revision = book.metadata_revision;
      book.review_state = "needs_review";
      book.confirmation = null;
      if (fixture.reviewProofs[book.id] === JSON.stringify(book.metadata)) {
        book.review_state = "confirmed";
        book.confirmation = {
          acknowledged_issues: ["source_absent"],
          evidence_event_id: 1,
        };
      }
      return json(book);
    }
    if (action === "/review")
      return json({
        token: String(book.metadata_revision),
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
            ? book.confirmation || { acknowledged_issues: ["source_absent"] }
            : null,
      });
    if (action === "/review/automatic") {
      fixture.reviewChecks.push(book.id);
      if (fixture.failReview) return json({}, 409);
      if (JSON.parse(options.body).token !== String(book.metadata_revision))
        return json({}, 409);
      const reasons =
        fixture.reviewProofs[book.id] === JSON.stringify(book.metadata)
          ? []
          : ["local_confirmation_missing"];
      if (!reasons.length) {
        book.review_state = "confirmed";
        book.confirmation = {
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
      book.review_state = "confirmed";
      fixture.reviewProofs[book.id] = JSON.stringify(book.metadata);
      book.confirmation = { acknowledged_issues: ["source_absent"] };
      return json(book);
    }
    return json(book);
  };
})();
