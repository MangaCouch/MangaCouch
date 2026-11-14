import { one, dispatch, attr, all } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import { act } from "@testing-library/react";
import { Downloads } from "../src/Downloads.tsx";
import React from "react";
import { TaskProvider, TaskQueue } from "../src/TaskQueue.tsx";
import { button, click, renderPage, visibleText } from "./harness.mjs";
const job = (id, values = {}) => ({
  id,
  input_url: `https://e-hentai.org/g/${id.length}/abc/`,
  source: { site: "ehentai", gallery_id: 1, gallery_token: "abc" },
  state: "queued",
  revision: 1,
  received: 0,
  total: null,
  book_id: null,
  error_code: null,
  retry_at: 0,
  attempts: 0,
  created_at: 1,
  details: {},
  ...values,
});
async function setup(t, prepare = () => {}, Component = Downloads) {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  globalThis.document.hidden = false;
  const x = await renderPage(
    t,
    (props) =>
      React.createElement(
        TaskProvider,
        { enabled: true },
        React.createElement(Component, props),
      ),
    {
      prepare,
      globals: { document: globalThis.document },
    },
  );
  const textarea = () => one(x.view.container, "textarea");
  return {
    ...x,
    reads: (path) =>
      x.fixture.requests.filter((r) => r.path === path && r.method === "GET")
        .length,
    textarea,
    submit: () =>
      act(async () =>
        dispatch(one(x.view.container, "form"), "submit", {
          preventDefault() {},
        }),
      ),
    type: (value) =>
      act(async () => dispatch(textarea(), "change", { target: { value } })),
    shortcut: () =>
      act(async () =>
        dispatch(textarea(), "keyDown", {
          key: "Enter",
          ctrlKey: true,
          metaKey: false,
          preventDefault() {},
        }),
      ),
  };
}
test("several pasted links are submitted in one batch with per-line results", async (t) => {
  const x = await setup(t, (f) => {
    f.downloads.push(
      job("existing", { input_url: "https://e-hentai.org/g/7/def/" }),
    );
  });
  await x.type(
    [
      "https://e-hentai.org/g/1/abc/",
      "",
      "https://example.org/g/200/",
      "not a link",
      "https://example.com/g/5/",
      "https://e-hentai.org/g/1/abc/",
      "https://e-hentai.org/g/7/def/",
    ].join("\n"),
  );
  assert.ok(x.button("Add 6 links"));
  await x.shortcut();
  const posts = x.fixture.requests.filter(
    (r) => r.path === "/api/downloads/batch",
  );
  assert.equal(posts.length, 1);
  assert.equal(
    x.fixture.requests.filter(
      (r) => r.path === "/api/downloads" && r.method === "POST",
    ).length,
    0,
  );
  const text = x.text();
  assert.match(text, /https:\/\/e-hentai\.org\/g\/1\/abc\/Added/);
  assert.match(text, /https:\/\/example\.org\/g\/200\/Unsupported site/);
  assert.match(text, /not a linkNot a recognized gallery link/);
  assert.match(text, /example\.com\/g\/5\/Unsupported site/);
  assert.equal((text.match(/Already queued/g) ?? []).length, 2);
  // Only the lines that need correcting stay in the box.
  assert.equal(
    attr(x.textarea(), "value"),
    "https://example.org/g/200/\nnot a link\nhttps://example.com/g/5/",
  );
  assert.equal(x.fixture.downloads.length, 2);
  assert.equal(all(x.view.container, { className: "download-row" }).length, 2);
});
test("automatic retries and source cooldowns are explained in the queue and the task popover", async (t) => {
  const later = Math.floor(Date.now() / 1000) + 600;
  const prepare = (f) => {
    f.downloads.push(
      job("waiting", {
        retry_at: later,
        attempts: 2,
        error_code: "source_network",
      }),
    );
    f.sourceCooldowns = { ehentai: later, exhentai: later };
  };
  const x = await setup(t, prepare);
  const text = x.text();
  assert.match(text, /Retrying at .+, attempt 3/);
  assert.match(text, /Source connection failed/);
  assert.match(
    text,
    /E-Hentai \/ ExHentai rate-limited until .+; downloads and source checks resume automatically/,
  );
});
test("the task popover shows the same retry and cooldown lines", async (t) => {
  const later = Math.floor(Date.now() / 1000) + 600;
  const queue = await setup(
    t,
    (f) => {
      f.downloads.push(job("waiting", { retry_at: later, attempts: 2 }));
      f.sourceCooldowns = { ehentai: later };
    },
    TaskQueue,
  );
  await queue.click("Task queue");
  assert.match(queue.text(), /Retrying at .+, attempt 3/);
  assert.match(queue.text(), /E-Hentai rate-limited until/);
});
test("the list polls fast only while active jobs move and slowly otherwise", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(job("running", { state: "downloading" })),
  );
  const reads = () => x.reads("/api/downloads");
  assert.equal(reads(), 1);
  await x.tick(2000);
  assert.equal(reads(), 2);
  x.fixture.downloads[0].state = "needs_approval";
  await x.tick(2000);
  assert.equal(reads(), 3);
  await x.tick(2000);
  assert.equal(reads(), 3);
  await x.tick(8000);
  assert.equal(reads(), 4);
  await x.click("History");
  assert.equal(reads(), 5);
  await x.tick(30000);
  assert.equal(reads(), 5);
});
test("a failed refresh after a successful action is not reported as a failed action", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(job("running", { state: "downloading" })),
  );
  x.fixture.downloadListFailure = true;
  await x.click("Pause");
  assert.equal(x.fixture.downloads[0].state, "pausing");
  await x.tick(2000);
  assert.match(
    x.text(),
    /Could not refresh the downloads: Library storage is unavailable/,
  );
  x.fixture.downloadListFailure = false;
  await x.tick(10000);
  assert.doesNotMatch(x.text(), /Could not refresh/);
});
test("the cancel action is offered consistently and never for finished or cancelling jobs", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("a", { state: "cancelling" }),
      job("b", { state: "paused" }),
    ),
  );
  const cancels = all(x.view.container, "button").filter(
    (node) => attr(node, "aria-label") === "Cancel task",
  );
  assert.equal(cancels.length, 1);
});
test("jobs winding down after pause or cancel still count as running but offer no pause", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("a", { state: "pausing" }),
      job("b", { state: "cancelling" }),
    ),
  );
  assert.equal(x.view.container.querySelectorAll(".spinner").length, 2);
  assert.equal(x.button("Pause"), undefined);
  // Running jobs are followed every 2 s rather than the idle 10 s.
  const before = x.reads("/api/downloads");
  await x.tick(2000);
  assert.equal(x.reads("/api/downloads"), before + 1);
});
test("finished downloads are cleared or retried in bulk instead of paused or cancelled", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("a", { state: "done", book_id: "book-a" }),
      job("b", { state: "cancelled" }),
      job("c", { state: "downloading", received: 10, total: 100 }),
    ),
  );
  await x.click("History");
  await x.tick(0);
  assert.equal(x.view.container.querySelectorAll(".download-row").length, 2);
  const selectAll = one(
    x.view.container,
    ".download-tools input[type=checkbox]",
  );
  await act(async () =>
    dispatch(selectAll, "change", { target: { checked: true } }),
  );
  assert.ok(x.button("Retry") && x.button("Clear records"));
  assert.equal(x.button("Pause"), undefined);
  assert.equal(x.button("Cancel"), undefined);
  await x.click("Clear records");
  await x.tick(0);
  assert.equal(
    [...x.fixture.downloadActions]
      .map((item) => `${item.id}:${item.action}`)
      .sort()
      .join(" "),
    "a:delete b:delete",
  );
  assert.equal(x.view.container.querySelectorAll(".download-row").length, 0);
  assert.equal(x.fixture.downloads.length, 1);
  await x.click("Active");
  await x.tick(0);
  assert.ok(one(x.view.container, ".download-title > progress"));
  assert.equal(
    all(x.view.container, "button").filter(
      (node) => attr(node, "aria-label") === "Clear record",
    ).length,
    0,
  );
});
test("the task queue polls every 30 s when idle and every 5 s while work runs", async (t) => {
  const x = await setup(t, () => {}, TaskQueue);
  const reads = () => x.reads("/api/tasks");
  assert.equal(reads(), 1);
  await x.tick(5000);
  assert.equal(reads(), 1);
  x.fixture.downloads.push(job("running", { state: "downloading" }));
  await x.tick(25000);
  assert.equal(reads(), 2);
  await x.tick(5000);
  assert.equal(reads(), 3);
});
test("running cover and thumbnail warm-ups appear in the task popover with its progress", async (t) => {
  const queue = await setup(
    t,
    (f) => {
      f.cache.covers = {
        ...f.cache.covers,
        state: "active",
        done: 2,
        total: 6,
      };
      f.cache.thumbnails = {
        ...f.cache.thumbnails,
        state: "active",
        done: 1,
        total: 6,
      };
    },
    TaskQueue,
  );
  await queue.click("Task queue");
  assert.match(queue.text(), /Rendering covers\s*2\/6/);
  assert.match(queue.text(), /Rendering thumbnails\s*1\/6/);
  const bar = queue.view.container.ownerDocument.querySelector(
    ".task-popover .task-row progress",
  );
  assert.equal(bar.getAttribute("value"), "2");
  assert.equal(bar.getAttribute("max"), "6");
  await queue.click("Rendering thumbnails1/6");
  assert.deepEqual(queue.routes, ["settings/cache"]);
});
test("switching tabs shows placeholders until that tab's own list arrives", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("a", { state: "done", book_id: "book-a" }),
      job("c", { state: "downloading", received: 10, total: 100 }),
    ),
  );
  assert.equal(x.view.container.querySelectorAll(".download-row").length, 1);
  const real = globalThis.fetch;
  let release;
  const held = new Promise((resolve) => (release = resolve));
  globalThis.fetch = async (path, init) => {
    if (String(path).includes("scope=history")) await held;
    return real(path, init);
  };
  t.after(() => (globalThis.fetch = real));
  await x.click("History");
  assert.equal(x.view.container.querySelectorAll(".download-row").length, 0);
  assert.ok(x.view.container.querySelector(".skeleton[aria-busy=true]"));
  assert.doesNotMatch(x.text(), /No downloads/);
  await act(async () => release());
  await x.tick(0);
  assert.equal(x.view.container.querySelector(".skeleton"), null);
  assert.equal(x.view.container.querySelectorAll(".download-row").length, 1);
});
test("adding a downloaded gallery again by its ExHentai link says so and offers the book", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("seven", {
        input_url: "https://e-hentai.org/g/7/def/",
        state: "done",
        book_id: "book-7",
      }),
    ),
  );
  await x.type("https://exhentai.org/g/7/def/");
  await x.submit();
  assert.match(x.text(), /Already downloaded; the book is in the library/);
  assert.doesNotMatch(x.text(), /Added/);
  assert.equal(x.fixture.downloads.length, 1);
  await x.click("Open book");
  assert.deepEqual(x.routes, ["book/book-7"]);
  await x.click("History");
  await x.tick(0);
  await x.click("Download again");
  assert.equal(
    x.fixture.downloadActions.map((item) => item.action).join(),
    "redownload",
  );
});
test("an approval lists the books already in the library and the prepared archive choice", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("prepared", {
        state: "needs_approval",
        details: {
          mode: "reuse",
          reusable: true,
          quote: { original_gp: null, resample_gp: null, balance_gp: null },
          library: [{ id: "book-1", title: "Old copy", relation: "same" }],
        },
      }),
    ),
  );
  assert.match(x.text(), /The library already has this gallery/);
  assert.match(x.text(), /Prepared archive \(quality unknown, 0 GP\)/);
  await x.click("Old copy");
  assert.deepEqual(x.routes, ["book/book-1"]);
  const approve = one(x.view.container, ".download-approval button.primary");
  assert.equal(approve.disabled, false);
  await click(approve);
  assert.equal(
    x.fixture.downloadActions
      .map((item) => `${item.action}:${item.mode}`)
      .join(),
    "approve:reuse",
  );
});
test("an archive that cannot be imported names the problem instead of blaming storage", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("bad", {
        state: "error",
        error_code: "download_invalid_archive",
        details: { import_error: "Page could not be decoded: 002.jpg" },
      }),
    ),
  );
  assert.match(x.text(), /cannot be imported/);
  assert.match(x.text(), /002\.jpg/);
  assert.doesNotMatch(x.text(), /Check storage/);
});
test("a recovered prepared archive replaces the preceding manual quality choice", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("recover", {
        state: "needs_approval",
        details: {
          mode: "org",
          quote: { original_gp: 25, resample_gp: 0, balance_gp: 100 },
        },
      }),
    ),
  );
  await act(async () =>
    dispatch(one(x.view.container, ".download-approval select"), "change", {
      target: { value: "res" },
    }),
  );
  await click(one(x.view.container, ".download-approval button.primary"));
  assert.equal(x.fixture.downloadActions.at(-1).mode, "res");
  x.fixture.downloads[0].state = "needs_approval";
  x.fixture.downloads[0].details = {
    mode: "reuse",
    reusable: true,
    quote: { original_gp: null, resample_gp: null, balance_gp: null },
  };
  await x.tick(10000);
  assert.equal(
    one(x.view.container, ".download-approval select").value,
    "reuse",
  );
  const approve = one(x.view.container, ".download-approval button.primary");
  assert.equal(approve.disabled, false);
  await click(approve);
  assert.equal(x.fixture.downloadActions.at(-1).mode, "reuse");
});
test("batch actions and the selection count cover only the jobs on the current page", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("a", { state: "queued" }),
      job("b", { state: "queued" }),
    ),
  );
  const selectAll = one(
    x.view.container,
    ".download-tools input[type=checkbox]",
  );
  await act(async () =>
    dispatch(selectAll, "change", { target: { checked: true } }),
  );
  assert.match(x.text(), /2 selected/);
  // Job a finishes and leaves the active list: it no longer counts and is not acted on.
  x.fixture.downloads[0].state = "done";
  await x.tick(2000);
  assert.match(x.text(), /1 selected/);
  await x.click("Pause");
  assert.equal(
    x.fixture.downloadActions.map((item) => `${item.id}:${item.action}`).join(),
    "b:pause",
  );
  x.fixture.downloads[1].state = "done";
  await x.tick(10000);
  assert.doesNotMatch(x.text(), /selected/);
  assert.equal(x.button("Resume / retry"), undefined);
});
const answer = (value) =>
  new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
