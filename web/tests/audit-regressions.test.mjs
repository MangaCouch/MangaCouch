import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { act, render, waitFor } from "@testing-library/react";
import {
  SearchBox,
  SEARCH_DEBOUNCE_MS,
  SUGGESTION_DELAY_MS,
} from "../src/SearchBox.tsx";
import { App } from "../src/App.tsx";
import { Collections } from "../src/Collections.tsx";
import { one, dispatch, all } from "./dom.mjs";
import {
  english as t,
  tick,
  button,
  click,
  visibleText,
  setupFixture,
  hashRouting,
  renderPage,
} from "./harness.mjs";

test("external search replacement cancels the old debounced submission", async (c) => {
  c.mock.timers.enable({ apis: ["setTimeout"] });
  c.mock.method(
    globalThis,
    "fetch",
    async () => new Response(JSON.stringify({ items: [] })),
  );
  const submitted = [];
  const props = { value: "original", search: (v) => submitted.push(v), t };
  let view;
  await act(async () => {
    view = render(React.createElement(SearchBox, props));
  });
  c.after(async () => act(async () => view.unmount()));
  await act(async () =>
    dispatch(one(view.container, "input"), "change", {
      target: { value: "old pending draft", selectionStart: 17 },
    }),
  );
  await act(async () =>
    view.rerender(
      React.createElement(SearchBox, {
        ...props,
        value: "external replacement",
      }),
    ),
  );
  assert.equal(one(view.container, "input").value, "external replacement");
  await tick(c, SEARCH_DEBOUNCE_MS + 1);
  assert.deepEqual(submitted, []);
});

test("chips and free text share the server query budget", async (c) => {
  c.mock.timers.enable({ apis: ["setTimeout"] });
  const tag = {
    namespace: "artist",
    value: "blue sky",
    translation: null,
    count: 2,
  };
  c.mock.method(
    globalThis,
    "fetch",
    async () => new Response(JSON.stringify({ items: [tag] })),
  );
  const submitted = [];
  let view;
  await act(async () => {
    view = render(
      React.createElement(SearchBox, {
        value: "",
        search: (v) => submitted.push(v),
        t,
      }),
    );
  });
  c.after(async () => act(async () => view.unmount()));
  await act(async () =>
    dispatch(one(view.container, "input"), "change", {
      target: { value: "blue", selectionStart: 4 },
    }),
  );
  await tick(c, SUGGESTION_DELAY_MS);
  await act(async () =>
    dispatch(one(view.container, "[role=option]"), "click"),
  );
  await act(async () =>
    dispatch(one(view.container, "input"), "change", {
      target: { value: "x".repeat(1024), selectionStart: 1024 },
    }),
  );
  await tick(c, SEARCH_DEBOUNCE_MS + 1);
  assert.equal(submitted.at(-1).length, 1024);
});

test("logo query reset clears select-all without touching unselected books", async (c) => {
  const routing = hashRouting(window, "/?query=Book");
  const x = await setupFixture(
    c,
    (fixture) => {
      const base = structuredClone(fixture.books[0]);
      fixture.books.splice(0);
      for (let n = 0; n < 30; n++)
        fixture.books.push({
          ...structuredClone(base),
          id: `group-a-${n}`,
          metadata: { ...base.metadata, title: `Book ${n}` },
        });
      for (let n = 0; n < 5; n++)
        fixture.books.push({
          ...structuredClone(base),
          id: `group-b-${n}`,
          metadata: { ...base.metadata, title: `Other ${n}` },
        });
    },
    {
      globals: {
        ...routing,
        localStorage: {
          getItem: (key) => (key.endsWith("language") ? "en" : null),
          setItem() {},
        },
      },
    },
  );
  const calls = [];
  const bulk = [];
  const originalFetch = window.fetch;
  c.mock.method(globalThis, "fetch", async (path, opts) => {
    if (path === "/api/books/selection-state")
      calls.push(JSON.parse(opts.body));
    if (path === "/api/bulk-search") bulk.push(JSON.parse(opts.body));
    return originalFetch(path, opts);
  });
  const view = await x.render(React.createElement(App));
  await waitFor(() =>
    assert.equal(all(view.container, ".book-card").length, 24),
  );
  await act(async () =>
    dispatch(document.body, "keyDown", { key: "a", ctrlKey: true }),
  );
  await act(async () =>
    dispatch(document.body, "keyDown", { key: "a", ctrlKey: true }),
  );
  assert.match(visibleText(view), /30 selected \(all results\)/);
  await click(button(view, "MangaCouch"));
  await waitFor(() =>
    assert.doesNotMatch(visibleText(view), /selected \(all results\)/),
  );
  assert.equal(bulk.length, 0);
  assert.ok(
    x.fixture.books
      .filter((b) => b.id.startsWith("group-b"))
      .every((b) => !b.personal.completed),
  );
});

test("a dirty collection blocks leaving and keeps its draft until discard", async (c) => {
  const x = await renderPage(c, Collections);
  await x.click("New collection");
  await act(async () =>
    dispatch(one(x.view.container, "dialog input"), "change", {
      target: { value: "Draft collection name" },
    }),
  );
  await act(async () => assert.equal(await x.guard.current(), false));
  await x.click("Close");
  assert.equal(
    one(x.view.container, "dialog input").value,
    "Draft collection name",
  );
  await x.click("Keep editing");
  await x.click("Close");
  await x.click("Discard changes");
  await x.click("New collection");
  assert.equal(one(x.view.container, "dialog input").value, "");
});
