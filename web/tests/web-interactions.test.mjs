import { one, dispatch, all, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import {
  SearchBox,
  SEARCH_DEBOUNCE_MS,
  SUGGESTION_DELAY_MS,
  tokenAt,
} from "../src/SearchBox.tsx";
import { rangeIds, intersects, BookShelf } from "../src/BookShelf.tsx";
import { LibraryFilters, initialBrowse } from "../src/LibraryFilters.tsx";
import { BulkActions, BULK_POLL_MS } from "../src/BulkActions.tsx";
import { Modal } from "../src/Modal.tsx";
import {
  button,
  click,
  installGlobals,
  tick,
  visibleText,
} from "./harness.mjs";
const t = (zh, en) => en;
const tag = {
  namespace: "artist",
  value: "blue sky",
  translation: "蓝天",
  count: 2,
};
test("the caret token is found across CJK text and apostrophes", () => {
  assert.deepEqual(tokenAt("你好 蓝", 4), { start: 3, end: 4 });
  assert.deepEqual(tokenAt("reader's 蓝", 10), { start: 9, end: 10 });
  assert.deepEqual(tokenAt('one artist:"blue s" remaining', 13), {
    start: 4,
    end: 19,
  });
});
test("range selection works in either direction and rectangle selection includes touching edges", () => {
  assert.deepEqual(rangeIds(["a", "b", "c", "d"], "c", "a"), ["a", "b", "c"]);
  assert.deepEqual(rangeIds(["a", "b", "c"], null, "b"), ["a", "b"]);
  assert.equal(
    intersects(
      { left: 0, top: 0, right: 10, bottom: 10 },
      { left: 10, top: 4, right: 12, bottom: 8 },
    ),
    true,
  );
  assert.equal(
    intersects(
      { left: 0, top: 0, right: 10, bottom: 10 },
      { left: 11, top: 4, right: 12, bottom: 8 },
    ),
    false,
  );
});
test("combobox respects IME and selecting a suggestion searches immediately", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let current = "",
    submitted = "",
    changed = "";
  context.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(JSON.stringify({ tags: [tag] }), {
        headers: { "Content-Type": "application/json" },
      }),
  );
  let view;
  function Host() {
    const [value, setValue] = React.useState("");
    current = value;
    return React.createElement(SearchBox, {
      value,
      change: (v) => {
        changed = v;
        setValue(v);
      },
      search: (v) => {
        submitted = v;
      },
      t,
    });
  }
  await act(async () => {
    view = render(React.createElement(Host));
  });
  context.after(async () => {
    await act(async () => view.unmount());
  });
  const input = () => one(view.container, "input");
  await act(async () =>
    dispatch(input(), "change", { target: { value: "蓝", selectionStart: 1 } }),
  );
  assert.equal(submitted, "");
  await tick(context, SUGGESTION_DELAY_MS);
  assert.equal(all(view.container, { role: "option" }).length, 1);
  await tick(context, SEARCH_DEBOUNCE_MS - SUGGESTION_DELAY_MS);
  assert.equal(submitted, "蓝");
  let prevented = 0;
  const key = (value) => ({
    key: value,
    nativeEvent: {},
    preventDefault: () => prevented++,
  });
  await act(async () => dispatch(input(), "compositionStart"));
  await act(async () => dispatch(input(), "keyDown", key("Enter")));
  await act(async () =>
    dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
  );
  await tick(context, SEARCH_DEBOUNCE_MS);
  assert.equal(submitted, "蓝");
  await act(async () =>
    dispatch(input(), "compositionEnd", { currentTarget: { value: "蓝" } }),
  );
  await tick(context, SEARCH_DEBOUNCE_MS);
  await act(async () => dispatch(input(), "keyDown", key("ArrowDown")));
  await act(async () => dispatch(input(), "keyDown", key("Enter")));
  assert.equal(current, 'artist:"blue sky"');
  assert.equal(changed, current);
  assert.equal(submitted, current);
  assert.equal(prevented, 2);
});
test("all-results selection remains distinct from loaded books and supports exclusions", async (context) => {
  const restore = installGlobals({
    window: globalThis.window,
    document: globalThis.document,
  });
  const books = Array.from({ length: 3 }, (_, i) => ({
    id: String(i),
    metadata: { title: `Book ${i}`, language: "en" },
    content: { page_count: 4 },
    personal: { resume_page: null, completed: false, rating: null },
    review_state: "confirmed",
  }));
  let output;
  context.mock.method(
    globalThis,
    "fetch",
    async () => new Response("", { status: 404 }),
  );
  let view;
  await act(async () => {
    view = render(
      React.createElement(BookShelf, {
        selectRequested: false,
        onSelectionStarted() {},
        books,
        total: 66,
        browse: { query: "a" },
        busy: false,
        t,
        open() {},
        organize: (v) => (output = v),
      }),
    );
  });
  context.after(async () => {
    await act(async () => view.unmount());
    restore();
  });
  await click(button(view, "Select"));
  await click(button(view, "Select all 66 results"));
  await click(button(view, "Select Book 1"));
  await click(button(view, "Bulk actions"));
  assert.equal(output.all.total, 66);
  assert.deepEqual(output.all.exclude, ["1"]);
  assert.deepEqual(output.books, []);
  await click(button(view, "Select 3 loaded"));
  await click(button(view, "Bulk actions"));
  assert.equal(output.all, undefined);
  assert.equal(output.books.length, 3);
});
test("suggestions of the previous fragment disappear as soon as the fragment changes", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  context.mock.method(globalThis, "fetch", async (path) => {
    const query = new URL(path, "http://x").searchParams.get("query");
    return new Response(
      JSON.stringify({
        tags: [
          {
            namespace: "artist",
            value: `${query}-tag`,
            translation: null,
            count: 1,
          },
        ],
      }),
      { headers: { "Content-Type": "application/json" } },
    );
  });
  let view;
  await act(async () => {
    view = render(
      React.createElement(SearchBox, { value: "", search() {}, t }),
    );
  });
  context.after(async () => {
    await act(async () => view.unmount());
  });
  const type = (value) =>
    act(async () =>
      dispatch(one(view.container, "input"), "change", {
        target: { value, selectionStart: value.length },
      }),
    );
  await type("ab");
  await tick(context, SUGGESTION_DELAY_MS);
  assert.match(visibleText(view), /ab-tag/);
  await type("xy");
  assert.doesNotMatch(visibleText(view), /ab-tag/);
  assert.match(visibleText(view), /Searching/);
  await tick(context, SUGGESTION_DELAY_MS);
  assert.match(visibleText(view), /xy-tag/);
});
const domGlobals = () =>
  installGlobals({
    window: globalThis.window,
    document: globalThis.document,
  });
