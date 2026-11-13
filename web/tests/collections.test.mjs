import { dispatch, all, attr, one } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { act } from "@testing-library/react";
import { Collections } from "../src/Collections.tsx";
import { Notifications, toast } from "../src/feedback.tsx";
import { click, english, renderPage } from "./harness.mjs";

const collection = (t, options = {}) =>
  renderPage(t, Collections, {
    ...options,
    props: { id: "fixture-series", ...options.props },
  });
const reorder = (x, name, key) =>
  act(async () =>
    dispatch(x.button(`Reorder ${name}`), "keyDown", {
      key,
      preventDefault() {},
    }),
  );

test("collection drag handle supports keyboard reorder and retains all books", async (t) => {
  const x = await collection(t);
  await reorder(x, "海边的慢日子", "ArrowUp");
  assert.deepEqual(Array.from(x.fixture.collections[0].book_ids), [
    "fixture-0",
    "fixture-2",
    "fixture-1",
  ]);
  assert.equal(x.fixture.books.length, 6);
});
test("Escape abandons a collection rename even though the field then blurs", async (t) => {
  const x = await collection(t);
  await x.click("周末慢读");
  const field = one(x.view.container, { "aria-label": "Collection name" });
  await act(async () =>
    dispatch(field, "change", { target: { value: "Renamed" } }),
  );
  // The blur that removing the field causes arrives before React re-renders.
  await act(async () => {
    dispatch(field, "keyDown", { key: "Escape" });
    dispatch(field, "blur");
  });
  assert.equal(x.fixture.collections[0].name, "周末慢读");
  assert.ok(!x.fixture.requests.some((r) => r.method === "PUT"));
});
test("stale collection reorder preserves concurrent edits", async (t) => {
  const x = await collection(t);
  x.fixture.collections[0].revision++;
  x.fixture.collections[0].book_ids.reverse();
  await reorder(x, "海边的慢日子", "ArrowUp");
  assert.deepEqual(Array.from(x.fixture.collections[0].book_ids), [
    "fixture-2",
    "fixture-1",
    "fixture-0",
  ]);
  assert.match(x.text(), /Another device changed/);
});
test("a finished collection's Read again opens its first book at the first page", async (t) => {
  const x = await collection(t, {
    prepare: (fixture) => {
      for (const book of fixture.books)
        book.personal = { ...book.personal, completed: true, resume_page: 7 };
    },
  });
  assert.ok(x.button("Read again"));
  await x.click("Read again");
  assert.deepEqual(x.routes, ["read/fixture-0/in/fixture-series/page/1"]);
});

/** Collection fixture-series with 250 more books, beyond the first page of 200. */
async function longCollection(t) {
  toast.dismiss();
  const x = await collection(t, {
    prepare: (fixture) => {
      const extra = Array.from({ length: 250 }, (_, index) => ({
        ...structuredClone(fixture.books[0]),
        id: `extra-${index}`,
      }));
      fixture.books.push(...extra);
      fixture.collections[0].book_ids.push(...extra.map((book) => book.id));
    },
    wrap: (page) =>
      React.createElement(
        React.Fragment,
        null,
        page,
        React.createElement(Notifications, { t: english }),
      ),
  });
  const rows = () =>
    all(x.view.container, { className: "collection-row" }).length;
  return { ...x, rows };
}
test("collection changes keep every loaded page of books", async (t) => {
  const x = await longCollection(t);
  assert.equal(x.rows(), 200);
  await x.click("More books");
  assert.equal(x.rows(), 253);
  await act(async () =>
    dispatch(
      all(x.view.container, { "aria-label": "Remove from collection" }).find(
        (node) => node.tagName === "BUTTON",
      ),
      "click",
    ),
  );
  assert.equal(x.rows(), 252);
});
test("moving the last loaded book to an unloaded end explains why nothing happened", async (t) => {
  const x = await longCollection(t);
  const before = Array.from(x.fixture.collections[0].book_ids);
  const handles = all(x.view.container, "button").filter((node) =>
    attr(node, "className")?.includes("drag-handle"),
  );
  await act(async () =>
    dispatch(handles.at(-1), "keyDown", {
      key: "ArrowDown",
      preventDefault() {},
    }),
  );
  assert.match(x.text(), /Load all books before moving one to the end/);
  assert.deepEqual(Array.from(x.fixture.collections[0].book_ids), before);
});

