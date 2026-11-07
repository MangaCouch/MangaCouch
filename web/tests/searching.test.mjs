import { all, attr, dispatch, one } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { App } from "../src/App.tsx";
import { storageKey } from "../src/device.ts";
import { button, click, setupFixture, visibleText } from "./harness.mjs";
async function setup(t, withoutUUID = false) {
  const window = globalThis.window,
    document = globalThis.document;
  window.matchMedia = () =>
    Object.assign(new EventTarget(), { matches: false });
  document.hidden = false;
  let hash = "";
  const location = {
    pathname: "/",
    search: "",
    get hash() {
      return hash;
    },
    set hash(value) {
      hash = value.startsWith("#") ? value : `#${value}`;
      queueMicrotask(() => window.dispatchEvent(new Event("hashchange")));
    },
  };
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
        history: { replaceState: () => {} },
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
      if (gate) {
        const captured = gate;
        gate = null;
        return captured;
      }
    }
    return fixtureFetch(path, options);
  });
  const view = await x.render(React.createElement(App));
  const wait = async (predicate) => {
    for (let i = 0; i < 80; i++) {
      if (predicate()) return;
      await act(async () => new Promise((r) => setTimeout(r, 5)));
    }
    assert.fail("UI did not settle");
  };
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
test("random order seed survives pagination and detail navigation until explicit shuffle", async (t) => {
  const x = await setup(t);
  await x.click("Random");
  await x.wait(() => x.cards().length === 24);
  const seed = x.last().get("seed"),
    first = one(x.cards()[0], "strong").textContent;
  assert.match(seed, /^[a-f0-9]{32}$/);
  await x.click("Next page");
  await x.wait(() => x.cards().length === 11);
  assert.equal(x.last().get("seed"), seed);
  await act(async () => dispatch(x.cards()[0], "click", {}));
  await x.wait(() => !!x.button("Library"));
  await x.click("Library");
  await x.wait(() => x.cards().length === 24);
  assert.equal(x.last().get("seed"), seed);
  assert.equal(one(x.cards()[0], "strong").textContent, first);
  await x.click("Shuffle again");
  await x.wait(() => x.cards().length === 24);
  assert.notEqual(x.last().get("seed"), seed);
  assert.equal(x.last().has("after_id"), false);
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
test("a continue-reading failure clears once a later refresh succeeds", async (t) => {
  const { Library } = await import("../src/Library.tsx");
  const { initialBrowse } = await import("../src/LibraryFilters.tsx");
  const window = globalThis.window;
  const x = await setupFixture(t, () => {}, {
    window,
    globals: { document: globalThis.document },
  });
  let failContinue = true;
  const fixtureFetch = x.window.fetch;
  t.mock.method(globalThis, "fetch", async (path, options) =>
    path.includes("reading=in_progress") && failContinue
      ? new Response("{}", { status: 500 })
      : fixtureFetch(path, options),
  );
  const props = {
    t: (_, en) => en,
    guard: { current: null },
    navigate: () => {},
    browse: initialBrowse(),
    onBrowse: () => {},
    onOrganize: () => {},
    selectRequested: false,
    onSelectionStarted: () => {},
    version: 0,
  };
  const view = await x.render(React.createElement(Library, props));
  assert.match(visibleText(view), /Continue reading could not be loaded/);
  failContinue = false;
  await act(async () =>
    view.rerender(React.createElement(Library, { ...props, version: 1 })),
  );
  assert.doesNotMatch(visibleText(view), /could not be loaded/);
});
