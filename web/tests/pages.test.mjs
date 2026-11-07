import test from "node:test";
import assert from "node:assert/strict";
import {
  EmptyPageError,
  Pages,
  coverUrl,
  thumbnailPath,
} from "../src/pages.ts";

test("page cache respects both byte budget and count, and disposal cancels cached reads", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls++;
    return new Response(new Blob(["12345"]));
  });
  const cache = new Pages("book", 10, 2),
    signal = new AbortController().signal;
  await cache.get(0, signal);
  await cache.get(1, signal);
  await cache.get(0, signal);
  assert.equal(calls, 2);
  await cache.get(2, signal);
  assert.equal(cache.has(1), false);
  assert.equal(cache.has(0), true);
  cache.dispose();
  assert.equal(cache.has(0), false);
  await assert.rejects(cache.get(0, signal), { name: "AbortError" });
});
test("401 locks the UI, failed pages never enter cache", async (t) => {
  const window = new EventTarget();
  let locked = 0;
  window.addEventListener("mc-locked", () => locked++);
  globalThis.window = window;
  t.after(() => {
    delete globalThis.window;
  });
  t.mock.method(
    globalThis,
    "fetch",
    async () => new Response("", { status: 401 }),
  );
  const cache = new Pages("book");
  await assert.rejects(cache.get(0, new AbortController().signal), {
    status: 401,
  });
  assert.equal(locked, 1);
  assert.equal(cache.has(0), false);
});

const flush = async () => {
  for (let i = 0; i < 30; i++) await Promise.resolve();
};
function pendingFetch(t) {
  const calls = [];
  t.mock.method(
    globalThis,
    "fetch",
    (path, options) =>
      new Promise((resolve, reject) => {
        const call = {
          page: Number(path.split("/").at(-1)),
          options,
          resolve,
          reject,
        };
        calls.push(call);
        options.signal.addEventListener(
          "abort",
          () => reject(options.signal.reason),
          { once: true },
        );
      }),
  );
  return calls;
}

test("same-page display and warmup share a fetch; canceling one subscriber preserves the other", async (t) => {
  const calls = pendingFetch(t),
    pages = new Pages("book");
  const warm = new AbortController(),
    display = new AbortController();
  const a = pages.get(2, warm.signal, true),
    b = pages.get(2, display.signal);
  const cancelled = assert.rejects(a, { name: "AbortError" });
  warm.abort();
  await cancelled;
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.signal.aborted, false);
  calls[0].resolve(new Response("page two"));
  assert.equal(await (await b).text(), "page two");
  assert.equal(pages.progress(4).done, 1);
  pages.dispose();
});

test("foreground gets the reserved slot and overtakes queued background work with two requests maximum", async (t) => {
  const calls = pendingFetch(t),
    pages = new Pages("book"),
    signal = new AbortController().signal;
  const requests = [
    pages.get(0, signal, true),
    pages.get(1, signal, true),
    pages.get(2, signal),
    pages.get(3, signal),
  ];
  assert.deepEqual(
    calls.map((c) => c.page),
    [0, 2],
  );
  assert.equal(calls[0].options.priority, "low");
  assert.equal(calls[1].options.priority, "high");
  calls[1].resolve(new Response("two"));
  await requests[2];
  assert.deepEqual(
    calls.map((c) => c.page),
    [0, 2, 3],
  );
  calls[2].resolve(new Response("three"));
  await requests[3];
  assert.equal(calls.length, 3);
  calls[0].resolve(new Response("zero"));
  await requests[0];
  assert.deepEqual(
    calls.map((c) => c.page),
    [0, 2, 3, 1],
  );
  calls[3].resolve(new Response("one"));
  await requests[1];
  pages.dispose();
});

test("an abandoned request has a short grace period and an immediate reversal reuses the stream", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls = pendingFetch(t),
    pages = new Pages("book"),
    controller = new AbortController();
  const cancelled = assert.rejects(pages.get(3, controller.signal), {
    name: "AbortError",
  });
  controller.abort();
  await cancelled;
  t.mock.timers.tick(400);
  assert.equal(calls[0].options.signal.aborted, false);
  const retry = pages.get(3, new AbortController().signal);
  t.mock.timers.tick(200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.signal.aborted, false);
  calls[0].resolve(new Response("reused"));
  assert.equal(await (await retry).text(), "reused");
  pages.dispose();
});

