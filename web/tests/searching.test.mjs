import { all, attr, dispatch, one } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { act, render, waitFor } from "@testing-library/react";
import { App } from "../src/App.tsx";
import { LibraryFilters, initialBrowse } from "../src/LibraryFilters.tsx";
import { storageKey } from "../src/device.ts";
import {
  button,
  click,
  english,
  hashRouting,
  renderPage,
  setupFixture,
  visibleText,
} from "./harness.mjs";
async function setup(t, withoutUUID = false, prepare = () => {}) {
  const window = globalThis.window,
    document = globalThis.document;
  window.matchMedia = () =>
    Object.assign(new EventTarget(), { matches: false });
  document.hidden = false;
  const { location, history } = hashRouting(window);
  const x = await setupFixture(
    t,
    (fixture) => {
      const template = fixture.books[0];
      for (let i = 6; i < 35; i++)
        fixture.books.push({
          ...structuredClone(template),
          id: `fixture-${i}`,
          metadata: { ...template.metadata, title: `Book ${i}` },
        });
      prepare(fixture);
    },
    {
      window,
      globals: {
        ...(withoutUUID
          ? {
              crypto: {
                getRandomValues: crypto.getRandomValues.bind(crypto),
                subtle: crypto.subtle,
              },
            }
          : {}),
        document,
        location,
        history,
        localStorage: {
          getItem: (key) => (key === storageKey("language") ? "en" : null),
          setItem: () => {},
        },
      },
    },
  );
  const calls = [];
  let gate = null;
  const fixtureFetch = x.window.fetch;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    if (path.startsWith("/api/books?")) {
      calls.push(new URL(path, "http://fixture"));
      if (gate && path.includes("&page=")) {
        const captured = gate;
        gate = null;
        return captured;
      }
    }
    return fixtureFetch(path, options);
  });
  const view = await x.render(React.createElement(App));
  /** Waits until `predicate` holds: a response takes several event-loop turns to show. */
  const wait = (predicate) => waitFor(() => assert.ok(predicate()));
  const cards = () => all(view.container, { className: "book-card" });
  await wait(() => cards().length === 24);
  return {
    view,
    window,
    location,
    calls,
    cards,
    button: (name) => button(view, name),
    wait,
    click: (name) => click(button(view, name)),
    change: async (label, value) =>
      act(async () =>
        dispatch(
          all(view.container, "*").find(
            (n) =>
              (n.tagName === "INPUT" || n.tagName === "SELECT") &&
              attr(n, "aria-label") === label,
          ),
          "change",
          { target: { value } },
        ),
      ),
    last: () => calls.at(-1).searchParams,
    text: () => visibleText(view),
    /** The next book list request fails with 503 storage_unavailable. */
    failNext: () => {
      gate = Promise.resolve(unavailable());
    },
    delayNext: () => {
      let resolve;
      gate = new Promise((yes) => (resolve = yes));
      return resolve;
    },
  };
}
test("filters reset paging and send personal/source ratings independently", async (t) => {
  const x = await setup(t);
  await x.click("Next page");
  await x.wait(() => x.cards().length === 11);
  assert.equal(x.last().get("page"), "2");
  await x.click("Rated");
  await act(async () =>
    dispatch(
      all(x.view.container, "input")
        .filter((n) => attr(n, "aria-label") === "My rating")
        .at(-1),
      "change",
      { target: { value: "4.5" } },
    ),
  );
  await x.wait(() => x.cards().length === 1);
  assert.equal(x.last().has("after_id"), false);
  assert.equal(x.last().get("rating"), "rated");
  assert.equal(x.last().get("min_rating"), "4.5");
  await act(async () =>
    dispatch(
      all(x.view.container, "input").find(
        (n) => attr(n, "aria-label") === "Minimum source rating",
      ),
      "change",
      { target: { value: "4" } },
    ),
  );
  await x.wait(() => x.cards().length === 0);
  assert.equal(x.last().get("source_rating_min"), "4");
  await x.click("Clear filters");
  await x.wait(() => x.cards().length === 24);
  assert.equal(x.last().get("min_rating"), null);
  assert.equal(x.last().get("source_rating_min"), null);
});
test("random order seed and the page survive detail navigation until explicit shuffle", async (t) => {
  const x = await setup(t);
  await x.click("Random");
  await x.wait(() => x.cards().length === 24);
  const seed = x.last().get("seed"),
    first = one(x.cards()[0], "strong").textContent;
  assert.match(seed, /^[a-f0-9]{32}$/);
  await x.click("Next page");
  await x.wait(() => x.cards().length === 11);
  assert.equal(x.last().get("seed"), seed);
  const second = one(x.cards()[0], "strong").textContent;
  await act(async () => dispatch(x.cards()[0], "click", {}));
  await x.wait(() => !!x.button("Library"));
  await x.click("Library");
  // Back from the book, the shelf is still on page 2 of the same random order.
  await x.wait(() => x.cards().length === 11);
  assert.equal(x.last().get("seed"), seed);
  assert.equal(x.last().get("page"), "2");
  assert.equal(one(x.cards()[0], "strong").textContent, second);
  await x.click("Previous page");
  await x.wait(() => x.cards().length === 24);
  assert.equal(one(x.cards()[0], "strong").textContent, first);
  await x.click("Shuffle again");
  await x.wait(() => x.cards().length === 24);
  assert.notEqual(x.last().get("seed"), seed);
  assert.equal(x.last().has("after_id"), false);
});
test("reloading the tab restores the shelf's search, filters, random order and page", async (t) => {
  const x = await setup(t);
  await x.click("Random");
  await x.wait(() => x.cards().length === 24);
  const seed = x.last().get("seed");
  await x.click("Next page");
  await x.wait(() => x.cards().length === 11);
  // A discarded background tab or a relaunched page starts the app again from storage.
  let view = x.view;
  const reload = () => {
    view.unmount();
    x.calls.length = 0;
    view = render(React.createElement(App));
  };
  const cards = () => all(view.container, { className: "book-card" });
  const shelf = () =>
    x.calls.filter((url) => url.searchParams.has("page")).at(-1)?.searchParams;
  reload();
  await x.wait(() => cards().length === 11);
  assert.equal(shelf().get("order"), "random");
  assert.equal(shelf().get("seed"), seed);
  assert.equal(shelf().get("page"), "2");
  await click(button(view, "Unread"));
  await x.wait(() => shelf()?.get("reading") === "unread");
  reload();
  await x.wait(() => !!shelf());
  assert.equal(shelf().get("reading"), "unread");
  assert.equal(shelf().get("seed"), seed);
  assert.equal(shelf().get("page"), "1");
  view.unmount();
});
test("back from a book the shelf returns to its page and scroll position; a tag search starts over", async (t) => {
  const x = await setup(t, false, (fixture) => {
    for (const book of fixture.books)
      book.metadata.tags = [{ namespace: "female", value: "glasses" }];
  });
  const scrolls = [];
  t.mock.method(x.window, "scrollTo", (left, top) => scrolls.push([left, top]));
  const scrollTo = async (y) => {
    Object.defineProperty(x.window, "scrollY", {
      configurable: true,
      value: y,
    });
    await act(async () => x.window.dispatchEvent(new Event("scroll")));
  };
  t.after(() => delete x.window.scrollY);
  await x.click("Next page");
  await x.wait(() => x.cards().length === 11);
  await scrollTo(640);
  await act(async () => dispatch(x.cards()[3], "click", {}));
  await x.wait(() => !!x.button("Library"));
  // Leaving shrinks the page; that is not where the reader was.
  await scrollTo(0);
  await x.click("Library");
  await x.wait(() => x.cards().length === 11);
  assert.equal(x.last().get("page"), "2");
  assert.deepEqual(scrolls.at(-1), [0, 640]);
  // A tag search from a book opens the first page of its results, unscrolled.
  await act(async () => dispatch(x.cards()[0], "click", {}));
  await x.wait(() => !!x.button("Library"));
  await act(async () =>
    dispatch(
      all(x.view.container, "button").find((node) =>
        attr(node, "className")?.includes("tag-pill"),
      ),
      "click",
      {},
    ),
  );
  await x.wait(() => !!x.last().get("query")?.includes("glasses"));
  assert.equal(x.last().get("page"), "1");
  assert.deepEqual(scrolls.at(-1), [0, 0]);
});
test("late results from an older filter do not replace the current shelf", async (t) => {
  const x = await setup(t),
    release = x.delayNext();
  await x.click("Finished");
  await x.click("Unrated");
  await x.wait(() => x.text().includes("No matching books"));
  await act(async () =>
    release({
      ok: true,
      json: async () => ({
        books: [x.window.fixture.books[0]],
        after_id: null,
      }),
    }),
  );
  assert.equal(x.cards().length, 0);
  assert.equal(x.last().get("reading"), "completed");
});
test("search submission preserves filters and random seed while resetting its cursor", async (t) => {
  const x = await setup(t);
  await x.click("Random");
  await x.wait(() => x.cards().length === 24);
  const seed = x.last().get("seed");
  await x.click("Unread");
  await x.wait(() => x.cards().length === 24);
  await x.change("Search library", "山间");
  await act(async () =>
    dispatch(
      one(x.view.container, { className: "search search-combobox" }),
      "submit",
      { preventDefault() {} },
    ),
  );
  await x.wait(() => x.cards().length === 1);
  assert.equal(x.last().get("query"), "山间");
  assert.equal(x.last().get("reading"), "unread");
  assert.equal(x.last().get("seed"), seed);
  assert.equal(x.last().has("after_id"), false);
});
test("browse works when crypto exposes random bytes but no secure-context randomUUID", async (t) => {
  const x = await setup(t, true);
  assert.equal(crypto.randomUUID, undefined);
  await x.click("Random");
  await x.wait(() => x.cards().length === 24);
  assert.match(x.last().get("seed"), /^[a-f0-9]{32}$/);
});
test("a continue-reading failure clears once a retry succeeds", async (t) => {
  const { Library } = await import("../src/Library.tsx");
  let failContinue = true;
  const x = await renderPage(t, Library, {
    globals: { document: globalThis.document },
    props: {
      browse: initialBrowse(),
      onBrowse: () => {},
      page: 1,
      onPage: () => {},
      scroll: { current: null },
    },
    prepare: (fixture, window) => {
      const fixtureFetch = window.fetch;
      window.fetch = async (path, options) =>
        path.includes("reading=in_progress") && failContinue
          ? new Response("{}", { status: 500 })
          : fixtureFetch(path, options);
    },
  });
  assert.match(x.text(), /Continue reading could not be loaded/);
  failContinue = false;
  await act(async () =>
    dispatch(
      all(x.view.container, "button").find((n) => n.textContent === "Retry"),
      "click",
    ),
  );
  assert.doesNotMatch(x.text(), /could not be loaded/);
});
test("reading and rating segments toggle off instead of offering an explicit 'all'", async (t) => {
  const x = await setup(t);
  const pressed = (group) =>
    all(x.view.container, "button")
      .filter((n) => n.closest(`[aria-label="${group}"]`))
      .filter((n) => attr(n, "aria-pressed") === true)
      .map((n) => n.textContent);
  assert.equal(x.button("All"), undefined);
  assert.deepEqual(pressed("Reading status"), []);
  await x.click("Unread");
  await x.wait(() => x.last().get("reading") === "unread");
  assert.deepEqual(pressed("Reading status"), ["Unread"]);
  await x.click("Unread");
  await x.wait(() => x.last().get("reading") === "all");
  assert.deepEqual(pressed("Reading status"), []);
  await x.click("Rated");
  await x.wait(() => x.last().get("rating") === "rated");
  await act(async () =>
    dispatch(
      all(x.view.container, "input")
        .filter((n) => attr(n, "aria-label") === "My rating")
        .at(-1),
      "change",
      { target: { value: "3" } },
    ),
  );
  await x.wait(() => x.last().get("min_rating") === "3");
  assert.deepEqual(pressed("My rating"), ["Rated"]);
  // Lifting the pressed segment also drops the threshold that hung off it.
  await x.click("Rated");
  await x.wait(() => x.last().get("rating") === "all");
  assert.equal(x.last().has("min_rating"), false);
  assert.deepEqual(pressed("My rating"), []);
});
test("the detail page links the source and a newer version beside the reading state", async (t) => {
  const source = { site: "ehentai", gallery_id: 100, gallery_token: "abcdef" };
  const x = await setup(t, false, (fixture) => {
    fixture.books[0].metadata.source = source;
    fixture.books[0].metadata.newer_versions = [
      { ...source, gallery_id: 101, gallery_token: "fedcba" },
    ];
  });
  const card = x.cards().find((n) => n.textContent.includes("山间来信"));
  await act(async () => dispatch(card, "click", {}));
  await x.wait(() => !!x.button("Library"));
  const badges = one(x.view.container, { className: "detail-badges" });
  const links = all(badges, "a").map((n) => [n.textContent, attr(n, "href")]);
  assert.deepEqual(links, [
    ["E(x)Hentai", "https://e-hentai.org/g/100/abcdef/"],
    ["Newer version", "https://e-hentai.org/g/101/fedcba/"],
  ]);
  assert.ok(badges.textContent.includes("Unread"));
});
test("filter helper buttons never submit an enclosing form", async () => {
  let view;
  const browse = { ...initialBrowse(), order: "random", reading: "unread" };
  await act(async () => {
    view = render(
      React.createElement(
        "form",
        null,
        React.createElement(LibraryFilters, {
          value: browse,
          change() {},
          t: english,
        }),
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

test("selecting every result then excluding the only unfinished book offers mark unread", async (t) => {
  const x = await setup(t, false, (fixture) => {
    for (const book of fixture.books)
      book.personal.completed = book.id !== "fixture-0";
  });
  await act(async () => {
    dispatch(document, "keyDown", { key: "a", ctrlKey: true });
  });
  await act(async () => {
    dispatch(document, "keyDown", { key: "a", ctrlKey: true });
  });
  const unfinished = one(x.view.container, '[data-book-id="fixture-0"]');
  await click(one(unfinished, { className: "book-select" }));
  const finished = one(x.view.container, '[data-book-id="fixture-1"]');
  await act(async () =>
    dispatch(finished, "contextMenu", { clientX: 20, clientY: 20 }),
  );
  await x.wait(() => x.text().includes("Mark as unread"));
  assert.doesNotMatch(x.text(), /Mark as read\b/);
});
test("removing the selected book ends selecting instead of keeping it chosen", async (t) => {
  const x = await setup(t);
  const card = () => one(x.view.container, '[data-book-id="fixture-0"]');
  await click(one(card(), { className: "book-select" }));
  assert.match(x.text(), /1 selected/);
  await act(async () =>
    dispatch(card(), "contextMenu", { clientX: 20, clientY: 20 }),
  );
  await x.wait(() => !!x.button("Remove…"));
  await x.click("Remove…");
  const confirm = () =>
    all(x.view.container, "button").find(
      (node) => node.textContent === "Remove",
    );
  await x.wait(() => confirm() && !confirm().disabled);
  await click(confirm());
  await x.wait(
    () =>
      all(x.view.container, '[data-book-id="fixture-0"]').length === 0 &&
      x.cards().length === 24,
  );
  assert.doesNotMatch(x.text(), /selected/);
  assert.equal(
    all(x.view.container, { className: "selection-toolbar" }).length,
    0,
  );
});

const unavailable = () =>
  new Response(JSON.stringify({ error: "storage_unavailable" }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });
const selectAllKeys = () =>
  act(async () => {
    dispatch(document, "keyDown", { key: "a", ctrlKey: true });
  });
test("a failed filter shows its error and a retry, not the previous results", async (t) => {
  const x = await setup(t);
  assert.match(x.text(), /35 books/);
  x.failNext();
  await x.click("Finished");
  await x.wait(() => x.text().includes("Library storage is unavailable"));
  assert.equal(x.cards().length, 0);
  assert.doesNotMatch(x.text(), /35 books/);
  assert.doesNotMatch(x.text(), /from earlier/);
  // Nothing of the old query can be chosen, one by one or all at once.
  assert.equal(all(x.view.container, { className: "book-select" }).length, 0);
  await selectAllKeys();
  await selectAllKeys();
  assert.doesNotMatch(x.text(), /selected/);
  await x.click("Retry");
  await x.wait(() => !x.text().includes("Library storage is unavailable"));
  assert.equal(x.last().get("reading"), "completed");
  assert.match(x.text(), /No matching books/);
});
test("a failed search or page change shows no books of the previous query", async (t) => {
  const x = await setup(t);
  x.failNext();
  await x.click("Next page");
  await x.wait(() => x.text().includes("Library storage is unavailable"));
  assert.equal(x.cards().length, 0);
  assert.equal(x.last().get("page"), "2");
  await x.click("Retry");
  await x.wait(() => x.cards().length === 11);
  assert.equal(x.last().get("page"), "2");
  x.failNext();
  const box = one(x.view.container, { "aria-label": "Search library" });
  await act(async () =>
    dispatch(box, "change", { target: { value: "Book 7" } }),
  );
  await act(async () =>
    dispatch(box, "keyDown", { key: "Enter", nativeEvent: {} }),
  );
  await x.wait(() => x.text().includes("Library storage is unavailable"));
  assert.equal(x.cards().length, 0);
  assert.doesNotMatch(x.text(), /35 books/);
});
test("a failed refresh of the same page keeps its books, marked as earlier results", async (t) => {
  const x = await setup(t);
  const card = one(x.view.container, '[data-book-id="fixture-1"]');
  await act(async () =>
    dispatch(card, "contextMenu", { clientX: 20, clientY: 20 }),
  );
  await x.wait(() => !!x.button("Refresh thumbnails"));
  x.failNext();
  await x.click("Refresh thumbnails");
  await x.wait(() => x.text().includes("Library storage is unavailable"));
  assert.match(x.text(), /Refreshing failed; the books below are from earlier/);
  assert.equal(x.cards().length, 24);
  assert.match(x.text(), /35 books/);
});

test("Alt navigation and focused links do not page the library", async (t) => {
  const x = await setup(t);
  await act(async () =>
    dispatch(document, "keyDown", { key: "ArrowRight", altKey: true }),
  );
  assert.equal(x.last().get("page"), "1");
  const link = x.view.container.querySelector("a");
  await act(async () => dispatch(link, "keyDown", { key: "ArrowRight" }));
  assert.equal(x.last().get("page"), "1");
  await act(async () => dispatch(document, "keyDown", { key: "ArrowRight" }));
  await x.wait(() => x.last().get("page") === "2");
});
