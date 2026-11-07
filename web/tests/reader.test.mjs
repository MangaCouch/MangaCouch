import { attr, all, one, dispatch } from "./dom.mjs";
import {
  PROGRESS_SAVE_DELAY_MS,
  SAVING_PILL_DELAY_MS,
} from "../src/readerLifecycle.ts";
import assert from "node:assert/strict";
import test from "node:test";
import React, { useState } from "react";
import { render, act } from "@testing-library/react";
import { Reader } from "../src/Reader.tsx";
import { PersonalQueue } from "../src/personal.ts";
import {
  PreferencesContext,
  defaultPreferences,
} from "../src/PreferenceContext.tsx";
import { defaultDevice, storageKey } from "../src/device.ts";
import { Pages } from "../src/pages.ts";
import { visibleText } from "./harness.mjs";
async function setup(t, options = {}) {
  const storage = new Map([
    [storageKey("autoplay-seconds"), "3"],
    [storageKey("fit"), "width"],
    [storageKey("reading-mode"), options.mode || "single"],
    [storageKey("cover-solo"), String(options.coverSolo ?? true)],
  ]);
  if (options.emptyDevice) storage.clear();
  const document = globalThis.document;
  document.hidden = false;
  const window = globalThis.window;
  const observers = new Set();
  const originalObserver = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe() {
      observers.add(this.callback);
    }
    disconnect() {
      observers.delete(this.callback);
    }
  };
  t.after(() => {
    if (originalObserver) globalThis.ResizeObserver = originalObserver;
    else delete globalThis.ResizeObserver;
  });
  globalThis.document = document;
  globalThis.window = window;
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
    },
  });
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let now = 1000;
  t.mock.method(performance, "now", () => now);
  const blobs = new Map(),
    decodes = [],
    decoded = new Map();
  t.mock.method(URL, "createObjectURL", (blob) => {
    const url = `blob:test-${blobs.size}-${Math.random()}`;
    blobs.set(url, blob);
    return url;
  });
  t.mock.method(URL, "revokeObjectURL", (url) => blobs.delete(url));
  globalThis.requestAnimationFrame = (fn) => {
    fn();
    return 1;
  };
  globalThis.Image = class {
    async decode() {
      const page = Number(await blobs.get(this.src).text());
      const ratio = decoded.has(page)
        ? decoded.get(page)
        : await new Promise((resolve, reject) =>
            decodes.push({ page, resolve, reject }),
          );
      this.naturalWidth = ratio * 1000;
      this.naturalHeight = 1000;
    }
  };
  let book = {
    id: "test-book",
    metadata: { title: "Reader fixture" },
    content: { page_count: options.count ?? 4, sha256: options.sha256 },
    ...(options.sizes ? { page_sizes: options.sizes } : {}),
    personal_revision: 1,
    personal: {
      resume_page: options.resume ?? null,
      completed: false,
      rating: null,
      bookmarks: options.bookmarks ?? [],
    },
  };
  const writes = [],
    requests = [];
  let failure = false,
    notify = () => {},
    release = () => {};
  const queue = new PersonalQueue(
    book,
    async (state, revision) => {
      if (failure) throw Error("save failed");
      if (options.holdSaves)
        await new Promise((resolve) => {
          release = resolve;
        });
      assert.equal(revision, book.personal_revision);
      book = { ...book, personal: state, personal_revision: revision + 1 };
      writes.push(structuredClone(state));
      return book;
    },
    async () => book,
    (book, status) => notify({ book, status }),
  );
  const invalidated = [];
  const pages = new Pages("test-book");
  const invalidate = pages.invalidate.bind(pages);
  t.mock.method(pages, "invalidate", (page) => {
    invalidated.push(page);
    invalidate(page);
  });
  t.mock.method(globalThis, "fetch", async (path, init) => {
    init.signal.throwIfAborted();
    requests.push(Number(path.split("/").at(-1)));
    if (options.stall?.includes(requests.at(-1)))
      await new Promise((_, reject) =>
        init.signal.addEventListener("abort", () => reject(init.signal.reason)),
      );
    return new Response(new TextEncoder().encode(String(requests.at(-1))));
  });
  function Harness() {
    const [state, setState] = useState({ book, status: "saved" });
    notify = setState;
    return React.createElement(Reader, {
      ...state,
      queue,
      pages,
      t: (_, en) => en,
      exit: () => {},
      collection: options.collection,
    });
  }
  let viewport = {
    clientWidth: 800,
    clientHeight: 600,
    scrollTop: 0,
    focus() {},
  };
  Object.defineProperty(Element.prototype, "clientWidth", {
    configurable: true,
    get: () => 800,
  });
  Object.defineProperty(Element.prototype, "clientHeight", {
    configurable: true,
    get: () => 600,
  });
  let view;
  await act(async () => {
    view = render(
      React.createElement(
        PreferencesContext.Provider,
        {
          value: {
            saved: defaultPreferences,
            device: { ...defaultDevice, ...options.device },
            accept: () => {},
          },
        },
        React.createElement(Harness),
      ),
    );
  });
  if (options.mode === "scroll") {
    viewport = one(view.container, { className: "page-canvas scroll-reader" });
    Object.defineProperty(viewport, "clientWidth", {
      configurable: true,
      writable: true,
      value: 800,
    });
    Object.defineProperty(viewport, "clientHeight", {
      configurable: true,
      writable: true,
      value: 600,
    });
  }
  t.after(async () => {
    await act(async () => view.unmount());
    pages.dispose();
    queue.close();
    delete globalThis.Image;
    delete globalThis.requestAnimationFrame;
    delete globalThis.localStorage;
  });
  const image = (page) =>
    page === undefined
      ? all(view.container, "img")
          .filter((node) => attr(node, "alt")?.startsWith("Page "))
          .at(-1)
      : one(view.container, { alt: `Page ${page + 1}` });
  const play = () =>
    all(view.container, "button").find((node) =>
      node.textContent.includes("3s"),
    );
  const button = (label) =>
    all(view.container, "button").find(
      (node) => attr(node, "aria-label") === label,
    );
  const loaded = async (page, ratio = 0.7) => {
    if (options.mode === "scroll") {
      await act(async () =>
        dispatch(image(page), "load", {
          currentTarget: { naturalWidth: ratio * 1000, naturalHeight: 1000 },
        }),
      );
      return;
    }
    const target =
      page ?? attr(one(view.container, { type: "range" }), "value");
    await act(async () => {
      decoded.set(target, ratio);
      for (const decode of decodes.filter((item) => item.page === target))
        decode.resolve(ratio);
    });
  };
  const click = async (node) => {
    now += 1000;
    await act(async () => {
      dispatch(node, "click");
    });
  };
  const tick = async (amount) => {
    await act(async () => {
      now += amount;
      t.mock.timers.tick(amount);
    });
  };
  return {
    view,
    displayed: () =>
      all(view.container, "img")
        .filter((node) => attr(node, "alt")?.startsWith("Page "))
        .map((node) => attr(node, "alt")),
    rejectDecode: async (page) =>
      act(async () => {
        for (const item of decodes.filter((item) => item.page === page))
          item.reject(Error("decode failed"));
      }),
    key: async (key, repeat = false, elapsed = 0) => {
      now += elapsed;
      const event = new Event("keydown", { cancelable: true });
      Object.defineProperties(event, {
        key: { value: key },
        repeat: { value: repeat },
        target: { value: { closest: () => null } },
      });
      await act(async () => window.dispatchEvent(event));
    },
    viewport,
    resizeViewport: () => observers.forEach((callback) => callback()),
    storage,
    invalidated,
    writes,
    requests,
    queue,
    document,
    window,
    image,
    play,
    button,
    loaded,
    click,
    tick,
    /** Lets the debounced progress save go out. */
    saved: () => tick(PROGRESS_SAVE_DELAY_MS),
    releaseSave: () => act(async () => release()),
    text: () => visibleText(view),
    stage: () => one(view.container, { "aria-label": "Reading page" }),
    page: () => attr(one(view.container, { type: "range" }), "value"),
    fail: () => {
      failure = true;
    },
  };
}
test("preview and prefetch do not save reading progress; autoplay waits for displayed decoded pages", async (t) => {
  const x = await setup(t);
  for (let i = 0; i < 5; i++) await x.tick(50);
  assert.ok(x.requests.includes(3));
  assert.equal(x.writes.length, 0);
  assert.deepEqual(x.displayed(), []);
  await x.loaded();
  assert.deepEqual(x.displayed(), ["Page 1"]);
  await x.click(x.play());
  await x.tick(3000);
  assert.deepEqual(x.displayed(), ["Page 1"]);
  assert.equal(attr(one(x.view.container, { type: "range" }), "value"), 1);
  await x.tick(9000);
  assert.equal(x.writes.at(-1).resume_page, 0);
  await x.loaded(1);
  assert.deepEqual(x.displayed(), ["Page 2"]);
  await x.tick(3000);
  await x.loaded(2);
  await x.tick(3000);
  await x.loaded(3);
  assert.equal(x.writes.at(-1).completed, true);
  assert.equal(attr(x.play(), "disabled"), true);
});
test("background and manual navigation pause, foreground never restarts playback", async (t) => {
  const x = await setup(t);
  await x.loaded();
  await x.click(x.play());
  await act(async () => {
    x.document.hidden = true;
    x.document.dispatchEvent(new Event("visibilitychange"));
  });
  await x.tick(10000);
  assert.equal(attr(x.image(), "alt"), "Page 1");
  assert.equal(attr(x.play(), "aria-pressed"), false);
  await act(async () => {
    x.document.hidden = false;
    x.document.dispatchEvent(new Event("visibilitychange"));
  });
  await x.tick(10000);
  assert.equal(attr(x.image(), "alt"), "Page 1");
  await x.click(x.play());
  await x.click(x.button("Next page"));
  await x.loaded();
  await x.tick(10000);
  assert.equal(attr(x.image(), "alt"), "Page 2");
  assert.equal(attr(x.play(), "aria-pressed"), false);
});
test("settings, same-page jumps, and IME keyboard events preserve the loaded page", async (t) => {
  const x = await setup(t);
  await x.loaded();
  await x.click(x.play());
  await x.click(x.button("Reading settings"));
  assert.equal(attr(x.play(), "aria-pressed"), false);
  await act(async () => {
    dispatch(one(x.view.container, { type: "range" }), "change", {
      target: { value: "0" },
    });
  });
  assert.equal(all(x.view.container, { className: "page-loading" }).length, 0);
  const event = new Event("keydown", { cancelable: true });
  Object.defineProperties(event, {
    target: { value: { closest: () => null } },
    code: { value: "Space" },
    isComposing: { value: true },
  });
  await act(async () => x.window.dispatchEvent(event));
  assert.equal(event.defaultPrevented, false);
  assert.equal(attr(x.play(), "aria-pressed"), false);
});
test("image and save failures stop playback before further page turns", async (t) => {
  const x = await setup(t);
  await x.loaded();
  await x.click(x.play());
  x.fail();
  await x.tick(3000);
  await x.loaded();
  assert.equal(attr(x.play(), "disabled"), true);
  assert.equal(attr(x.play(), "aria-pressed"), false);
  await x.tick(10000);
  assert.equal(attr(x.image(), "alt"), "Page 2");
  assert.ok(all(x.view.container, { role: "alert" }).length);
});
test("decode failure retains the last frame, pauses playback and offers retry", async (t) => {
  const x = await setup(t);
  await x.loaded(0);
  await x.click(x.button("Next page"));
  await x.click(x.play());
  await x.rejectDecode(1);
  assert.deepEqual(x.displayed(), ["Page 1"]);
  assert.equal(attr(x.play(), "disabled"), true);
  assert.equal(attr(x.play(), "aria-pressed"), false);
  assert.deepEqual(x.invalidated, [1]);
  assert.ok(all(x.view.container, { role: "alert" }).length);
});
test("spread replacement is atomic and reading completion requires both decoded pages", async (t) => {
  const x = await setup(t, { mode: "spread", count: 5 });
  await x.loaded(0);
  await x.click(x.button("Next page"));
  await x.click(x.play());
  await x.loaded(1);
  await x.tick(9000);
  assert.deepEqual(x.displayed(), ["Page 1"]);
  assert.equal(x.writes.at(-1).resume_page, 0);
  await x.loaded(2);
  assert.deepEqual(x.displayed(), ["Page 2", "Page 3"]);
  await x.tick(3000);
  await x.loaded(4);
  assert.deepEqual(x.displayed(), ["Page 2", "Page 3"]);
  assert.equal(x.writes.at(-1).completed, false);
  await x.loaded(3);
  assert.deepEqual(x.displayed(), ["Page 4", "Page 5"]);
  assert.equal(x.writes.at(-1).completed, true);
});
test("spreads pair from recorded page sizes, so a wide page is alone before it is ever loaded", async (t) => {
  const portrait = [700, 1000],
    wide = [1800, 1000];
  const x = await setup(t, {
    mode: "spread",
    count: 5,
    sizes: [portrait, portrait, wide, portrait, portrait],
  });
  await x.click(x.button("Next page"));
  await x.loaded(1);
  assert.deepEqual(x.displayed(), ["Page 2"]);
  await x.click(x.button("Next page"));
  await x.loaded(2, 1.8);
  assert.deepEqual(x.displayed(), ["Page 3"]);
  await x.click(x.button("Next page"));
  await x.loaded(3);
  await x.loaded(4);
  assert.deepEqual(x.displayed(), ["Page 4", "Page 5"]);
});
test("resuming in the middle of a book keeps the spread pairing of earlier wide pages", async (t) => {
  const portrait = [700, 1000],
    wide = [1800, 1000];
  const x = await setup(t, {
    mode: "spread",
    count: 6,
    resume: 3,
    sizes: [portrait, wide, portrait, portrait, portrait, portrait],
  });
  await x.loaded(2);
  await x.loaded(3);
  assert.deepEqual(x.displayed(), ["Page 3", "Page 4"]);
});
test("spread page selection keeps original numbering and RTL reverses visual and keyboard direction", async (t) => {
  const x = await setup(t, { mode: "spread", count: 5 });
  await x.click(x.button("Next page"));
  await x.loaded(1);
  await x.loaded(2);
  await act(async () =>
    dispatch(x.button("Select page 3"), "click", { stopPropagation() {} }),
  );
  assert.equal(attr(one(x.view.container, { type: "range" }), "value"), 2);
  assert.equal(x.button("Bookmark this page"), undefined);
  await x.click(x.button("Reading settings"));
  await act(async () =>
    dispatch(one(x.view.container, { "aria-label": "Direction" }), "change", {
      target: { value: "rtl" },
    }),
  );
  await x.click(x.button("Reading settings"));
  assert.equal(
    all(x.view.container, "*").find(
      (n) =>
        n instanceof HTMLElement &&
        attr(n, "className")?.includes("retained-frame rtl"),
    ).children.length,
    2,
  );
  const event = new Event("keydown", { cancelable: true });
  Object.defineProperties(event, {
    target: { value: { closest: () => null } },
    key: { value: "ArrowLeft" },
  });
  await act(async () => x.window.dispatchEvent(event));
  await x.tick(180);
  await x.loaded(3);
  await x.loaded(4);
  assert.deepEqual(x.displayed(), ["Page 4", "Page 5"]);
});
test("switching modes keeps the selected original page and stops autoplay", async (t) => {
  const x = await setup(t, { resume: 2 });
  await x.loaded();
  await x.click(x.play());
  await x.click(x.button("Reading settings"));
  await act(async () =>
    dispatch(
      one(x.view.container, { "aria-label": "Reading mode" }),
      "change",
      { target: { value: "spread" } },
    ),
  );
  assert.equal(attr(x.play(), "aria-pressed"), false);
  assert.equal(x.storage.get(storageKey("reading-mode")), "spread");
  assert.equal(attr(one(x.view.container, { type: "range" }), "value"), 2);
  await act(async () =>
    dispatch(
      one(x.view.container, { "aria-label": "Reading mode" }),
      "change",
      { target: { value: "single" } },
    ),
  );
  assert.equal(attr(x.image(), "alt"), "Page 3");
});
test("late decoding from an abandoned target cannot advance progress", async (t) => {
  const x = await setup(t);
  await x.click(x.button("Next page"));
  await x.loaded(0);
  assert.equal(x.writes.length, 0);
  await x.loaded(1);
  assert.equal(x.writes.length, 0);
  await x.saved();
  assert.equal(x.writes.at(-1).resume_page, 1);
});
test("first key responds immediately, held repeats are limited and targets settle to one HD load", async (t) => {
  const x = await setup(t, { count: 30 });
  await x.loaded(0);
  await x.key("ArrowRight");
  assert.equal(attr(one(x.view.container, { type: "range" }), "value"), 1);
  await x.key("ArrowRight", true, 20);
  assert.equal(attr(one(x.view.container, { type: "range" }), "value"), 1);
  await x.key("ArrowRight", true, 80);
  await x.key("ArrowRight", true, 100);
  assert.equal(attr(one(x.view.container, { type: "range" }), "value"), 3);
  assert.deepEqual(x.displayed(), ["Page 1"]);
  assert.ok(
    all(x.view.container, "img").some((node) =>
      attr(node, "src").endsWith("/thumbnails/3"),
    ),
  );
  await x.tick(180);
  await x.loaded(3);
  assert.deepEqual(x.displayed(), ["Page 4"]);
  await x.saved();
  assert.equal(x.writes.at(-1).resume_page, 3);
});
test("scroll autoplay traverses a long final page by screen and completes only at its bottom", async (t) => {
  const x = await setup(t, { mode: "scroll", count: 1, sizes: [[400, 1000]] });
  await x.loaded(0, 0.4);
  assert.notEqual(x.writes.at(-1)?.completed, true);
  assert.equal(attr(x.play(), "disabled"), false);
  await x.click(x.play());
  await x.tick(3000);
  assert.equal(x.viewport.scrollTop, 510);
  assert.notEqual(x.writes.at(-1)?.completed, true);
  await x.tick(3000);
  assert.equal(x.viewport.scrollTop, 1020);
  await x.tick(3000);
  assert.equal(x.viewport.scrollTop, 1400);
  assert.equal(x.writes.at(-1).completed, true);
  assert.equal(attr(x.play(), "aria-pressed"), false);
});
test("scroll preloaded last page never marks complete until it is actually reached", async (t) => {
  const x = await setup(t, {
    mode: "scroll",
    count: 2,
    sizes: [
      [800, 1000],
      [800, 1000],
    ],
  });
  await x.loaded(0, 0.8);
  await x.loaded(1, 0.8);
  await x.saved();
  assert.equal(x.writes.at(-1).completed, false);
  const node = one(x.view.container, {
    className: "page-canvas scroll-reader",
  });
  await act(async () => {
    x.viewport.scrollTop = 1416;
    dispatch(node, "scroll", { currentTarget: x.viewport });
  });
  assert.equal(x.writes.at(-1).resume_page, 1);
  assert.equal(x.writes.at(-1).completed, true);
});
test("scrolling mounts a bounded image window when jumping through a 2000-page book", async (t) => {
  const x = await setup(t, { mode: "scroll", count: 2000 });
  assert.ok(all(x.view.container, "img").length <= 7);
  assert.ok(x.requests.length < 12);
  await x.click(x.button("Reading settings"));
  await act(async () =>
    dispatch(one(x.view.container, { type: "range" }), "change", {
      target: { value: "1999" },
    }),
  );
  assert.ok(all(x.view.container, "img").length <= 7);
  assert.ok(x.requests.length < 25);
  await x.loaded(1999, 0.8);
  await x.saved();
  assert.equal(x.writes.at(-1).resume_page, 1999);
  assert.equal(x.writes.at(-1).completed, false);
});
test("wheel and background pause scrolling playback without resuming automatically", async (t) => {
  const x = await setup(t, { mode: "scroll", count: 1, sizes: [[400, 1000]] });
  await x.loaded(0, 0.4);
  await x.click(x.play());
  await act(async () =>
    dispatch(
      one(x.view.container, { className: "page-canvas scroll-reader" }),
      "wheel",
    ),
  );
  await x.tick(9000);
  assert.equal(x.viewport.scrollTop, 0);
  await x.click(x.play());
  await act(async () => {
    x.document.hidden = true;
    x.document.dispatchEvent(new Event("visibilitychange"));
  });
  await x.tick(9000);
  assert.equal(x.viewport.scrollTop, 0);
  await act(async () => {
    x.document.hidden = false;
    x.document.dispatchEvent(new Event("visibilitychange"));
  });
  await x.tick(9000);
  assert.equal(x.viewport.scrollTop, 0);
});
test("legacy bookmark data does not hide pages or expose removed controls", async (t) => {
  const x = await setup(t, { mode: "scroll", count: 5, bookmarks: [2] });
  await act(async () =>
    dispatch(one(x.view.container, { type: "range" }), "change", {
      target: { value: "2" },
    }),
  );
  await x.loaded(2, 0.7);
  await x.saved();
  assert.equal(x.writes.at(-1).resume_page, 2);
  assert.deepEqual(x.writes.at(-1).bookmarks, [2]);
  assert.equal(x.button("Bookmark this page"), undefined);
});
test("scroll viewport resize preserves the visible page and fraction through a long image", async (t) => {
  const x = await setup(t, { mode: "scroll", count: 1, sizes: [[400, 1000]] });
  await x.loaded(0, 0.4);
  const node = one(x.view.container, {
    className: "page-canvas scroll-reader",
  });
  await act(async () => {
    x.viewport.scrollTop = 800;
    dispatch(node, "scroll", { currentTarget: x.viewport });
  });
  await act(async () => {
    x.viewport.clientWidth = 400;
    x.resizeViewport();
  });
  assert.ok(Math.abs(x.viewport.scrollTop - 400) < 0.01);
  await x.saved();
  assert.equal(x.writes.at(-1).resume_page, 0);
});
test("scroll image failures pause playback and a successful retry does not restart it", async (t) => {
  const x = await setup(t, { mode: "scroll", count: 1, sizes: [[400, 1000]] });
  await x.loaded(0, 0.4);
  await x.click(x.play());
  await act(async () => dispatch(x.image(0), "error"));
  assert.equal(attr(x.play(), "disabled"), true);
  assert.deepEqual(x.invalidated, [0]);
  const retry = all(x.view.container, "button").find((node) =>
    node.textContent.includes("Retry page"),
  );
  await act(async () => dispatch(retry, "click", { stopPropagation() {} }));
  await x.loaded(0, 0.4);
  assert.equal(attr(x.play(), "disabled"), false);
  assert.equal(attr(x.play(), "aria-pressed"), false);
  await x.tick(9000);
  assert.equal(x.viewport.scrollTop, 0);
});
test("collection next book pauses playback and only moves on an explicit click", async (t) => {
  const moves = [];
  const x = await setup(t, {
    collection: {
      name: "Series",
      index: 0,
      count: 2,
      previous: false,
      next: true,
      busy: false,
      error: "",
      back: () => {},
      move: (direction) => moves.push(direction),
    },
  });
  await x.loaded();
  await x.click(x.play());
  await act(async () =>
    dispatch(
      one(x.view.container, { "aria-label": "Reading page" }),
      "clickCapture",
      {
        target: { closest: () => null },
        currentTarget: {
          getBoundingClientRect: () => ({ left: 0, width: 100 }),
        },
        clientX: 50,
      },
    ),
  );
  const next = all(x.view.container, "button").find((node) =>
    node.textContent.includes("Next book"),
  );
  await x.click(next);
  assert.deepEqual(moves, [1]);
  assert.equal(attr(x.play(), "aria-pressed"), false);
  await x.tick(9000);
  assert.deepEqual(moves, [1]);
});
test("autoplay stops at the last book page without advancing the collection", async (t) => {
  const moves = [];
  const x = await setup(t, {
    count: 2,
    collection: {
      name: "Series",
      index: 0,
      count: 2,
      previous: false,
      next: true,
      busy: false,
      error: "",
      back: () => {},
      move: (direction) => moves.push(direction),
    },
  });
  await x.loaded();
  await x.click(x.play());
  await x.tick(3000);
  await x.loaded(1);
  await x.tick(9000);
  assert.deepEqual(moves, []);
  assert.equal(attr(x.play(), "aria-pressed"), false);
  assert.equal(x.writes.at(-1).completed, true);
});
test("scroll resume and far jumps request the current page before neighbouring buffer images", async (t) => {
  const x = await setup(t, { mode: "scroll", count: 2000, resume: 1000 });
  assert.equal(x.requests[0], 1000);
  await x.click(x.button("Reading settings"));
  const before = x.requests.length;
  await act(async () =>
    dispatch(one(x.view.container, { type: "range" }), "change", {
      target: { value: "1999" },
    }),
  );
  assert.equal(x.requests[before], 1999);
  assert.ok(all(x.view.container, "img").length <= 7);
});
test("initialized device defaults reach the reader while autoplay stays paused", async (t) => {
  const x = await setup(t, {
    emptyDevice: true,
    device: { reading_mode: "spread", direction: "rtl", autoplay_seconds: 12 },
  });
  assert.equal(attr(x.play(), "aria-pressed"), false);
  await x.click(x.button("Reading settings"));
  assert.equal(
    attr(one(x.view.container, { "aria-label": "Reading mode" }), "value"),
    "spread",
  );
  assert.equal(
    attr(one(x.view.container, { "aria-label": "Direction" }), "value"),
    "rtl",
  );
  assert.equal(x.storage.has(storageKey("playing")), false);
});
test("hidden tools leave the scrubber inert; only the reveal strip brings them back", async (t) => {
  const x = await setup(t);
  await x.loaded();
  const scrubber = () =>
    all(x.view.container, "*").find(
      (node) =>
        node instanceof HTMLElement &&
        attr(node, "className") === "reader-scrubber glass",
    );
  assert.equal(attr(scrubber(), "inert"), undefined);
  await act(async () =>
    dispatch(x.stage(), "clickCapture", {
      target: { closest: () => null },
      currentTarget: { getBoundingClientRect: () => ({ left: 0, width: 100 }) },
      clientX: 50,
    }),
  );
  assert.equal(attr(scrubber(), "inert"), true);
  assert.equal(attr(scrubber(), "aria-hidden"), true);
  const reveal = x.button("Show reading controls");
  await act(async () =>
    dispatch(reveal, "pointerEnter", { pointerType: "touch" }),
  );
  assert.equal(attr(scrubber(), "inert"), true);
  await act(async () => dispatch(reveal, "click"));
  assert.equal(attr(scrubber(), "inert"), undefined);
  assert.equal(x.button("Show reading controls"), undefined);
});
test("horizontal swipes turn pages in the reading direction; vertical and multi-touch do not", async (t) => {
  const x = await setup(t, { count: 6 });
  await x.loaded();
  const swipe = (from, to, { dy = 0, second = false } = {}) =>
    act(async () => {
      const stage = x.stage();
      dispatch(stage, "pointerDown", {
        isPrimary: true,
        clientX: from,
        clientY: 100,
      });
      if (second)
        dispatch(stage, "pointerDown", {
          isPrimary: false,
          clientX: 10,
          clientY: 10,
        });
      dispatch(stage, "pointerUp", {
        isPrimary: true,
        clientX: to,
        clientY: 100 + dy,
      });
    });
  await swipe(300, 200);
  assert.equal(x.page(), 1);
  await swipe(200, 300);
  assert.equal(x.page(), 0);
  await swipe(300, 200, { dy: 150 });
  assert.equal(x.page(), 0);
  await swipe(300, 230, { second: true });
  assert.equal(x.page(), 0);
  await swipe(300, 260);
  assert.equal(x.page(), 0);
  await x.click(x.button("Reading settings"));
  await act(async () =>
    dispatch(one(x.view.container, { "aria-label": "Direction" }), "change", {
      target: { value: "rtl" },
    }),
  );
  await swipe(200, 300);
  assert.equal(x.page(), 1);
});
test("Space leaves focused buttons alone; paged mode supports page and Home/End keys", async (t) => {
  const x = await setup(t, { count: 6 });
  await x.loaded();
  const press = async (key, target = { closest: () => null }) => {
    const event = new Event("keydown", { cancelable: true });
    Object.defineProperties(event, {
      key: { value: key },
      code: { value: key === " " ? "Space" : key },
      target: { value: target },
    });
    await act(async () => x.window.dispatchEvent(event));
    return event;
  };
  const focusedButton = {
    closest: (selector) => (selector.includes("button") ? {} : null),
  };
  const space = await press(" ", focusedButton);
  assert.equal(space.defaultPrevented, false);
  assert.equal(attr(x.play(), "aria-pressed"), false);
  await press("PageDown");
  assert.equal(x.page(), 1);
  await x.tick(200);
  await press("ArrowDown");
  assert.equal(x.page(), 2);
  await x.tick(200);
  await press("ArrowUp");
  assert.equal(x.page(), 1);
  await x.tick(200);
  await press("End");
  assert.equal(x.page(), 5);
  await x.tick(200);
  await press("Home");
  assert.equal(x.page(), 0);
  await x.tick(200);
  await press("PageUp");
  assert.equal(x.page(), 0);
  await press(" ");
  assert.equal(attr(x.play(), "aria-pressed"), true);
});
test("the fullscreen button follows the document's fullscreen state", async (t) => {
  const x = await setup(t);
  assert.ok(x.button("Fullscreen"));
  await act(async () => {
    x.document.fullscreenElement = {};
    x.document.dispatchEvent(new Event("fullscreenchange"));
  });
  assert.equal(x.button("Fullscreen"), undefined);
  assert.equal(attr(x.button("Exit fullscreen"), "aria-pressed"), true);
  await act(async () => {
    x.document.fullscreenElement = null;
    x.document.dispatchEvent(new Event("fullscreenchange"));
  });
  assert.ok(x.button("Fullscreen"));
});
test("page turns save progress once after a pause, hiding the tab saves at once, and Saving only shows when slow", async (t) => {
  const x = await setup(t, { count: 6, holdSaves: true });
  await x.loaded(0);
  for (const page of [1, 2, 3]) {
    await x.click(x.button("Next page"));
    await x.loaded(page);
  }
  await x.tick(PROGRESS_SAVE_DELAY_MS - 100);
  assert.equal(x.writes.length, 0);
  assert.doesNotMatch(x.text(), /Saving/);
  await x.tick(100);
  await x.tick(SAVING_PILL_DELAY_MS - 100);
  assert.doesNotMatch(x.text(), /Saving/);
  await x.tick(100);
  assert.match(x.text(), /Saving/);
  await x.releaseSave();
  assert.deepEqual(
    x.writes.map((state) => state.resume_page),
    [3],
  );
  assert.doesNotMatch(x.text(), /Saving/);
  await x.click(x.button("Next page"));
  await x.loaded(4);
  await act(async () => {
    x.document.hidden = true;
    x.document.dispatchEvent(new Event("visibilitychange"));
  });
  await x.releaseSave();
  assert.equal(x.writes.at(-1).resume_page, 4);
});
test("a cached page shows no thumbnail; an uncached one gets a versioned thumbnail only when slow", async (t) => {
  const x = await setup(t, {
    count: 30,
    sha256: "abcdef0123456789ffff",
    stall: [20],
  });
  const thumbnails = () =>
    all(x.view.container, "img").filter((node) =>
      attr(node, "src")?.includes("/thumbnails/"),
    );
  assert.equal(thumbnails().length, 0);
  await x.tick(100);
  assert.equal(thumbnails().length, 0);
  await x.loaded(0);
  await x.tick(50);
  await x.click(x.button("Next page"));
  assert.equal(thumbnails().length, 0);
  await x.loaded(1);
  assert.deepEqual(x.displayed(), ["Page 2"]);
  await x.tick(200);
  await act(async () =>
    dispatch(one(x.view.container, { type: "range" }), "change", {
      target: { value: "20" },
    }),
  );
  assert.equal(thumbnails().length, 0);
  await x.tick(120);
  assert.equal(
    attr(thumbnails()[0], "src"),
    "/api/books/test-book/thumbnails/20?v=abcdef0123456789",
  );
});
