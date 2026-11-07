import { one, dispatch, attr, all } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { Downloads } from "../src/Downloads.tsx";
import { TaskQueue } from "../src/TaskQueue.tsx";
import { button, click, setupFixture, tick, visibleText } from "./harness.mjs";
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
  const document = globalThis.document;
  document.hidden = false;
  const x = await setupFixture(t, prepare, { globals: { document } });
  const view = await x.render(
    React.createElement(Component, { t: (_, en) => en, navigate: () => {} }),
  );
  const textarea = () => one(view.container, "textarea");
  return {
    fixture: x.fixture,
    view,
    text: () => visibleText(view),
    button: (text) => button(view, text),
    click: (text) => click(button(view, text)),
    tick: (ms) => tick(t, ms),
    reads: (path) =>
      x.fixture.requests.filter((r) => r.path === path && r.method === "GET")
        .length,
    textarea,
    submit: () =>
      act(async () =>
        dispatch(one(view.container, "form"), "submit", {
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
      "https://nhentai.net/g/200/",
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
  assert.match(text, /https:\/\/nhentai\.net\/g\/200\/Added/);
  assert.match(text, /not a linkNot a recognized gallery link/);
  assert.match(text, /example\.com\/g\/5\/Unsupported site/);
  assert.equal((text.match(/Already queued/g) ?? []).length, 2);
  // Only the lines that need correcting stay in the box.
  assert.equal(
    attr(x.textarea(), "value"),
    "not a link\nhttps://example.com/g/5/",
  );
  assert.equal(x.fixture.downloads.length, 3);
  assert.equal(all(x.view.container, { className: "download-row" }).length, 3);
});
test("the nHentai entry choice appears only for nHentai links and is sent with the batch", async (t) => {
  const x = await setup(t);
  await x.type("https://e-hentai.org/g/1/abc/");
  assert.equal(x.button("Download directly"), undefined);
  assert.ok(x.button("Add"));
  await x.type("https://nhentai.net/g/200/");
  assert.ok(x.button("Continue"));
  await x.click("Download directly");
  await x.submit();
  assert.equal(x.fixture.downloads[0].details.entry, "nh_direct");
  assert.equal(attr(x.textarea(), "value"), "");
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
    f.sourceCooldowns = { ehentai: later, nhentai: 1 };
  };
  const x = await setup(t, prepare);
  const text = x.text();
  assert.match(text, /Retrying at .+, attempt 3/);
  assert.match(text, /Source connection failed/);
  assert.match(
    text,
    /E-Hentai rate-limited until .+; queue resumes automatically/,
  );
  assert.doesNotMatch(text, /nHentai rate-limited/);
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
  x.fixture.downloads[0].state = "needs_choice";
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
  assert.equal(x.fixture.downloads[0].state, "paused");
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
