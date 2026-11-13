import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { Pages } from "../src/pages.ts";
import { useWarmPages } from "../src/preload.ts";
async function setup(t, options = {}) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const requests = [];
  let failure = options.failure ?? -1;
  t.mock.method(globalThis, "fetch", async (path, init) => {
    const page = Number(path.split("/").at(-1));
    requests.push({ page, priority: init.priority });
    return new Response("image", {
      status: page === failure ? (options.status ?? 500) : 200,
    });
  });
  const pages = new Pages("book", options.budget);
  let progress,
    view,
    props = {
      pages,
      total: options.total ?? 24,
      start: 0,
      enabled: true,
    };
  function Harness({ pages, total, start, enabled }) {
    progress = useWarmPages(pages, total, start, enabled);
    return null;
  }
  await act(async () => {
    view = render(React.createElement(Harness, props));
  });
  t.after(async () => {
    await act(async () => view.unmount());
    pages.dispose();
  });
  return {
    pages,
    requests,
    progress: () => progress,
    recover: () => {
      failure = -1;
    },
    tick: () => act(async () => t.mock.timers.tick(50)),
    update: (change) =>
      act(async () => {
        props = { ...props, ...change };
        view.rerender(React.createElement(Harness, props));
      }),
    retry: () => act(async () => progress.retry()),
    unmount: () => act(async () => view.unmount()),
  };
}
const range = (from, to) =>
  Array.from({ length: to - from + 1 }, (_, index) => from + index);
test("reader warms at most thirty pages ahead; memory keeps only the window", async (t) => {
  const x = await setup(t, { total: 150 });
  for (let n = 0; n < 200; n++) await x.tick();
  assert.deepEqual(
    x.requests.map((r) => r.page),
    range(0, 30),
  );
  assert.equal(x.progress().done, 31);
  assert.ok(x.requests.every((r) => r.priority === "low"));
  assert.ok(range(0, 5).every((page) => x.pages.has(page)));
  assert.equal(x.pages.has(6), false);
  await x.update({ start: 17 });
  for (let n = 0; n < 40; n++) await x.tick();
  // Every page is downloaded (into the HTTP cache); memory holds the five each side.
  assert.ok(range(0, 47).every((page) => x.pages.downloaded(page)));
  assert.ok(range(12, 22).every((page) => x.pages.has(page)));
  assert.equal(x.pages.has(11), false);
  assert.equal(x.pages.has(23), false);
});
test("far jump prioritizes the destination and nearby pages without scanning the rest of the book", async (t) => {
  const x = await setup(t, { total: 12 });
  await x.update({ start: 10 });
  for (let n = 0; n < 14; n++) await x.tick();
  assert.deepEqual(
    x.requests.map((r) => r.page),
    [0, 10, 11, 9, 8, 7, 6, 5],
  );
  assert.equal(x.progress().done, 8);
});
test("background pause and unmount stop the remaining plan without marking cancellations failed", async (t) => {
  const x = await setup(t);
  await x.update({ enabled: false });
  for (let n = 0; n < 20; n++) await x.tick();
  assert.equal(x.requests.length, 1);
  assert.equal(x.progress().paused, true);
  assert.equal(x.progress().failed, 0);
  await x.update({ enabled: true });
  assert.equal(x.requests.length, 2);
  await x.unmount();
  for (let n = 0; n < 20; n++) await x.tick();
  assert.equal(x.requests.length, 2);
});
test("failed preload is counted once, does not spin, and explicit retry fetches only failures", async (t) => {
  const x = await setup(t, { total: 8, failure: 2 });
  for (let n = 0; n < 20; n++) await x.tick();
  assert.equal(x.progress().done, 7);
  assert.equal(x.progress().failed, 1);
  assert.equal(x.progress().waiting, 0);
  assert.equal(x.requests.length, 8);
  x.recover();
  await x.retry();
  assert.equal(x.progress().done, 8);
  assert.equal(x.progress().failed, 0);
  assert.deepEqual(
    x.requests.map((r) => r.page),
    [0, 1, 2, 3, 4, 5, 6, 7, 2],
  );
});
test("unavailable storage halts preload and foreground success resumes it", async (t) => {
  const x = await setup(t, { total: 2000, failure: 0, status: 503 });
  for (let n = 0; n < 20; n++) await x.tick();
  assert.equal(x.requests.length, 1);
  assert.equal(x.progress().paused, true);
  assert.equal(x.progress().failed, 1);
  assert.equal(x.progress().waiting, 1999);
  await x.update({ start: 1000, enabled: false });
  await x.update({ enabled: true });
  for (let n = 0; n < 5; n++) await x.tick();
  assert.equal(x.requests.length, 1);
  assert.equal(x.progress().paused, true);
  x.recover();
  await act(async () => x.pages.get(0, new AbortController().signal));
  assert.equal(x.progress().failed, 0);
  assert.equal(x.progress().paused, false);
  assert.equal(x.progress().done, 2);
  assert.equal(x.requests.length, 3);
});
test("switching books cancels an in-flight warmup and late bytes never feed the new book", async (t) => {
  const calls = [];
  t.mock.method(
    globalThis,
    "fetch",
    (path, options) =>
      new Promise((resolve) => calls.push({ path, options, resolve })),
  );
  const first = new Pages("first"),
    second = new Pages("second");
  let progress;
  function Harness({ pages }) {
    progress = useWarmPages(pages, 6);
    return null;
  }
  let view;
  await act(async () => {
    view = render(React.createElement(Harness, { pages: first }));
  });
  await act(async () => {
    view.rerender(React.createElement(Harness, { pages: second }));
  });
  first.dispose();
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(calls[1].options.signal.aborted, false);
  await act(async () => calls[0].resolve(new Response("old book")));
  assert.equal(first.has(0), false);
  assert.equal(progress.done, 0);
  assert.equal(calls.length, 2);
  await act(async () => view.unmount());
  first.dispose();
  second.dispose();
  calls[1].resolve(new Response("late second"));
});
test("pages already read leave memory but stay downloaded; returning reloads only the window", async (t) => {
  const x = await setup(t, { total: 200 });
  for (let n = 0; n < 250; n++) await x.tick();
  await x.update({ start: 140 });
  for (let n = 0; n < 50; n++) await x.tick();
  assert.equal(x.pages.has(1), false);
  assert.equal(x.pages.downloaded(1), true);
  const before = x.requests.length;
  await x.update({ start: 0 });
  for (let n = 0; n < 130; n++) await x.tick();
  // In a browser these come from the HTTP cache.
  assert.deepEqual(
    x.requests.slice(before).map((r) => r.page),
    range(0, 5),
  );
});
test("a cached page keeps one object URL until it leaves the memory window", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("image"));
  const revoked = [];
  t.mock.method(URL, "createObjectURL", () => `blob:${Math.random()}`);
  t.mock.method(URL, "revokeObjectURL", (url) => revoked.push(url));
  const pages = new Pages("book");
  t.after(() => pages.dispose());
  assert.equal(pages.url(0), undefined);
  await pages.get(0, new AbortController().signal);
  const url = pages.url(0);
  assert.ok(url);
  assert.equal(pages.url(0), url);
  pages.focus(5);
  assert.deepEqual(revoked, []);
  assert.equal(pages.url(0), url);
  pages.focus(6);
  assert.deepEqual(revoked, [url]);
  assert.equal(pages.url(0), undefined);
});
test("once memory is full, warming continues into the HTTP cache instead of stopping", async (t) => {
  // Each page is 5 bytes; a 12-byte budget is "warm enough" after two pages.
  const x = await setup(t, { total: 10, budget: 12 });
  for (let i = 0; i < 20; i++) await x.tick();
  assert.equal(x.progress().done, 10);
  assert.equal(x.pages.bufferedAhead(0, 10), 10);
  assert.ok(!x.pages.has(9));
  assert.deepEqual(
    x.requests.map((request) => request.page),
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
  );
});