/** The downloads page over a fetch answered by `handler`; returns the view. */
const page = async (t, handler) =>
  (await renderPage(t, Downloads, { fetch: handler })).view;
const quoted = job("job", {
  input_url: "https://e-hentai.org/g/10/abc/",
  source: { site: "ehentai", gallery_id: 10, gallery_token: "abc" },
  revision: 5,
  state: "needs_approval",
  details: { quote: { original_gp: 25, resample_gp: 0, balance_gp: 100 } },
});
test("download confirmation submits once, without a second modal", async (t) => {
  const mutations = [];
  const view = await page(t, async (url, options = {}) => {
    if (options.method === "POST") {
      mutations.push({ url, body: JSON.parse(options.body) });
      return answer({ ...quoted, state: "queued", revision: 6 });
    }
    return answer({
      items: [quoted],
      after: null,
      total: 1,
      page: 1,
      scope: "active",
      through: 1,
    });
  });
  assert.equal(attr(one(view.container, "select"), "value"), "org");
  await act(async () => dispatch(button(view, "Download"), "click"));
  assert.deepEqual(mutations, [
    {
      url: "/api/downloads/job/action?expected_revision=5",
      body: { action: "approve", mode: "org" },
    },
  ]);
  assert.equal(all(view.container, "dialog").length, 0);
});
test("archive mode chosen on the task survives the fee confirmation", async (t) => {
  let mutation;
  const view = await page(t, async (url, options = {}) => {
    if (options.method === "POST") {
      mutation = JSON.parse(options.body);
      return answer({ ...quoted, state: "queued", revision: 6 });
    }
    return answer({
      items: [quoted],
      after: null,
      total: 1,
      page: 1,
      scope: "active",
      through: 1,
    });
  });
  await act(async () =>
    dispatch(one(view.container, "select"), "change", {
      target: { value: "res" },
    }),
  );
  await act(async () => dispatch(button(view, "Download"), "click"));
  assert.equal(all(view.container, "dialog").length, 0);
  assert.equal(mutation.mode, "res");
});
test("history pages do not mutate completed tasks", async (t) => {
  const calls = [];
  const view = await page(t, async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET" });
    if (url.includes("scope=history"))
      return answer({
        items: [
          {
            ...quoted,
            id: url.includes("page=2") ? "older" : "recent",
            state: "done",
          },
        ],
        after: url.includes("page=2") ? null : "opaque/cursor",
        total: 21,
        page: url.includes("page=2") ? 2 : 1,
        scope: "history",
        through: 21,
      });
    return answer({
      items: [quoted],
      after: null,
      total: 1,
      page: 1,
      scope: "active",
      through: 1,
    });
  });
  await act(async () => dispatch(button(view, "History"), "click"));
  await act(async () =>
    dispatch(one(view.container, { "aria-label": "Next page" }), "click"),
  );
  assert.ok(
    calls.some(({ url }) => url.includes("scope=history&limit=20&page=2")),
  );
  assert.equal(all(view.container, { className: "download-row" }).length, 1);
  assert.ok(calls.every((call) => call.method === "GET"));
  assert.equal(all(view.container, "dialog").length, 0);
});
/** Holds the requests `match` picks until `release()`; with `fail` set they then answer 503. */
function holdRequests(t, match) {
  const real = globalThis.fetch;
  const control = { release: () => {}, fail: false, seen: 0 };
  const held = new Promise((resolve) => (control.release = resolve));
  globalThis.fetch = async (path, init) => {
    if (!match(String(path))) return real(path, init);
    control.seen++;
    await held;
    if (control.fail)
      return new Response(JSON.stringify({ error: "storage_unavailable" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      });
    return real(path, init);
  };
  t.after(() => (globalThis.fetch = real));
  return control;
}
const isBatch = (path) => path.includes("/api/downloads/batch");
test("links pasted while a submission waits stay in the box", async (t) => {
  const x = await setup(t);
  const hold = holdRequests(t, isBatch);
  await x.type("https://e-hentai.org/g/1/abc/\nnot a link");
  await x.submit();
  assert.equal(hold.seen, 1);
  await x.type(
    "https://e-hentai.org/g/1/abc/\nnot a link\nhttps://e-hentai.org/g/2/abc/",
  );
  await act(async () => hold.release());
  await x.tick(0);
  // The accepted line leaves; the refused one and the one pasted meanwhile stay.
  assert.equal(
    attr(x.textarea(), "value"),
    "not a link\nhttps://e-hentai.org/g/2/abc/",
  );
  assert.equal(x.fixture.downloads.length, 1);
});
test("a failed submission keeps every line, including ones pasted meanwhile", async (t) => {
  const x = await setup(t);
  const hold = holdRequests(t, isBatch);
  hold.fail = true;
  await x.type("https://e-hentai.org/g/1/abc/");
  await x.submit();
  await x.type("https://e-hentai.org/g/1/abc/\nhttps://e-hentai.org/g/2/abc/");
  await act(async () => hold.release());
  await x.tick(0);
  assert.equal(
    attr(x.textarea(), "value"),
    "https://e-hentai.org/g/1/abc/\nhttps://e-hentai.org/g/2/abc/",
  );
  assert.match(x.text(), /Library storage is unavailable/);
});
test("when a later batch fails, only the lines of the batches that went through leave", async (t) => {
  const x = await setup(t);
  let posts = 0;
  const real = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    if (isBatch(String(path)) && ++posts === 2)
      return new Response(JSON.stringify({ error: "storage_unavailable" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      });
    return real(path, init);
  };
  t.after(() => (globalThis.fetch = real));
  const links = Array.from(
    { length: 101 },
    (_, n) => `https://e-hentai.org/g/${n + 1}/abc/`,
  );
  await x.type(links.join("\n"));
  await x.submit();
  assert.equal(posts, 2);
  assert.equal(attr(x.textarea(), "value"), links[100]);
  assert.equal(x.fixture.downloads.length, 100);
});
test("a finished job cleared on another device does not stop the list from refreshing", async (t) => {
  const x = await setup(t);
  await x.type("https://e-hentai.org/g/1/abc/");
  await x.submit();
  // Finished, the job leaves the active list.
  x.fixture.downloads[0].state = "done";
  await x.tick(10000);
  assert.equal(all(x.view.container, { className: "download-row" }).length, 0);
  x.fixture.downloads.splice(0, 1);
  x.fixture.downloads.push(job("arrived", { state: "paused" }));
  await x.tick(10000);
  assert.doesNotMatch(x.text(), /Could not refresh/);
  assert.equal(all(x.view.container, { className: "download-row" }).length, 1);
  assert.match(x.text(), /Paused/);
});
test("clearing a finished job keeps the history list refreshing", async (t) => {
  const x = await setup(t);
  await x.type("https://e-hentai.org/g/1/abc/");
  await x.submit();
  x.fixture.downloads[0].state = "done";
  await x.tick(10000);
  await x.click("History");
  const pinnedReads = x.reads("/api/downloads/fixture-job-1");
  await x.click("Clear record");
  x.fixture.downloads.push(job("arrived", { state: "done" }));
  await x.tick(60000);
  assert.doesNotMatch(x.text(), /Could not refresh/);
  assert.equal(x.reads("/api/downloads/fixture-job-1"), pinnedReads);
  assert.match(x.text(), /In library/);
});
test("a failed download is cleared in one step", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("failed", { state: "error", error_code: "source_unavailable" }),
    ),
  );
  await x.click("Clear record");
  assert.equal(x.fixture.downloads.length, 0);
  assert.equal(
    x.fixture.downloadActions.map((item) => item.action).join(),
    "delete",
  );
});
test("a stopped task can be retried even when its old retry time is in the future", async (t) => {
  const later = Math.floor(Date.now() / 1000) + 600;
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("failed", {
        state: "error",
        error_code: "source_unavailable",
        retry_at: later,
      }),
    ),
  );
  const resume = x.button("Resume / retry");
  assert.equal(resume.disabled, false);
  await x.click("Resume / retry");
  assert.equal(x.fixture.downloadActions.at(-1).action, "resume");
});
test("adding a gallery that waits in the list says how it waits instead of 'queued'", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("paused", {
        input_url: "https://e-hentai.org/g/7/def/",
        state: "paused",
      }),
    ),
  );
  await x.type("https://e-hentai.org/g/7/def/");
  await x.submit();
  assert.match(x.text(), /Already in the list \(Paused\); continue it below/);
  assert.doesNotMatch(x.text(), /Already queued/);
});
test("the download list uses the shared task summary, not a separate source budget poll", async (t) => {
  const x = await setup(t, (f) => f.downloads.push(job("a")));
  assert.equal(x.reads("/api/sources/settings"), 0);
  assert.equal(x.reads("/api/sources/budget"), 0);
  assert.ok(x.reads("/api/tasks") >= 1);
});