/** Another device changes fixture-series: a new revision with a fourth book. */
const changeElsewhere = (x) => {
  const series = x.fixture.collections[0];
  if (!series.book_ids.includes("fixture-3")) series.book_ids.push("fixture-3");
  series.revision++;
};
const reloaded =
  /Another device changed this collection\. The latest version is loaded; please try again\./;
/** Searches "Add books" for `value` as pressing Enter does. */
async function searchBooks(x, value) {
  const box = one(x.view.container, { "aria-label": "Search library" });
  await act(async () => dispatch(box, "change", { target: { value } }));
  await act(async () =>
    dispatch(box.closest("form"), "submit", { preventDefault() {} }),
  );
}
const rowCount = (x) =>
  all(x.view.container, { className: "collection-row" }).length;
test("adding after a change elsewhere reloads at once and the repeat succeeds", async (t) => {
  const x = await collection(t);
  await searchBooks(x, "");
  changeElsewhere(x);
  const add = () =>
    all(x.view.container, { className: "collection-search-row" })
      .map((row) => one(row, "button"))
      .find((node) => !node.disabled);
  const target = add();
  await click(target);
  assert.match(x.text(), reloaded);
  assert.equal(rowCount(x), 4);
  await click(add());
  assert.doesNotMatch(x.text(), reloaded);
  assert.equal(x.fixture.collections[0].book_ids.length, 5);
});
test("removing and reordering after a change elsewhere recover in the page", async (t) => {
  const x = await collection(t);
  changeElsewhere(x);
  const remove = () =>
    all(x.view.container, { "aria-label": "Remove from collection" })[0];
  await click(remove());
  assert.match(x.text(), reloaded);
  assert.equal(rowCount(x), 4);
  await click(remove());
  assert.equal(rowCount(x), 3);
  assert.doesNotMatch(x.text(), reloaded);
  changeElsewhere(x);
  await reorder(x, "海边的慢日子", "ArrowUp");
  assert.match(x.text(), reloaded);
  await reorder(x, "海边的慢日子", "ArrowUp");
  assert.doesNotMatch(x.text(), reloaded);
});
test("a rename that meets a change elsewhere keeps the typed name for the repeat", async (t) => {
  const x = await collection(t);
  await x.click("周末慢读");
  const field = () =>
    one(x.view.container, { "aria-label": "Collection name" });
  await act(async () =>
    dispatch(field(), "change", { target: { value: "My name" } }),
  );
  changeElsewhere(x);
  await act(async () =>
    dispatch(field(), "keyDown", { key: "Enter", nativeEvent: {} }),
  );
  assert.match(x.text(), reloaded);
  assert.equal(attr(field(), "value"), "My name");
  assert.equal(rowCount(x), 4);
  await act(async () =>
    dispatch(field(), "keyDown", { key: "Enter", nativeEvent: {} }),
  );
  assert.equal(x.fixture.collections[0].name, "My name");
});
test("deleting after a change elsewhere reloads and the repeat deletes", async (t) => {
  const x = await collection(t);
  await x.click("Collection actions");
  await x.click("Delete collection");
  changeElsewhere(x);
  const confirm = () =>
    all(document.body, "button").find((node) => node.textContent === "Delete");
  await click(confirm());
  assert.match(document.body.textContent, reloaded);
  assert.equal(x.fixture.collections.length, 1);
  await click(confirm());
  assert.equal(x.fixture.collections.length, 0);
  assert.deepEqual(x.routes, ["collections"]);
});
test("more books after a change elsewhere starts over from the first page", async (t) => {
  const x = await longCollection(t);
  changeElsewhere(x);
  await x.click("More books");
  assert.match(
    x.text(),
    /The collection changed, so it was loaded again from the first page/,
  );
  assert.equal(x.rows(), 200);
  await x.click("More books");
  assert.equal(x.rows(), 254);
});
test("a failed book search shows its own error until a later search succeeds", async (t) => {
  const backend = [];
  const x = await collection(t);
  const fixtureFetch = globalThis.fetch;
  let failing = new Set(["broken", "slow"]);
  let releaseSlow;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    const query = String(path).startsWith("/api/books?")
      ? new URL(path, "http://x").searchParams.get("query")
      : null;
    backend.push(query);
    if (query === "slow")
      await new Promise((resolve) => {
        releaseSlow = resolve;
      });
    if (failing.has(query))
      return new Response(JSON.stringify({ error: "storage_unavailable" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      });
    return fixtureFetch(path, options);
  });
  const search = (value) => searchBooks(x, value);
  await search("broken");
  assert.match(x.text(), /Library storage is unavailable/);
  // The page itself still works: the error belongs to the search.
  assert.equal(rowCount(x), 3);
  await search("slow");
  await search("");
  assert.doesNotMatch(x.text(), /Library storage is unavailable/);
  const found = all(x.view.container, {
    className: "collection-search-row",
  }).length;
  assert.ok(found > 0);
  // The older search failing late does not replace the newer results.
  await act(async () => releaseSlow());
  assert.doesNotMatch(x.text(), /Library storage is unavailable/);
  assert.equal(
    all(x.view.container, { className: "collection-search-row" }).length,
    found,
  );
  failing = new Set();
});

