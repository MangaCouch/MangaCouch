import { one, dispatch, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import {
  rangeIds,
  intersects,
  BookShelf,
  noSelection,
} from "../src/BookShelf.tsx";
import { button, click, english as t, visibleText } from "./harness.mjs";
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
/** A shelf whose selection lives outside it, as the library keeps it across pages. */
function Shelf(props) {
  const [selection, setSelection] = React.useState(noSelection);
  return React.createElement(BookShelf, {
    browse: { query: "a" },
    busy: false,
    t,
    open() {},
    allUnfinished: null,
    menu: () => [],
    ...props,
    selection: props.selection ?? selection,
    select: props.select ?? setSelection,
  });
}
const shelfBooks = (count) =>
  Array.from({ length: count }, (_, i) => ({
    id: String(i),
    metadata: { title: `Book ${i}`, language: "en" },
    content: { page_count: 4 },
    personal: { resume_page: null, completed: i === 2, rating: null },
    review_state: "confirmed",
  }));
test("choosing a book starts selecting; Select all takes the page, then every result, then hides", async (context) => {
  context.mock.method(
    globalThis,
    "fetch",
    async () => new Response("", { status: 404 }),
  );
  const books = shelfBooks(3);
  const targets = [];
  let view;
  await act(async () => {
    view = render(
      React.createElement(Shelf, {
        books,
        total: 66,
        menu: (value) => {
          targets.push(value);
          return [{ label: "Probe", action() {} }];
        },
      }),
    );
  });
  context.after(async () => act(async () => view.unmount()));
  // Nothing to switch on first: choosing a book is enough.
  assert.equal(button(view, "Select"), undefined);
  assert.equal(button(view, "Select all"), undefined);
  await click(button(view, "Select Book 0"));
  assert.match(visibleText(view), /1 selected/);
  // Un-choosing the last book ends selecting.
  await click(button(view, "Select Book 0"));
  assert.equal(button(view, "Select all"), undefined);
  await click(button(view, "Select Book 0"));
  await click(button(view, "Select all"));
  assert.match(visibleText(view), /3 selected/);
  await act(async () =>
    dispatch(one(view.container, { "data-book-id": "2" }), "contextMenu"),
  );
  assert.deepEqual(targets.at(-1).bookIds, ["0", "1", "2"]);
  assert.equal(targets.at(-1).unfinished, null);
  await click(button(view, "Select all"));
  assert.match(visibleText(view), /66 selected \(all results\)/);
  assert.equal(button(view, "Select all"), undefined);
  await click(button(view, "Select Book 1"));
  assert.match(visibleText(view), /65 selected/);
  await act(async () =>
    dispatch(one(view.container, { "data-book-id": "0" }), "contextMenu"),
  );
  assert.equal(targets.at(-1).all.total, 66);
  assert.deepEqual(targets.at(-1).all.exclude, ["1"]);
  await click(button(view, "Clear selection"));
  assert.doesNotMatch(visibleText(view), /selected/);
});
test("a book outside the selection gets a menu of its own", async (context) => {
  context.mock.method(
    globalThis,
    "fetch",
    async () => new Response("", { status: 404 }),
  );
  const targets = [];
  let view;
  await act(async () => {
    view = render(
      React.createElement(Shelf, {
        books: shelfBooks(3),
        total: 3,
        menu: (value) => (targets.push(value), []),
      }),
    );
  });
  context.after(async () => act(async () => view.unmount()));
  await click(button(view, "Select Book 0"));
  await act(async () =>
    dispatch(one(view.container, { "data-book-id": "2" }), "contextMenu"),
  );
  assert.deepEqual(targets.at(-1).bookIds, ["2"]);
  // Book 2 is finished, so its own menu offers "mark unread".
  assert.equal(targets.at(-1).unfinished, null);
});
test("Ctrl/⌘-click and middle-click open a book in a new tab instead of selecting it", async (context) => {
  const opened = [];
  context.mock.method(window, "open", (url, target) =>
    opened.push([url, target]),
  );
  context.mock.method(
    globalThis,
    "fetch",
    async () => new Response("", { status: 404 }),
  );
  const books = [
    {
      id: "b1",
      metadata: { title: "Book 1", language: "en" },
      content: { page_count: 4 },
      personal: { resume_page: null, completed: false, rating: null },
      review_state: "confirmed",
    },
  ];
  const visits = [];
  let view;
  await act(async () => {
    view = render(
      React.createElement(BookShelf, {
        books,
        total: 1,
        browse: { query: "" },
        busy: false,
        t,
        open: (id) => visits.push(id),
        selection: noSelection(),
        select() {},
        menu: () => [],
        allUnfinished: null,
      }),
    );
  });
  context.after(async () => {
    await act(async () => view.unmount());
  });
  const card = button(view, "Book 1");
  await act(async () => dispatch(card, "click", { ctrlKey: true }));
  await act(async () => dispatch(card, "click", { metaKey: true }));
  await act(async () =>
    card.dispatchEvent(
      new MouseEvent("auxclick", { bubbles: true, button: 1 }),
    ),
  );
  assert.equal(opened.length, 3);
  assert.ok(
    opened.every(
      ([url, target]) => url.endsWith("#book/b1") && target === "_blank",
    ),
  );
  assert.deepEqual(visits, []);
  assert.equal(
    String(attr(button(view, "Select Book 1"), "aria-pressed")),
    "false",
  );
  await click(card);
  assert.deepEqual(visits, ["b1"]);
});