test("selecting the entire queue confirms a snapshot across pages with the GP total", async (t) => {
  const x = await setup(t, (f) => {
    for (let i = 0; i < 125; i++)
      f.downloads.push(
        job(`item-${i}`, {
          state: "needs_approval",
          details: {
            quote: { original_gp: 25, resample_gp: 0, balance_gp: 20 },
          },
        }),
      );
  });
  assert.equal(x.view.container.querySelectorAll(".download-row").length, 20);
  await x.click("Select all 125 tasks");
  assert.match(x.text(), /125 selected/);
  assert.ok(x.button("Confirm 125 downloads · 3,125 GP"));
  assert.match(x.text(), /Not enough GP. You can still confirm/);
  await click(one(x.view.container, '[aria-label="Next page"]'));
  assert.match(x.text(), /125 selected/);
  x.fixture.downloads.push(
    job("new-after-selection", {
      state: "needs_approval",
      details: { quote: { original_gp: 99 } },
    }),
  );
  await x.click("Confirm 125 downloads · 3,125 GP");
  assert.equal(x.fixture.downloadActions.length, 0);
  await x.click("Confirm download");
  assert.equal(x.fixture.downloadActions.length, 125);
  assert.equal(
    new Set([...x.fixture.downloadActions].map((item) => item.id)).size,
    125,
  );
  assert.ok(
    x.fixture.downloadActions.every((item) => item.action === "approve"),
  );
  assert.equal(x.fixture.downloads.at(-1).state, "needs_approval");
});

