import { dispatch, all, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { Collections } from "../src/Collections.tsx";
import { Devices } from "../src/Devices.tsx";
import { button, setupFixture, visibleText } from "./harness.mjs";
async function setup(t, Component, props = {}) {
  const x = await setupFixture(t);
  const guard = { current: null },
    routes = [];
  let exits = 0;
  const view = await x.render(
    React.createElement(Component, {
      t: (_, en) => en,
      guard,
      navigate: async (route) =>
        routes.push({ route, allowed: await guard.current() }),
      logout: () => {
        exits++;
      },
      ...props,
    }),
  );
  return {
    view,
    guard,
    routes,
    fixture: x.fixture,
    button: (text) => button(view, text),
    text: () => visibleText(view),
    exits: () => exits,
  };
}
test("collection drag handle supports keyboard reorder and retains all books", async (t) => {
  const x = await setup(t, Collections, { id: "fixture-series" });
  const handle = x.button("Reorder 海边的慢日子");
  await act(async () =>
    dispatch(handle, "keyDown", { key: "ArrowUp", preventDefault() {} }),
  );
  assert.deepEqual(Array.from(x.fixture.collections[0].book_ids), [
    "fixture-0",
    "fixture-2",
    "fixture-1",
  ]);
  assert.equal(x.fixture.books.length, 6);
});
test("stale collection reorder preserves concurrent edits", async (t) => {
  const x = await setup(t, Collections, { id: "fixture-series" });
  x.fixture.collections[0].revision++;
  x.fixture.collections[0].book_ids.reverse();
  await act(async () =>
    dispatch(x.button("Reorder 海边的慢日子"), "keyDown", {
      key: "ArrowUp",
      preventDefault() {},
    }),
  );
  assert.deepEqual(Array.from(x.fixture.collections[0].book_ids), [
    "fixture-2",
    "fixture-1",
    "fixture-0",
  ]);
  assert.match(x.text(), /Another device changed/);
});
test("web device sign-out is immediate and leaves API keys intact", async (t) => {
  const x = await setup(t, Devices);
  const buttons = all(x.view.container, "button").filter((n) =>
    n.textContent.includes("Sign out"),
  );
  await act(async () => dispatch(buttons[1], "click"));
  assert.ok(x.fixture.credentials[1].revoked_at);
  assert.equal(x.fixture.credentials[2].revoked_at, null);
  assert.equal(all(x.view.container, "dialog").length, 0);
});
async function longCollection(t) {
  const x = await setupFixture(t, (fixture) => {
    const extra = Array.from({ length: 250 }, (_, index) => ({
      ...structuredClone(fixture.books[0]),
      id: `extra-${index}`,
    }));
    fixture.books.push(...extra);
    fixture.collections[0].book_ids.push(...extra.map((book) => book.id));
  });
  const { Notifications, toast } = await import("../src/feedback.tsx");
  toast.dismiss();
  const view = await x.render(
    React.createElement(
      React.Fragment,
      null,
      React.createElement(Collections, {
        id: "fixture-series",
        t: (_, en) => en,
        guard: { current: null },
        navigate: () => {},
      }),
      React.createElement(Notifications, { t: (_, en) => en }),
    ),
  );
  const rows = () =>
    all(view.container, { className: "collection-row" }).length;
  return { x, view, rows, text: () => visibleText(view) };
}
test("collection changes keep every loaded page of books", async (t) => {
  const { view, rows } = await longCollection(t);
  assert.equal(rows(), 200);
  await act(async () => dispatch(button(view, "More books"), "click"));
  assert.equal(rows(), 253);
  await act(async () =>
    dispatch(
      all(view.container, { "aria-label": "Remove from collection" }).find(
        (node) => node.tagName === "BUTTON",
      ),
      "click",
    ),
  );
  assert.equal(rows(), 252);
});
test("moving the last loaded book to an unloaded end explains why nothing happened", async (t) => {
  const { x, view, text } = await longCollection(t);
  const before = Array.from(x.fixture.collections[0].book_ids);
  const handles = all(view.container, "button").filter((node) =>
    attr(node, "className")?.includes("drag-handle"),
  );
  await act(async () =>
    dispatch(handles.at(-1), "keyDown", {
      key: "ArrowDown",
      preventDefault() {},
    }),
  );
  assert.match(text(), /Load all books before moving one to the end/);
  assert.deepEqual(Array.from(x.fixture.collections[0].book_ids), before);
});
