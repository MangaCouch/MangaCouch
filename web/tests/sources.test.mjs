import { all, attr, dispatch, one } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { act } from "@testing-library/react";
import { SourcesSettings } from "../src/Sources.tsx";
import { SourceCheckStatus } from "../src/SourceCheckStatus.tsx";
import {
  SourceComments,
  SourceRefresh,
  useSourceDiscussion,
} from "../src/SourceDetail.tsx";
import { button, click, renderPage, visibleText } from "./harness.mjs";
/** Renders `Component` over a fetch answered by `handler`; returns the view. */
async function mount(t, Component, handler, props) {
  globalThis.document.hidden = false;
  return (await renderPage(t, Component, { fetch: handler, props })).view;
}
/** Source comments with their discussion loaded the way the detail page loads it. */
const LoadedComments = (props) =>
  React.createElement(SourceComments, {
    ...props,
    discussion: useSourceDiscussion(props.book),
  });
const response = (value) =>
  new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
const buttons = (view, text) =>
  all(view.container, "button").filter((node) =>
    node.textContent.includes(text),
  );
/** The input inside the label that reads `text` (cookie fields are plain text, not password). */
const field = (view, text) =>
  one(
    all(view.container, "label").find((node) =>
      node.textContent.includes(text),
    ),
    "input",
  );