test("bulk approval sums the chosen quality and skips unknown prices and other states", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("paid", {
        state: "needs_approval",
        details: { quote: { original_gp: 25, resample_gp: 5 } },
      }),
      job("free", {
        state: "needs_approval",
        details: { mode: "reuse", reusable: true },
      }),
      job("unknown", { state: "needs_approval" }),
      job("running", { state: "downloading" }),
    ),
  );
  const paid = one(x.view.container, "#download-paid");
  await act(async () =>
    dispatch(paid.querySelector("select"), "change", {
      target: { value: "res" },
    }),
  );
  await act(async () =>
    dispatch(one(x.view.container, ".download-tools input"), "change", {
      target: { checked: true },
    }),
  );
  assert.ok(x.button("Confirm 2 downloads · 5 GP"));
  assert.match(x.text(), /Tasks with unknown prices are excluded/);
  await x.click("Confirm 2 downloads · 5 GP");
  await x.click("Confirm download");
  assert.deepEqual(
    Array.from(x.fixture.downloadActions, (item) => [
      item.id,
      item.mode,
    ]).sort(),
    [
      ["free", "reuse"],
      ["paid", "res"],
    ],
  );
});

test("a failed queue selection does not select a partial list", async (t) => {
  const x = await setup(t, (f) => {
    for (let i = 0; i < 125; i++) f.downloads.push(job(`item-${i}`));
  });
  const fetch = globalThis.fetch;
  t.mock.method(globalThis, "fetch", (url, options) =>
    String(url).includes("limit=100&after=")
      ? Promise.resolve(new Response("{}", { status: 503 }))
      : fetch(url, options),
  );
  await x.click("Select all 125 tasks");
  assert.doesNotMatch(x.text(), /125 selected|Selected across pages/);
  assert.ok(x.view.container.querySelector('[role="alert"]'));
});