test("503 warmup resumes automatically after ten seconds", async (t) => {
  const x = await setup(t, { status: 503, failure: 0 });
  assert.equal(x.progress().paused, true);
  x.recover();
  await act(async () => t.mock.timers.tick(9999));
  assert.equal(x.requests.length, 1);
  await act(async () => t.mock.timers.tick(1));
  assert.equal(x.progress().paused, false);
  assert.equal(x.requests.length, 2);
});

test("save-data connections preload only three pages ahead", async (t) => {
  Object.defineProperty(navigator, "connection", {
    value: { saveData: true },
    configurable: true,
  });
  t.after(() => delete navigator.connection);
  const x = await setup(t, { total: 200 });
  for (let n = 0; n < 20; n++) await x.tick();
  assert.deepEqual(
    x.requests.map((r) => r.page),
    [0, 1, 2, 3],
  );
});

test("pausing or retrying at the same page preserves the 200 MiB preload allowance", async (t) => {
  const { PageLimitError } = await import("../src/api.ts");
  const x = await setup(t, { total: 50 });
  const limits = [];
  t.mock.method(
    x.pages,
    "get",
    async (_page, _signal, _background, _store, limit) => {
      limits.push(limit);
      if (limit < 80 * 1024 ** 2) throw new PageLimitError(limit);
      return { size: 80 * 1024 ** 2 };
    },
  );
  for (let n = 0; n < 5; n++) await x.tick();
  assert.equal(limits.length, 3);
  assert.equal(limits.at(-1), 40 * 1024 ** 2 - 5);
  await x.update({ enabled: false });
  await x.update({ enabled: true });
  assert.equal(limits.at(-1), 40 * 1024 ** 2 - 5);
});