const config = {
  settings: {
    download_limit_bytes: 34359738368,
    comments: true,
    user_agent: "fixture",
  },
  configured: { eh_cookie: true },
  revision: 2,
  rate_events: [],
  budget: {
    ehentai: {
      level: 0,
      capacity: 1100,
      leak_per_min: 13,
      remaining: 1,
      empty_in: 0,
      penalty: 1,
      penalty_until: null,
    },
  },
  cooldowns: {},
};
test("source settings preserve stored credentials unless explicitly edited or cleared", async (t) => {
  let sent, revision;
  const view = await mount(t, SourcesSettings, async (url, options = {}) => {
    if (options.method === "PUT") {
      sent = JSON.parse(options.body);
      revision = new URL(url, "http://x").searchParams.get("expected_revision");
      return response({ ...config, revision: 3 });
    }
    return response(config);
  });
  const cookie = field(view, "E-Hentai Cookie");
  // A saved value is never shown again: the field is empty and says one is saved.
  assert.equal(attr(cookie, "value"), "");
  assert.match(
    attr(cookie, "placeholder"),
    /Saved — enter a new value to replace/,
  );
  assert.match(
    attr(field(view, "ExHentai Cookie"), "placeholder"),
    /Not configured/,
  );
  assert.equal(sent, undefined);
  await act(async () =>
    dispatch(cookie, "change", { target: { value: "cookie=example" } }),
  );
  await act(async () =>
    dispatch(
      all(view.container, "button").find(
        (n) => attr(n, "aria-label") === "Save E-Hentai Cookie",
      ),
      "click",
    ),
  );
  assert.deepEqual(sent.secrets, { eh_cookie: "cookie=example" });
  assert.equal(revision, "2");
  await act(async () =>
    dispatch(
      all(view.container, "button").find(
        (n) => attr(n, "aria-label") === "Clear E-Hentai Cookie",
      ),
      "click",
    ),
  );
  // Clearing asks first; nothing is sent until it is confirmed.
  assert.deepEqual(sent.secrets, { eh_cookie: "cookie=example" });
  await act(async () => dispatch(buttons(view, "Clear").at(-1), "click"));
  assert.deepEqual(sent.secrets, { eh_cookie: "" });
});
async function sources(t, prepare) {
  const x = await renderPage(t, SourcesSettings, { prepare });
  const input = (label) =>
    one(
      all(x.view.container, "label").find((node) =>
        node.textContent.includes(label),
      ),
      "input",
    );
  return { ...x, input };
}
test("credential fields are plain text and never show a saved value", async (t) => {
  const x = await sources(t, (f) => {
    f.sourceConfig.configured = { eh_cookie: true, ex_cookie: true };
  });
  const fields = ["E-Hentai Cookie", "ExHentai Cookie", "Proxy URL"].map(
    x.input,
  );
  assert.ok(fields.every((n) => attr(n, "type") === "text"));
  assert.ok(fields.every((n) => attr(n, "autoComplete") === "off"));
  assert.equal(
    all(x.view.container, "input").filter((n) => attr(n, "type") === "password")
      .length,
    0,
  );
  for (const n of [x.input("E-Hentai Cookie"), x.input("ExHentai Cookie")]) {
    assert.equal(n.value, "");
    assert.equal(
      attr(n, "placeholder"),
      "Saved — enter a new value to replace",
    );
  }
});
test("leaving an unchanged field does not save; a real change saves once", async (t) => {
  const x = await sources(t);
  await act(async () => dispatch(x.input("User-Agent"), "blur"));
  await act(async () => dispatch(x.input("E-Hentai Cookie"), "blur"));
  assert.equal(x.fixture.sourceSaves.length, 0);
  await act(async () =>
    dispatch(x.input("User-Agent"), "change", { target: { value: "Agent/2" } }),
  );
  await act(async () => dispatch(x.input("User-Agent"), "blur"));
  await act(async () => dispatch(x.input("User-Agent"), "blur"));
  assert.equal(x.fixture.sourceSaves.length, 1);
  assert.equal(x.fixture.sourceSaves[0].settings.user_agent, "Agent/2");
  assert.equal(x.fixture.sourceConfig.revision, 3);
});
test("E-Hentai budget limits save whole numbers within their ranges only", async (t) => {
  const x = await sources(t);
  const capacity = () => x.input("Capacity");
  const recovery = () => x.input("Recovery per minute");
  const concurrency = () => x.input("Max concurrency");
  assert.deepEqual(
    [capacity(), recovery(), concurrency()].map((field) =>
      attr(field, "value"),
    ),
    [1100, 13, 4],
  );
  for (const value of ["99", "3001", "850.5"]) {
    await act(async () =>
      dispatch(capacity(), "change", { target: { value } }),
    );
    await act(async () => dispatch(capacity(), "blur"));
    assert.match(x.text(), /whole number from 100–3000/);
  }
  await act(async () =>
    dispatch(capacity(), "change", { target: { value: "900" } }),
  );
  await act(async () =>
    dispatch(concurrency(), "change", { target: { value: "9" } }),
  );
  await act(async () => dispatch(concurrency(), "blur"));
  assert.match(x.text(), /whole number from 1–8/);
  assert.equal(x.fixture.sourceSaves.length, 0);
  await act(async () =>
    dispatch(concurrency(), "change", { target: { value: "2" } }),
  );
  await act(async () =>
    dispatch(recovery(), "change", { target: { value: "16" } }),
  );
  await act(async () => dispatch(recovery(), "blur"));
  assert.doesNotMatch(x.text(), /whole number from/);
  assert.equal(x.fixture.sourceSaves.length, 1);
  const saved = x.fixture.sourceSaves[0].settings;
  assert.deepEqual(
    [
      saved.eh_budget_capacity,
      saved.eh_budget_leak_per_min,
      saved.eh_max_concurrency,
    ],
    [900, 16, 2],
  );
});
test("a limit out of range stays unsaved without failing other settings", async (t) => {
  const x = await sources(t);
  const capacity = () => x.input("Capacity");
  await act(async () =>
    dispatch(capacity(), "change", { target: { value: "99" } }),
  );
  await act(async () => dispatch(capacity(), "blur"));
  assert.match(x.text(), /whole number from 100–3000/);
  await click(x.input("Fetch source comments"));
  assert.equal(x.fixture.sourceSaves.length, 1);
  const saved = x.fixture.sourceSaves[0].settings;
  assert.equal(saved.comments, false);
  assert.equal(saved.eh_budget_capacity, 1100);
  assert.doesNotMatch(x.text(), /Invalid input/);
  assert.match(x.text(), /whole number from 100–3000/);
  assert.equal(attr(capacity(), "value"), 99);
});
test("the usage meter shows what is left, when it is full again", async (t) => {
  const x = await sources(t);
  assert.match(x.text(), /EH 72% left, full again in about 24 min/);
  assert.doesNotMatch(
    x.text(),
    /Reduced after a rate limit|Recent rate limits/,
  );
});
test("a penalty and the recent rate limits are shown", async (t) => {
  const x = await sources(t, (fixture) => {
    fixture.sourceBudget.ehentai = {
      ...fixture.sourceBudget.ehentai,
      capacity: 737,
      remaining: 0.05,
      empty_in: 4840,
      penalty: 0.67,
      penalty_until: 1_800_086_400,
    };
    fixture.sourceConfig.rate_events = [
      {
        at: 1_800_000_000,
        site: "ehentai",
        kind: "page",
        reason: "ban",
        until: 1_800_000_600,
        level: 1012,
        remaining: 0.08,
        penalty: 0.67,
      },
    ];
  });
  assert.match(x.text(), /EH 5% left, full again in about 1 h 21 min/);
  assert.match(
    x.text(),
    /Reduced after a rate limit \(capacity and drain ×0\.67\) until /,
  );
  assert.match(x.text(), /Recent rate limits/);
  assert.match(x.text(), /IP ban \(page request, 8% left\), halted until /);
});
test("background checks fetch comments only when asked and comments are on", async (t) => {
  const x = await sources(t);
  const background = () => x.input("Fetch comments in background checks");
  assert.equal(attr(background(), "checked"), false);
  await act(async () =>
    dispatch(background(), "change", { target: { checked: true } }),
  );
  assert.equal(x.fixture.sourceSaves.at(-1).settings.background_comments, true);
  await act(async () =>
    dispatch(x.input("Fetch source comments"), "change", {
      target: { checked: false },
    }),
  );
  assert.equal(attr(background(), "checked"), false);
  assert.equal(attr(background(), "disabled"), true);
});
test("fake-IP DNS answers stay refused until explicitly allowed, with proxy guidance", async (t) => {
  const x = await sources(t);
  const box = x.input("Allow fake-IP DNS answers");
  assert.equal(attr(box, "checked"), false);
  await x.click("About proxy");
  assert.match(
    x.text(),
    /Source requests ignore system and environment proxies/,
  );
  await x.click("About fake-IP DNS");
  assert.match(
    x.text(),
    /Other private addresses and IP-address links stay refused/,
  );
  await act(async () => dispatch(box, "change", { target: { checked: true } }));
  assert.equal(x.fixture.sourceSaves.length, 1);
  assert.equal(x.fixture.sourceSaves[0].settings.allow_fake_ip, true);
  assert.equal(attr(x.input("Allow fake-IP DNS answers"), "checked"), true);
});
for (const detail of ["source_network", "source_login_required"]) {
  test(`source verification reports ${detail} without a success marker`, async (t) => {
    const view = await mount(t, SourcesSettings, async (url) =>
      response(
        url.includes("/verify/")
          ? {
              reachable: detail !== "source_network",
              authenticated: false,
              detail,
            }
          : config,
      ),
    );
    await act(async () =>
      dispatch(
        all(view.container, "button").find(
          (n) => attr(n, "aria-label") === "Verify E-Hentai",
        ),
        "click",
      ),
    );
    assert.equal(
      all(view.container, "svg").filter(
        (n) => attr(n, "aria-label") === "Verified",
      ).length,
      0,
    );
  });
}
test("source verification checks the typed, unsaved values and requires authentication", async (t) => {
  let saved = 0;
  const verified = [];
  const view = await mount(t, SourcesSettings, async (url, options = {}) => {
    if (options.method === "PUT") saved++;
    if (url.includes("/verify/")) verified.push(JSON.parse(options.body));
    return response(
      url.includes("/verify/")
        ? { reachable: true, authenticated: true, detail: null }
        : config,
    );
  });
  const verify = () =>
    act(async () =>
      dispatch(
        all(view.container, "button").find(
          (n) => attr(n, "aria-label") === "Verify E-Hentai",
        ),
        "click",
      ),
    );
  const marks = () =>
    all(view.container, "svg").filter(
      (n) => attr(n, "aria-label") === "Verified",
    ).length;
  // Nothing typed: the saved values are checked.
  await verify();
  assert.deepEqual(verified.at(-1), { secrets: {} });
  for (const [label, value] of [
    ["E-Hentai Cookie", "cookie=edited"],
    ["ExHentai Cookie", "cookie=ex-edited"],
  ])
    await act(async () =>
      dispatch(field(view, label), "change", { target: { value } }),
    );
  // Typing drops the earlier result; verifying sends the typed values without saving them.
  assert.equal(marks(), 0);
  await verify();
  assert.deepEqual(verified.at(-1), {
    secrets: { eh_cookie: "cookie=edited", ex_cookie: "cookie=ex-edited" },
  });
  assert.equal(saved, 0);
  assert.equal(marks(), 1);
  assert.equal(attr(field(view, "E-Hentai Cookie"), "value"), "cookie=edited");
});
test("a save rejected by another device reloads the settings, keeps the change and can be retried", async (t) => {
  const x = await sources(t);
  // Another tab saves first.
  x.fixture.sourceConfig = {
    ...x.fixture.sourceConfig,
    settings: { ...x.fixture.sourceConfig.settings, user_agent: "Other/1" },
    revision: 5,
  };
  await act(async () =>
    dispatch(x.input("Fetch source comments"), "change", {
      target: { checked: false },
    }),
  );
  assert.equal(x.fixture.sourceSaves.length, 0);
  assert.match(x.text(), /changed elsewhere and were reloaded/);
  assert.equal(attr(x.input("User-Agent"), "value"), "Other/1");
  assert.equal(attr(x.input("Fetch source comments"), "checked"), false);
  await click(button(x.view, "Save my change again"));
  assert.equal(x.fixture.sourceSaves.length, 1);
  assert.equal(x.fixture.sourceSaves[0].expected_revision, 5);
  assert.equal(x.fixture.sourceSaves[0].settings.comments, false);
  assert.equal(x.fixture.sourceSaves[0].settings.user_agent, "Other/1");
  assert.doesNotMatch(x.text(), /changed elsewhere/);
});
for (const [state, notice] of [
  ["pending", /Saved; file sync pending/],
  ["conflict", /Saved; metadata file sync needs attention/],
]) {
  test(`source refresh reports ${state} sidecar sync alongside comment failures`, async (t) => {
    const book = {
      id: "book",
      metadata_revision: 1,
      metadata: {
        source: { site: "ehentai", gallery_id: 10, gallery_token: "abc" },
      },
    };
    let updated;
    const view = await mount(
      t,
      SourceRefresh,
      async () =>
        response({
          book: { ...book, metadata_revision: 2 },
          comments_error: "source_rate_limited",
          sync: { state },
        }),
      {
        book,
        notes: [],
        update: (value) => {
          updated = value;
        },
      },
    );
    await act(async () =>
      dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
    );
    assert.equal(updated.metadata_revision, 2);
    assert.match(visibleText(view), notice);
    assert.match(visibleText(view), /Comments were rate limited/);
    assert.equal(all(view.container, { role: "alert" }).length, 0);
  });
}
test("source refresh sends the exact revision", async (t) => {
  let received, updated;
  const book = {
    id: "book",
    metadata_revision: 8,
    metadata: {
      source: { site: "ehentai", gallery_id: 10, gallery_token: "abc" },
    },
  };
  const notes = [];
  const view = await mount(
    t,
    SourceRefresh,
    async (url, options) => {
      const { pathname, searchParams } = new URL(url, "http://x");
      if (pathname === "/api/books/book/source" && options.method === "POST") {
        received = [
          ...(received || []),
          {
            ...JSON.parse(options.body),
            expected_revision: Number(searchParams.get("expected_revision")),
          },
        ];
        return response({
          book: { ...book, metadata_revision: 9 + received.length - 1 },
          comments_error: null,
        });
      }
      return response({ error: "not_found" });
    },
    {
      book,
      notes,
      update: (value) => {
        updated = value;
      },
    },
  );
  const inputs = all(view.container, "input");
  assert.deepEqual(
    inputs.map((input) => attr(input, "value")),
    ["https://e-hentai.org/g/10/abc/"],
  );
  await act(async () =>
    dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
  );
  assert.deepEqual(
    received.map((body) => [body.url, body.expected_revision]),
    [["https://e-hentai.org/g/10/abc/", 8]],
  );
  assert.equal(updated.metadata_revision, 9);
  assert.match(visibleText(view), /Updated/);
});
test("a refresh a rate limit put off says it is queued and finishes by itself", async (t) => {
  const book = {
    id: "book",
    metadata_revision: 8,
    metadata: {
      source: { site: "ehentai", gallery_id: 10, gallery_token: "abc" },
    },
  };
  const retryAt = Math.floor(Date.now() / 1000) + 600;
  const view = await mount(
    t,
    SourceRefresh,
    async (url, options) => {
      if (options.method === "POST")
        return new Response(
          JSON.stringify({ book, comments_error: null, retry_at: retryAt }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        );
      return response({ error: "not_found" });
    },
    { book, notes: [], update: () => {} },
  );
  await act(async () =>
    dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
  );
  assert.match(
    visibleText(view),
    /Queued: it finishes by itself in the background around .*even if you close this page/,
  );
  assert.doesNotMatch(visibleText(view), /Updated/);
});
test("comments a refresh could not read because of a rate limit are fetched later", async (t) => {
  const book = { id: "book", metadata_revision: 8, metadata: { source: null } };
  const view = await mount(
    t,
    SourceRefresh,
    async () =>
      response({
        book: { ...book, metadata_revision: 9 },
        comments_error: "source_rate_limited",
      }),
    { book, notes: [], update: () => {} },
  );
  await act(async () =>
    dispatch(all(view.container, "input")[0], "change", {
      target: { value: "https://e-hentai.org/g/10/abc/" },
    }),
  );
  await act(async () =>
    dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
  );
  assert.match(
    visibleText(view),
    /Comments were rate limited; they are fetched in the background later/,
  );
});
test("source comments load from their own endpoint and refresh without touching the book", async (t) => {
  const posts = [];
  const source = { site: "ehentai", gallery_id: 10, gallery_token: "abc" };
  const note = {
    source,
    tags: [],
    fetched_at: 1,
    comments_sort: "source",
    comments_pages: 1,
    comments_complete: true,
  };
  const comment = (body) => ({
    source,
    comment_id: body,
    author: "reader",
    body,
    posted_at: null,
    score: null,
    is_uploader: false,
  });
  const book = { id: "book", metadata_revision: 3, metadata: { source } };
  const view = await mount(
    t,
    LoadedComments,
    async (url, options) => {
      assert.equal(url, "/api/books/book/source-comments");
      if (options.method === "POST") {
        posts.push(JSON.parse(options.body));
        return response({
          notes: [note],
          comments: [comment("Fresh remark")],
          comments_error: null,
        });
      }
      return response({ notes: [note], comments: [comment("Old remark")] });
    },
    { book },
  );
  assert.match(visibleText(view), /Old remark/);
  await click(one(view.container, { "aria-label": "Update source comments" }));
  assert.deepEqual(posts, [{ url: "https://e-hentai.org/g/10/abc/" }]);
  assert.match(visibleText(view), /Fresh remark/);
  assert.doesNotMatch(visibleText(view), /Old remark/);
});
test("source comments stay hidden for a book without scraped notes", async (t) => {
  const view = await mount(
    t,
    LoadedComments,
    async () => response({ notes: [], comments: [] }),
    { book: { id: "book", metadata_revision: 1, metadata: { source: null } } },
  );
  assert.equal(visibleText(view), "");
});
test("source settings that failed to load come back with a retry", async (t) => {
  let calls = 0;
  const view = await mount(t, SourcesSettings, async (url) => {
    if (url === "/api/sources/settings" && ++calls === 1)
      return new Response(JSON.stringify({ error: "storage_unavailable" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      });
    return response(config);
  });
  assert.match(visibleText(view), /Library storage is unavailable/);
  assert.equal(all(view.container, "input").length, 0);
  await click(button(view, "Retry"));
  assert.equal(calls, 2);
  assert.doesNotMatch(visibleText(view), /storage is unavailable/);
  assert.match(visibleText(view), /E-Hentai Cookie/);
});
test("a lost source key explains itself and resets after a confirmation", async (t) => {
  const x = await sources(t, (fixture) => {
    fixture.sourceConfig.key_lost = true;
    fixture.sourceConfig.configured = {};
  });
  assert.match(x.text(), /\.sources\.key beside the database\) is missing/);
  await x.click("Reset source credentials");
  // Nothing is reset until the dialog is confirmed.
  assert.equal(x.fixture.sourceResets, 0);
  await click(buttons(x.view, "Reset").at(-1));
  assert.equal(x.fixture.sourceResets, 1);
  assert.doesNotMatch(x.text(), /is missing or does not match/);
  assert.match(x.text(), /Source credentials were reset. Requests stay paused/);
  assert.equal(x.fixture.sourceConfig.settings.online_paused, true);
  await x.click("Resume online sources");
  assert.equal(x.fixture.sourceConfig.settings.online_paused, false);
});
test("a failed background comment fetch shows its reason with the comments", async (t) => {
  const source = { site: "ehentai", gallery_id: 10, gallery_token: "abc" };
  const note = {
    source,
    tags: [],
    fetched_at: 1,
    comments_sort: "source",
    comments_pages: 1,
    comments_complete: true,
    comments_failure: { code: "gallery_unavailable", at: 1_800_000_000 },
  };
  const view = await mount(
    t,
    LoadedComments,
    async () => response({ notes: [note], comments: [] }),
    { book: { id: "book", metadata_revision: 1, metadata: { source } } },
  );
  assert.match(
    visibleText(view),
    /The last background fetch failed: The gallery has been removed/,
  );
});
test("a failed comment fetch shows even before anything was fetched from the source", async (t) => {
  const source = { site: "ehentai", gallery_id: 10, gallery_token: "abc" };
  const note = {
    source,
    tags: [],
    fetched_at: null,
    comments_sort: "source",
    comments_pages: 0,
    comments_complete: false,
    comments_failure: { code: "gallery_unavailable", at: 1_800_000_000 },
  };
  const view = await mount(
    t,
    LoadedComments,
    async () => response({ notes: [note], comments: [] }),
    { book: { id: "book", metadata_revision: 1, metadata: { source } } },
  );
  assert.match(
    visibleText(view),
    /The last background fetch failed: The gallery has been removed/,
  );
  assert.doesNotMatch(visibleText(view), /1970/);
});
const checked = (check) => ({
  id: "book",
  metadata_revision: 1,
  metadata: { source: null },
  source_check: {
    book_id: "book",
    generation: 4,
    reasons: [],
    retry_at: 0,
    updated_at: 1,
    ...check,
  },
});
test("a failed source check shows its reason and retries with its generation", async (t) => {
  const posts = [],
    updates = [];
  const view = await mount(
    t,
    SourceCheckStatus,
    async (url, options) => {
      posts.push([url, JSON.parse(options.body)]);
      return response(checked({ state: "queued", generation: 5 }).source_check);
    },
    {
      book: checked({ state: "needs_review", error_code: "source_network" }),
      update: (book) => updates.push(book),
    },
  );
  assert.match(
    visibleText(view),
    /Source check failed: Source connection failed/,
  );
  await click(button(view, "Retry"));
  assert.deepEqual(posts, [
    ["/api/books/book/source-check?expected_generation=4", { action: "retry" }],
  ]);
  assert.equal(updates.at(-1).source_check.state, "queued");
});
test("a check that retries by itself says when, and a done check shows nothing", async (t) => {
  const retryAt = Math.floor(Date.now() / 1000) + 300;
  const waiting = await mount(t, SourceCheckStatus, async () => response({}), {
    book: checked({
      state: "queued",
      error_code: "source_network",
      retry_at: retryAt,
    }),
    update: () => {},
  });
  assert.match(
    visibleText(waiting),
    /The source check retries by itself around .* \(Source connection failed/,
  );
  assert.equal(all(waiting.container, "button").length, 0);
  const done = await mount(t, SourceCheckStatus, async () => response({}), {
    book: checked({ state: "done", error_code: null }),
    update: () => {},
  });
  assert.equal(visibleText(done), "");
});

test("empty reserve restores its saved GiB value and whitespace cannot clear a credential", async (t) => {
  const x = await sources(t);
  const reserve = x.input("Download disk reserve (GiB)");
  const before = attr(reserve, "value");
  await act(async () => dispatch(reserve, "change", { target: { value: "" } }));
  await act(async () => dispatch(reserve, "blur"));
  assert.equal(attr(reserve, "value"), before);
  assert.equal(x.fixture.sourceSaves.length, 0);
  await act(async () =>
    dispatch(x.input("E-Hentai Cookie"), "change", {
      target: { value: "   " },
    }),
  );
  assert.equal(attr(x.button("Save E-Hentai Cookie"), "disabled"), true);
  assert.equal(x.fixture.sourceConfig.configured.eh_cookie, true);
  await act(async () =>
    dispatch(reserve, "change", { target: { value: "-1" } }),
  );
  await act(async () => dispatch(reserve, "blur"));
  assert.match(reserve.closest("label").textContent, /\(GiB\).*GiB\./);
  assert.equal(x.fixture.sourceSaves.length, 0);
});

test("size limits show their defaults as whole numbers and typeable range hints", async (t) => {
  const x = await sources(t);
  const reserve = x.input("Download disk reserve (GiB)");
  const size = x.input("Maximum download size (GiB)");
  // 10 GiB and 32 GiB: one unit, and both server defaults are whole in it.
  assert.equal(String(attr(reserve, "value")), "10");
  assert.equal(String(attr(size, "value")), "32");
  const hint = (input) =>
    input.closest("label").querySelector('[role="alert"]')?.textContent;
  await act(async () => dispatch(size, "change", { target: { value: "0" } }));
  await act(async () => dispatch(size, "blur"));
  assert.equal(hint(size), "Enter more than 0 and at most 1048576 GiB.");
  await act(async () =>
    dispatch(reserve, "change", { target: { value: "-1" } }),
  );
  await act(async () => dispatch(reserve, "blur"));
  assert.equal(hint(reserve), "Enter 0–1048576 GiB.");
  assert.equal(x.fixture.sourceSaves.length, 0);
  await act(async () =>
    dispatch(reserve, "change", { target: { value: "12.5" } }),
  );
  await act(async () => dispatch(reserve, "blur"));
  assert.equal(x.fixture.sourceSaves.length, 1);
  const saved = x.fixture.sourceSaves[0].settings;
  assert.equal(saved.download_reserve_bytes, 12.5 * 1024 ** 3);
  assert.equal(saved.download_limit_bytes, 32 * 1024 ** 3);
});

test("size limit range hints read in Chinese too", async (t) => {
  const x = await renderPage(t, SourcesSettings, {
    props: { t: (zh) => zh },
  });
  const input = (label) =>
    one(
      all(x.view.container, "label").find((node) =>
        node.textContent.includes(label),
      ),
      "input",
    );
  const size = input("单个下载大小上限（GiB）");
  assert.equal(String(attr(input("下载磁盘保留空间（GiB）"), "value")), "10");
  await act(async () => dispatch(size, "change", { target: { value: "0" } }));
  await act(async () => dispatch(size, "blur"));
  assert.equal(
    size.closest("label").querySelector('[role="alert"]').textContent,
    "请输入大于 0、不超过 1048576 GiB 的数值。",
  );
});

test("proxy validation is shown next to the proxy field and can be corrected", async (t) => {
  const x = await sources(t);
  const fetch = globalThis.fetch;
  t.mock.method(globalThis, "fetch", async (path, options) =>
    options?.method === "PUT" &&
    JSON.parse(options.body).secrets?.proxy === "ftp://bad"
      ? new Response(JSON.stringify({ error: "invalid_proxy" }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        })
      : fetch(path, options),
  );
  const field = x.input("Proxy URL");
  await act(async () =>
    dispatch(field, "change", { target: { value: "ftp://bad" } }),
  );
  await x.click("Save Proxy URL");
  assert.match(field.closest("label").textContent, /http|socks/i);
  await act(async () =>
    dispatch(field, "change", { target: { value: "http://localhost:8080" } }),
  );
  await x.click("Save Proxy URL");
  assert.equal(field.closest("label").querySelector('[role="alert"]'), null);
  assert.equal(x.fixture.sourceConfig.configured.proxy, true);
});