test("dispose rejects active and queued reads and ignores a transport's late response", async (t) => {
  const calls = [];
  t.mock.method(
    globalThis,
    "fetch",
    (path, options) =>
      new Promise((resolve) => calls.push({ resolve, options })),
  );
  const pages = new Pages("book"),
    signal = new AbortController().signal;
  const cancelled = [
    pages.get(0, signal),
    pages.get(1, signal),
    pages.get(2, signal),
  ].map((p) => assert.rejects(p, { name: "AbortError" }));
  pages.dispose();
  await Promise.all(cancelled);
  calls.forEach((c) => c.resolve(new Response("late")));
  await flush();
  assert.equal(calls.length, 2);
  assert.ok(calls.every((c) => c.options.signal.aborted));
  assert.equal(pages.has(0), false);
  assert.deepEqual(pages.progress(3), {
    done: 0,
    failed: 0,
    total: 3,
    waiting: 3,
  });
});

test("oversized Content-Length cancels the body before reading and reports a page-specific limit", async (t) => {
  let cancelled = 0;
  t.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(
        new ReadableStream({
          cancel() {
            cancelled++;
          },
        }),
        { headers: { "Content-Length": "101" } },
      ),
  );
  const pages = new Pages("book", 40, 2, 100);
  await assert.rejects(pages.get(0, new AbortController().signal), {
    bytes: 100,
  });
  assert.equal(cancelled, 1);
  assert.deepEqual(pages.progress(1), {
    done: 0,
    failed: 1,
    total: 1,
    waiting: 0,
  });
  pages.dispose();
});

test("missing or false Content-Length cannot bypass the incremental response byte cap", async (t) => {
  for (const headers of [{}, { "Content-Length": "1" }]) {
    let cancelled = 0;
    t.mock.method(
      globalThis,
      "fetch",
      async () =>
        new Response(
          new ReadableStream({
            pull(controller) {
              controller.enqueue(new Uint8Array(6));
            },
            cancel() {
              cancelled++;
            },
          }),
          { headers },
        ),
    );
    const pages = new Pages("book", 20, 2, 10);
    await assert.rejects(pages.get(0, new AbortController().signal), {
      bytes: 10,
    });
    assert.equal(cancelled, 1);
    assert.equal(pages.has(0), false);
    pages.dispose();
  }
});

test("an unused stalled stream aborts after its grace period", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let cancelled = 0;
  t.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(
        new ReadableStream({
          cancel() {
            cancelled++;
          },
        }),
      ),
  );
  const pages = new Pages("book"),
    controller = new AbortController();
  const result = assert.rejects(pages.get(0, controller.signal), {
    name: "AbortError",
  });
  await flush();
  controller.abort();
  await result;
  assert.equal(cancelled, 0);
  t.mock.timers.tick(500);
  await flush();
  assert.equal(cancelled, 1);
  assert.equal(pages.state(0), undefined);
  pages.dispose();
});

test("background images share a bounded LRU cache and progress remembers successful downloads after eviction", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("12345"));
  const pages = new Pages("book", 10, 2),
    signal = new AbortController().signal;
  await pages.get(0, signal);
  await pages.get(1, signal);
  await pages.get(2, signal, true);
  assert.equal(pages.has(0), false);
  assert.equal(pages.has(1), true);
  assert.equal(pages.has(2), true);
  assert.equal(pages.bufferedAhead(1, 20), 2);
  assert.deepEqual(pages.progress(20), {
    done: 3,
    failed: 0,
    total: 20,
    waiting: 17,
  });
  pages.invalidate(2);
  assert.equal(pages.progress(20).failed, 1);
  await pages.get(2, signal);
  assert.equal(pages.progress(20).failed, 0);
  pages.dispose();
});

test("an empty page response is an error and never a cached page", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(""));
  const pages = new Pages("book");
  await assert.rejects(
    pages.get(0, new AbortController().signal),
    EmptyPageError,
  );
  assert.equal(pages.has(0), false);
  assert.equal(pages.state(0), "failed");
  pages.dispose();
});

test("thumbnail and cover URLs carry the content version", () => {
  assert.equal(
    thumbnailPath("a b", 3, "0123456789abcdef9999"),
    "/api/books/a%20b/thumbnails/3?v=0123456789abcdef",
  );
  assert.equal(thumbnailPath("id", 0, undefined), "/api/books/id/thumbnails/0");
  assert.equal(
    coverUrl("id", "fedcba98765432100000"),
    "/api/books/id/cover?v=fedcba9876543210",
  );
});
