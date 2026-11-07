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
  const pages = new Pages("book");
  let progress,
    view,
    props = {
      pages,
      total: options.total ?? 24,
      reading: options.reading ?? true,
      start: 0,
      enabled: true,
    };
  function Harness({ pages, total, start, reading, enabled }) {
    progress = useWarmPages(pages, total, start, reading, enabled);
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
test("detail only requests its first six pages and does not start a full-book scan", async (t) => {
  const x = await setup(t, { total: 6, reading: false });
  assert.deepEqual(
    x.requests.map((r) => r.page),
    [0, 1, 2, 3, 4, 5],
  );
  assert.equal(x.progress().done, 6);
  for (let n = 0; n < 10; n++) await x.tick();
  assert.equal(x.requests.length, 6);
});
test("reader warms a bounded neighbourhood and retains previous pages for immediate reverse navigation", async (t) => {
  const x = await setup(t, { total: 2000 });
  for (let n = 0; n < 20; n++) await x.tick();
  assert.deepEqual(
    x.requests.map((r) => r.page),
    [0, 1, 2, 3, 4, 5, 6, 7, 8],
  );
  assert.equal(x.progress().done, 9);
  assert.ok(x.requests.every((r) => r.priority === "low"));
  await x.update({ start: 17 });
  for (let n = 0; n < 20; n++) await x.tick();
  assert.equal(x.progress().done, 19);
  assert.ok([14, 15, 16, 17, 18, 19, 20].every((page) => x.pages.has(page)));
  const before = x.requests.length;
  await x.pages.get(16, new AbortController().signal);
  assert.equal(x.requests.length, before);
});
test("far jump prioritizes the destination and nearby pages without scanning the rest of the book", async (t) => {
  const x = await setup(t, { total: 12 });
  await x.update({ start: 10 });
  for (let n = 0; n < 14; n++) await x.tick();
  assert.deepEqual(
    x.requests.map((r) => r.page),
    [0, 10, 11, 9, 8, 7],
  );
  assert.equal(x.progress().done, 6);
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
test("unavailable storage halts the remaining book until an explicit retry", async (t) => {
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
  assert.equal(x.progress().paused, true);
  await x.retry();
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
test("pages evicted from the cache are prefetched again when the reader returns to them", async (t) => {
  const x = await setup(t, { total: 2000 });
  for (let n = 0; n < 20; n++) await x.tick();
  await x.update({ start: 40 });
  for (let n = 0; n < 20; n++) await x.tick();
  assert.equal(x.pages.has(1), false);
  assert.equal(x.pages.state(1), "done");
  const before = x.requests.length;
  await x.update({ start: 0 });
  for (let n = 0; n < 20; n++) await x.tick();
  assert.deepEqual(
    x.requests.slice(before).map((r) => r.page),
    [0, 1, 2, 3, 4, 5, 6, 7, 8],
  );
  assert.ok([0, 1, 2, 3].every((page) => x.pages.has(page)));
});