test("filter helper buttons never submit an enclosing form", async (context) => {
  context.after(domGlobals());
  let view;
  const browse = { ...initialBrowse(), order: "random", reading: "unread" };
  await act(async () => {
    view = render(
      React.createElement(
        "form",
        null,
        React.createElement(LibraryFilters, { value: browse, change() {}, t }),
      ),
    );
  });
  const buttons = all(view.container, "button");
  assert.ok(buttons.some((node) => node.textContent.includes("Shuffle again")));
  assert.deepEqual(
    buttons.filter((node) => attr(node, "type") !== "button").map(String),
    [],
  );
  await act(async () => view.unmount());
});
async function bulk(context, routes, selection) {
  const restore = domGlobals();
  const calls = [];
  context.mock.method(globalThis, "fetch", async (path, init = {}) => {
    calls.push(`${init.method ?? "GET"} ${path}`);
    for (const [pattern, reply] of routes)
      if (pattern.test(`${init.method ?? "GET"} ${path}`)) {
        const value = await reply(init);
        return new Response(JSON.stringify(value), {
          headers: { "Content-Type": "application/json" },
        });
      }
    return new Response(JSON.stringify({ collections: [] }), {
      headers: { "Content-Type": "application/json" },
    });
  });
  let changed = 0,
    closed = 0,
    view;
  await act(async () => {
    view = render(
      React.createElement(BulkActions, {
        selection,
        t,
        close: () => closed++,
        changed: () => changed++,
      }),
    );
  });
  context.after(async () => {
    await act(async () => view.unmount());
    restore();
  });
  return {
    view,
    calls,
    changed: () => changed,
    closed: () => closed,
    text: () => visibleText(view),
  };
}
const shelfBook = (id) => ({
  id,
  metadata: { title: id, source: null },
  content: { original_filename: `${id}.cbz` },
  metadata_revision: 1,
  personal_revision: 1,
});
test("bulk jobs run on the server; the dialog only polls until the job is done", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let polls = 0;
  const x = await bulk(
    context,
    [
      [
        /^POST \/api\/bulk$/,
        () => ({ id: "job", state: "running", counts: { pending: 2 } }),
      ],
      [
        /^GET \/api\/bulk\/job/,
        () =>
          ++polls < 2
            ? { id: "job", state: "running", counts: { pending: 1, done: 1 } }
            : { id: "job", state: "done", counts: { pending: 0, done: 2 } },
      ],
    ],
    { books: [shelfBook("a"), shelfBook("b")] },
  );
  await click(button(x.view, "Apply"));
  assert.match(x.text(), /0 \/ 2/);
  await tick(context, BULK_POLL_MS);
  assert.match(x.text(), /1 \/ 2/);
  await tick(context, BULK_POLL_MS);
  assert.match(x.text(), /Complete/);
  assert.equal(x.changed(), 1);
  assert.ok(!x.calls.some((call) => call.includes("/step")));
});
test("loading all results for removal can be stopped and the dialog stays closable", async (context) => {
  let release;
  const x = await bulk(
    context,
    [
      [
        /^POST \/api\/removals\/search\/selection$/,
        (init) =>
          new Promise((resolve, reject) => {
            release = () => resolve({ book_ids: ["a"] });
            init.signal.addEventListener("abort", () =>
              reject(init.signal.reason),
            );
          }),
      ],
    ],
    {
      books: [],
      all: {
        total: 1,
        exclude: [],
        browse: { ...initialBrowse(), query: "x" },
      },
    },
  );
  await act(async () =>
    dispatch(one(one(x.view.container, "dialog"), "select"), "change", {
      target: { value: "remove" },
    }),
  );
  await click(button(x.view, "Continue"));
  assert.ok(button(x.view, "Stop"));
  await click(button(x.view, "Stop"));
  assert.match(x.text(), /Stopped\./);
  assert.ok(release);
  await click(button(x.view, "Continue"));
  assert.ok(button(x.view, "Stop"));
  // The dialog can be dismissed while results are still loading.
  await click(button(x.view, "Close"));
  assert.equal(x.closed(), 1);
});
test("Modal closes once per Escape and keeps Escape inside local comboboxes", async (context) => {
  context.after(domGlobals());
  let closed = 0,
    view;
  await act(async () => {
    view = render(
      React.createElement(
        Modal,
        { title: "Dialog", closeLabel: "Close", close: () => closed++ },
        React.createElement("input", { "data-escape-local": "true" }),
      ),
    );
  });
  const dialog = one(view.container, "dialog");
  const local = one(view.container, "input");
  await act(async () => dispatch(local, "cancel"));
  assert.equal(closed, 0);
  await act(async () => dispatch(dialog, "cancel"));
  assert.equal(closed, 1);
  const keyDown = (target, prevented = false) =>
    act(async () => {
      const event = new KeyboardEvent("keydown", {
        key: "Escape",
        bubbles: true,
        cancelable: true,
      });
      if (prevented) event.preventDefault();
      target.dispatchEvent(event);
    });
  await keyDown(dialog, true);
  assert.equal(closed, 1);
  await keyDown(local);
  assert.equal(closed, 1);
  await keyDown(dialog);
  assert.equal(closed, 2);
  await act(async () => view.unmount());
});