test("GP exhaustion is an explicit automatic wait in the page and task popover", async (t) => {
  const waiting = job("empty", {
    error_code: "insufficient_gp",
    retry_at: Math.floor(Date.now() / 1000) + 60,
  });
  const x = await setup(t, (f) => f.downloads.push(waiting));
  assert.match(x.text(), /Waiting for GP/);
  assert.match(x.text(), /your approval is saved/);
  await x.unmount();
  const queue = await renderPage(
    t,
    (props) =>
      React.createElement(
        TaskProvider,
        { enabled: true },
        React.createElement(TaskQueue, props),
      ),
    {
      prepare: (f) => f.downloads.push(waiting),
    },
  );
  await queue.click("Task queue");
  assert.match(queue.text(), /Waiting for GP/);
  await click(queue.view.container.ownerDocument.querySelector(".task-row"));
  assert.deepEqual(queue.routes, ["downloads/empty"]);
});

test("a deep link locates and focuses a job outside the current page, including finished jobs", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const x = await renderPage(t, Downloads, {
    props: { focusId: "target" },
    prepare: (f) => {
      f.downloads.push(job("target", { state: "done" }));
      for (let i = 0; i < 25; i++) f.downloads.push(job(`item-${i}`));
    },
  });
  const target = one(x.view.container, "#download-target");
  assert.equal(target.getAttribute("aria-current"), "true");
  assert.equal(document.activeElement, target);
});

