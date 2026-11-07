import { all, attr, dispatch, one } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { toast } from "sonner";
import { SourcesSettings } from "../src/Sources.tsx";
import { SourceComments, SourceRefresh } from "../src/SourceDetail.tsx";
import { Downloads } from "../src/Downloads.tsx";
import { button, click, setupFixture, visibleText } from "./harness.mjs";
const translate = (_, en) => en;
async function mount(t, Component, handler, props = {}) {
  const original = { document: globalThis.document, window: globalThis.window };
  document.hidden = false;
  globalThis.window = globalThis.window;
  t.mock.method(globalThis, "fetch", handler);
  let view;
  await act(async () => {
    view = render(
      React.createElement(Component, {
        t: translate,
        navigate: () => {},
        ...props,
      }),
    );
  });
  t.after(async () => {
    await act(async () => view.unmount());
    for (const [k, v] of Object.entries(original)) {
      if (v === undefined) delete globalThis[k];
      else globalThis[k] = v;
    }
  });
  return view;
}
const response = (value) =>
  new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
const buttons = (view, text) =>
  all(view.container, "button").filter((node) =>
    node.textContent.includes(text),
  );
const config = {
  settings: {
    download_limit_bytes: 34359738368,
    comments: true,
    user_agent: "fixture",
  },
  configured: { eh_cookie: true },
  revision: 2,
};
test("source settings preserve stored credentials unless explicitly edited or cleared", async (t) => {
  let sent;
  const view = await mount(t, SourcesSettings, async (url, options = {}) => {
    if (options.method === "PUT") {
      sent = JSON.parse(options.body);
      return response({ ...config, revision: 3 });
    }
    return response(config);
  });
  const password = all(view.container, "input").filter(
    (n) => attr(n, "type") === "password",
  );
  assert.equal(attr(password[0], "value"), "");
  assert.match(attr(password[0], "placeholder"), /Configured/);
  assert.equal(sent, undefined);
  await act(async () =>
    dispatch(password[0], "change", { target: { value: "cookie=example" } }),
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
  assert.equal(sent.expected_revision, 2);
  await act(async () =>
    dispatch(
      all(view.container, "button").find(
        (n) => attr(n, "aria-label") === "Clear E-Hentai Cookie",
      ),
      "click",
    ),
  );
  assert.deepEqual(sent.secrets, { eh_cookie: "" });
});
async function sources(t, prepare = () => {}) {
  const x = await setupFixture(t, prepare);
  const view = await x.render(
    React.createElement(SourcesSettings, { t: translate }),
  );
  const input = (label) =>
    one(
      all(view.container, "label").find((node) =>
        node.textContent.includes(label),
      ),
      "input",
    );
  return { ...x, view, input, text: () => visibleText(view) };
}
test("secret fields ask browsers not to autofill the library passcode", async (t) => {
  const x = await sources(t);
  const secrets = all(x.view.container, "input").filter(
    (n) => attr(n, "type") === "password",
  );
  assert.equal(secrets.length, 5);
  assert.ok(secrets.every((n) => attr(n, "autoComplete") === "new-password"));
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
for (const detail of [
  "source_network",
  "source_login_required",
  "nh_key_required",
]) {
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
          (n) => attr(n, "aria-label") === "Verify nHentai",
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
test("source verification requires authentication, and secret edits wait for Save", async (t) => {
  let saved = 0;
  const view = await mount(t, SourcesSettings, async (url, options = {}) => {
    if (options.method === "PUT") saved++;
    return response(
      url.includes("/verify/")
        ? { reachable: true, authenticated: true, detail: null }
        : config,
    );
  });
  const secret = all(view.container, "input").find(
    (n) => attr(n, "type") === "password",
  );
  await act(async () =>
    dispatch(secret, "change", { target: { value: "cookie=edited" } }),
  );
  assert.equal(saved, 0);
  await act(async () =>
    dispatch(
      all(view.container, "button").find(
        (n) => attr(n, "aria-label") === "Verify nHentai",
      ),
      "click",
    ),
  );
  assert.equal(
    all(view.container, "svg").filter(
      (n) => attr(n, "aria-label") === "Verified",
    ).length,
    1,
  );
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
test("source refresh sends exact revision and prefills links from stored source notes", async (t) => {
  let received, updated;
  const book = {
    id: "book",
    metadata_revision: 8,
    metadata: {
      source: { site: "ehentai", gallery_id: 10, gallery_token: "abc" },
    },
  };
  const view = await mount(
    t,
    SourceRefresh,
    async (url, options) => {
      if (url === "/api/books/book/source-comments")
        return response({
          notes: [
            {
              source: { site: "nhentai", gallery_id: 77, gallery_token: null },
              tags: [],
              fetched_at: 1,
              comments_sort: "source",
              comments_pages: 1,
              comments_complete: true,
            },
          ],
          comments: [],
        });
      if (url === "/api/books/book/source" && options.method === "POST") {
        received = [...(received || []), JSON.parse(options.body)];
        return response({
          book: { ...book, metadata_revision: 9 + received.length - 1 },
          comments_error: null,
        });
      }
      return response({ error: "not_found" });
    },
    {
      book,
      update: (value) => {
        updated = value;
      },
    },
  );
  const inputs = all(view.container, "input");
  assert.deepEqual(
    inputs.map((input) => attr(input, "value")),
    ["https://e-hentai.org/g/10/abc/", "https://nhentai.net/g/77/"],
  );
  await act(async () =>
    dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
  );
  assert.deepEqual(
    received.map((body) => [body.url, body.expected_revision]),
    [
      ["https://e-hentai.org/g/10/abc/", 8],
      ["https://nhentai.net/g/77/", 9],
    ],
  );
  assert.equal(updated.metadata_revision, 10);
  assert.match(visibleText(view), /Updated/);
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
    SourceComments,
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
    SourceComments,
    async () => response({ notes: [], comments: [] }),
    { book: { id: "book", metadata_revision: 1, metadata: { source: null } } },
  );
  assert.equal(visibleText(view), "");
});
const job = {
  id: "job",
  input_url: "https://e-hentai.org/g/10/abc/",
  source: { site: "ehentai", gallery_id: 10, gallery_token: "abc" },
  revision: 5,
  state: "needs_approval",
  details: { quote: { original_gp: 25, resample_gp: 0, balance_gp: 100 } },
  received: 0,
  total: null,
  error_code: null,
};
test("download confirmation submits once, without a second modal", async (t) => {
  const mutations = [];
  const view = await mount(t, Downloads, async (url, options = {}) => {
    if (options.method === "POST") {
      mutations.push(JSON.parse(options.body));
      return response({ ...job, state: "queued", revision: 6 });
    }
    return response({ items: [job], before: null, total: 1 });
  });
  assert.equal(attr(one(view.container, "select"), "value"), "org");
  await act(async () => dispatch(buttons(view, "Download")[0], "click"));
  assert.deepEqual(mutations, [
    { action: "approve", revision: 5, mode: "org" },
  ]);
  assert.equal(all(view.container, "dialog").length, 0);
});
test("failed EH search exposes explicit choices without auto fallback", async (t) => {
  let writes = 0;
  const nh = {
    ...job,
    state: "needs_choice",
    input_url: "https://nhentai.net/g/20/",
    source: { site: "nhentai", gallery_id: 20 },
    error_code: "source_search_failed",
    details: {
      original: {
        metadata: {
          title: "Fixture",
          source: { site: "nhentai", gallery_id: 20 },
          language: "English",
          declared_page_count: 20,
        },
      },
    },
  };
  const view = await mount(t, Downloads, async (url, options = {}) => {
    if (options.method === "POST") writes++;
    return response({ items: [nh], before: null });
  });
  assert.equal(writes, 0);
  const rendered = visibleText(view);
  assert.match(rendered, /E\(x\)Hentai search failed/);
  assert.match(rendered, /Find E\(x\)Hentai edition/);
  assert.ok(
    all(view.container, "input").some(
      (n) => attr(n, "aria-label") === "E(x)Hentai URL",
    ),
  );
});
test("archive mode chosen on the task survives the fee confirmation", async (t) => {
  let mutation;
  const view = await mount(t, Downloads, async (url, options = {}) => {
    if (options.method === "POST") {
      mutation = JSON.parse(options.body);
      return response({ ...job, state: "queued", revision: 6 });
    }
    return response({ items: [job], before: null, total: 1 });
  });
  await act(async () =>
    dispatch(one(view.container, "select"), "change", {
      target: { value: "res" },
    }),
  );
  await act(async () => dispatch(buttons(view, "Download")[0], "click"));
  assert.equal(all(view.container, "dialog").length, 0);
  assert.equal(mutation.mode, "res");
});
test("history pages do not mutate completed tasks", async (t) => {
  const calls = [];
  const view = await mount(t, Downloads, async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET" });
    if (url.includes("scope=history"))
      return response({
        items: [
          {
            ...job,
            id: url.includes("before=") ? "older" : "recent",
            state: "done",
          },
        ],
        before: url.includes("before=") ? null : 20,
        total: 21,
      });
    return response({ items: [job], before: null, total: 1 });
  });
  await act(async () => dispatch(buttons(view, "History")[0], "click"));
  await act(async () => dispatch(buttons(view, "Next")[0], "click"));
  assert.ok(
    calls.some(({ url }) => url.includes("scope=history&limit=20&before=20")),
  );
  assert.equal(all(view.container, { className: "download-row" }).length, 1);
  assert.ok(calls.every((call) => call.method === "GET"));
  assert.equal(all(view.container, "dialog").length, 0);
});
test("review mismatch reports both exact byte values and the image comparison basis", async () => {
  const { reviewIssue } = await import("../src/ReviewDetails.tsx");
  const text = reviewIssue(
    "declared_byte_size_mismatch",
    {
      metadata: { declared_byte_size: 1048577 },
      compared_byte_size: 1048576,
      actual_byte_size: 900000,
      byte_size_basis: "images",
    },
    translate,
  );
  assert.match(text, /1,048,577 B/);
  assert.match(text, /1,048,576 B/);
  assert.match(text, /Uncompressed image total/);
  assert.doesNotMatch(text, /900,000/);
});