test("a rule conflict reloads its revision so the next save succeeds", async (t) => {
  const x = await collection(t, {
    prepare: (f) => {
      f.collections[0].rule = { query: "", options: {} };
    },
  });
  await x.click("Edit rule");
  x.fixture.collections[0].revision++;
  await act(async () =>
    dispatch(one(x.view.container, ".modal-form"), "submit", {}),
  );
  assert.match(x.text(), reloaded);
  await act(async () =>
    dispatch(one(x.view.container, ".modal-form"), "submit", {}),
  );
  assert.doesNotMatch(x.text(), reloaded);
  assert.equal(x.view.container.querySelector(".modal-form"), null);
});

test("successful mutation followed by a paged reload conflict does not ask to repeat the write", async (t) => {
  const x = await longCollection(t);
  await x.click("More books");
  const fetch = globalThis.fetch;
  let raced = false;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    if (
      !raced &&
      String(path).includes("/api/collections/fixture-series?") &&
      String(path).includes("after=")
    ) {
      raced = true;
      x.fixture.collections[0].revision++;
    }
    return fetch(path, options);
  });
  await act(async () =>
    dispatch(
      all(x.view.container, { "aria-label": "Remove from collection" }).find(
        (n) => n.tagName === "BUTTON",
      ),
      "click",
      {},
    ),
  );
  assert.equal(x.fixture.collections[0].book_ids.length, 252);
  assert.equal(x.rows(), 200);
  assert.doesNotMatch(x.text(), /please try again|Another device changed/);
});

test("a conflict reload repeats the latest add-books search rather than its old query", async (t) => {
  const x = await collection(t);
  await searchBooks(x, "");
  const fetch = globalThis.fetch;
  let release;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    if (options?.method === "POST" && String(path).includes("/items"))
      await new Promise((resolve) => (release = resolve));
    return fetch(path, options);
  });
  changeElsewhere(x);
  const add = all(x.view.container, ".collection-search-row")
    .map((row) => one(row, "button"))
    .find((n) => !n.disabled);
  await click(add);
  const input = one(x.view.container, { "aria-label": "Search library" });
  await act(async () =>
    dispatch(input, "change", { target: { value: "不存在的新查询" } }),
  );
  await act(async () => release());
  assert.equal(attr(input, "value"), "不存在的新查询");
  assert.equal(
    x.fixture.requests
      .filter((r) => r.path === "/api/books")
      .at(-1)
      .query.get("query"),
    "不存在的新查询",
  );
});