test("approval dialog freezes revisions and keeps failed tasks selected", async (t) => {
  const x = await setup(t, (f) => {
    for (let i = 0; i < 20; i++)
      f.downloads.push(
        job(`frozen-${i}`, {
          state: "needs_approval",
          details: { quote: { original_gp: 25, resample_gp: 0 } },
        }),
      );
  });
  await act(async () =>
    dispatch(one(x.view.container, ".download-tools input"), "change", {
      target: { checked: true },
    }),
  );
  await x.click("Confirm 20 downloads · 500 GP");
  x.fixture.downloads.push(
    job("late", {
      state: "needs_approval",
      details: { quote: { original_gp: 100 } },
    }),
  );
  x.fixture.downloads[0].revision++;
  x.fixture.downloads[0].details.quote.original_gp = 50;
  await x.tick(10000);
  assert.match(x.text(), /Total: 500 GP/);
  await x.click("Confirm download");
  assert.equal(x.fixture.downloadActions.length, 20);
  assert.match(x.text(), /19 completed, 1 failed/);
  assert.equal(x.fixture.downloads[0].state, "needs_approval");
  assert.equal(x.fixture.downloads.at(-1).state, "needs_approval");
  assert.match(x.text(), /1 selected/);
});

test("last-page actions keep the page, clear only selected failures and clamp an empty tail", async (t) => {
  const x = await setup(t, (f) => {
    for (let i = 0; i < 41; i++)
      f.downloads.push(job(`stable-${i}`, { state: "error" }));
  });
  await x.click("Last page");
  assert.equal(all(x.view.container, ".download-row").length, 1);
  assert.ok(one(x.view.container, "#download-stable-0"));
  await x.click("Resume / retry");
  assert.ok(one(x.view.container, "#download-stable-0"));
  assert.equal(
    x.fixture.requests
      .filter((r) => r.path === "/api/downloads")
      .at(-1)
      .query.get("page"),
    "3",
  );
  x.fixture.downloads[0].state = "error";
  await x.tick(10000);
  await act(async () =>
    dispatch(one(x.view.container, ".download-tools input"), "change", {
      target: { checked: true },
    }),
  );
  await x.click("Clear failed tasks");
  assert.equal(x.fixture.downloads.length, 40);
  assert.equal(all(x.view.container, ".download-row").length, 20);
  assert.equal(
    x.fixture.requests
      .filter((r) => r.path === "/api/downloads")
      .at(-1)
      .query.get("page"),
    "2",
  );
});

test("a located old task is not duplicated on the first page", async (t) => {
  const x = await renderPage(t, Downloads, {
    props: { focusId: "target-old" },
    prepare(f) {
      f.downloads.push(job("target-old", { state: "error" }));
      for (let i = 0; i < 40; i++) f.downloads.push(job(`new-${i}`));
    },
  });
  assert.equal(all(x.view.container, ".download-row").length, 1);
  assert.equal(document.activeElement.id, "download-target-old");
  await x.click("First page");
  assert.equal(x.view.container.querySelector("#download-target-old"), null);
  assert.equal(all(x.view.container, ".download-row").length, 20);
});

test("cancelled downloads expose explicit retry and keep resume unavailable", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(job("cancelled", { state: "cancelled" })),
  );
  await x.click("History");
  await x.tick(0);
  assert.equal(x.button("Resume / retry"), undefined);
  await x.click("Retry");
  assert.equal(x.fixture.downloadActions.at(-1).action, "redownload");
});

test("batch actions keep ineligible selected jobs and disable unavailable actions", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("paused", { state: "paused" }),
      job("approval", {
        state: "needs_approval",
        details: { quote: { original_gp: 25 } },
      }),
    ),
  );
  await act(async () =>
    dispatch(
      one(x.view.container, ".download-tools input[type=checkbox]"),
      "change",
      { target: { checked: true } },
    ),
  );
  assert.equal(x.button("Pause").disabled, true);
  await x.click("Resume / retry");
  assert.deepEqual(
    [...x.fixture.downloadActions].map((item) => item.id),
    ["paused"],
  );
  assert.match(x.text(), /1 selected/);
  assert.match(x.text(), /Confirm 1 downloads/);
  assert.equal(x.button("Resume / retry").disabled, true);
});

test("a failed bulk approval closes the dialog so its error is visible", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("approval", {
        state: "needs_approval",
        details: { quote: { original_gp: 25 } },
      }),
    ),
  );
  await act(async () =>
    dispatch(
      one(x.view.container, ".download-tools input[type=checkbox]"),
      "change",
      { target: { checked: true } },
    ),
  );
  await x.click("Confirm 1 downloads · 25 GP");
  const fetch = globalThis.fetch;
  t.mock.method(globalThis, "fetch", (url, options) =>
    String(url).includes("/api/downloads/actions")
      ? Promise.reject(new TypeError("Failed to fetch"))
      : fetch(url, options),
  );
  await x.click("Confirm download");
  assert.equal(document.querySelector('[role="dialog"]'), null);
  assert.ok(x.view.container.querySelector('[role="alert"]'));
  assert.match(x.text(), /1 selected/);
});

test("a poll started before deletion cannot restore a cleared row when refresh fails", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(job("failed", { state: "error" })),
  );
  const fetch = globalThis.fetch;
  let release,
    held = false,
    failNext = false;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (
      String(url).includes("/api/downloads?") &&
      (!options?.method || options.method === "GET")
    ) {
      if (failNext) throw new TypeError("Failed to fetch");
      const response = await fetch(url, options);
      if (!held) {
        held = true;
        return new Promise((resolve) => {
          release = () => resolve(response);
        });
      }
      return response;
    }
    return fetch(url, options);
  });
  await x.tick(10000);
  assert.ok(release);
  await x.click("Clear record");
  assert.equal(x.fixture.downloads.length, 0);
  failNext = true;
  await act(async () => {
    release();
    await new Promise((resolve) => setImmediate(resolve));
  });
  assert.equal(x.view.container.querySelectorAll(".download-row").length, 0);
});

test("lost source keys show a settings link on downloads and in the task queue", async (t) => {
  function Both(props) {
    return React.createElement(
      React.Fragment,
      null,
      React.createElement(Downloads, props),
      React.createElement(TaskQueue, props),
    );
  }
  const x = await setup(
    t,
    (f) => {
      f.sourceBlocked = "source_key_lost";
      f.downloads.push(job("queued"));
    },
    Both,
  );
  assert.match(x.text(), /credential key is missing/);
  assert.ok(x.view.container.querySelector('a[href="#settings"]'));
  await x.click("Task queue");
  assert.match(document.body.textContent, /credential key is missing/);
  assert.equal(x.view.container.querySelector(".task-trigger .spinner"), null);
});

test("adding a cancelled gallery points to Retry under History", async (t) => {
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("cancelled", {
        input_url: "https://e-hentai.org/g/7/def/",
        state: "cancelled",
      }),
    ),
  );
  await x.type("https://e-hentai.org/g/7/def/");
  await x.submit();
  assert.match(
    x.text(),
    /Already in the list \(Cancelled\); use Retry under History to start it again/,
  );
  assert.doesNotMatch(x.text(), /continue it below/);
});

test("a pasted link of another site says which links to paste", async (t) => {
  const x = await setup(t);
  await x.type("https://example.org/g/200/");
  await x.submit();
  assert.match(
    x.text(),
    /Unsupported site\. Paste an E-Hentai or ExHentai link\./,
  );
});

test("uncounted waits keep the retry time but drop the stalled attempt number", async (t) => {
  const later = Math.floor(Date.now() / 1000) + 600;
  const x = await setup(t, (f) =>
    f.downloads.push(
      job("preparing", {
        retry_at: later,
        attempts: 2,
        error_code: "archive_preparing",
      }),
      job("busy", {
        input_url: "https://e-hentai.org/g/22/abc/",
        retry_at: later,
        attempts: 1,
        error_code: "database_busy",
      }),
      job("counted", {
        input_url: "https://e-hentai.org/g/333/abc/",
        retry_at: later,
        attempts: 1,
        error_code: "source_network",
      }),
    ),
  );
  const text = x.text();
  assert.equal((text.match(/Retrying automatically at/g) ?? []).length, 2);
  assert.equal((text.match(/Retrying at .+?, attempt 2/g) ?? []).length, 1);
  assert.doesNotMatch(text, /attempt 3/);
});
