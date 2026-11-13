import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { act, render } from "@testing-library/react";
import { ErrorBoundary, chunkFailure } from "../src/ErrorBoundary.tsx";
import { button, click, english, installGlobals } from "./harness.mjs";

/** A tab's session storage and a `location` whose reloads are counted. */
function browser(t) {
  const stored = new Map();
  const reloads = [];
  const restore = installGlobals({
    sessionStorage: {
      getItem: (key) => stored.get(key) ?? null,
      setItem: (key, value) => stored.set(key, String(value)),
    },
    location: { reload: () => reloads.push(Date.now()) },
  });
  // React logs caught errors; keep the test output to the assertions.
  t.mock.method(console, "error", () => {});
  return { stored, reloads, restore };
}
const missingChunk = () =>
  new TypeError(
    "Failed to fetch dynamically imported module: http://localhost/assets/Settings-old.js",
  );
function Throws({ error }) {
  if (error) throw error;
  return React.createElement("p", null, "Page content");
}
async function show(t, error, props = {}) {
  let view;
  const element = (failure, key) =>
    React.createElement(
      ErrorBoundary,
      { t: english, resetKey: key, ...props },
      React.createElement(Throws, { error: failure }),
    );
  await act(async () => {
    view = render(element(error, "first"));
  });
  t.after(() => view.unmount());
  return {
    view,
    text: () => view.container.textContent,
    rerender: (failure, key) =>
      act(async () => view.rerender(element(failure, key))),
  };
}

test("resource failures are distinguished from rendering errors", () => {
  assert.equal(chunkFailure(missingChunk()), true);
  assert.equal(
    chunkFailure(new TypeError("Importing a module script failed.")),
    true,
  );
  assert.equal(
    chunkFailure(new Error("error loading dynamically imported module")),
    true,
  );
  assert.equal(chunkFailure(new TypeError("x is undefined")), false);
  assert.equal(chunkFailure(null), false);
});

test("a render error shows a page that explains and reloads on request", async (t) => {
  const tab = browser(t);
  const x = await show(t, new TypeError("x is undefined"));
  t.after(tab.restore);
  assert.match(x.text(), /The page hit an error/);
  assert.equal(x.view.container.querySelector("[role=alert]") !== null, true);
  assert.equal(tab.reloads.length, 0);
  await click(button(x.view, "Reload"));
  assert.equal(tab.reloads.length, 1);
});

test("a resource failure stays in the app until explicit reload", async (t) => {
  const tab = browser(t);
  const x = await show(t, missingChunk());
  t.after(tab.restore);
  assert.equal(tab.reloads.length, 0);
  assert.match(x.text(), /Page resources could not load/);
  await click(button(x.view, "Reload"));
  assert.equal(tab.reloads.length, 1);
});

test("without session storage the page asks instead of reloading by itself", async (t) => {
  t.mock.method(console, "error", () => {});
  const reloads = [];
  const restore = installGlobals({
    sessionStorage: {
      getItem: () => {
        throw new Error("denied");
      },
    },
    location: { reload: () => reloads.push(1) },
  });
  const x = await show(t, missingChunk());
  t.after(restore);
  assert.equal(reloads.length, 0);
  assert.match(x.text(), /Page resources could not load/);
});

test("another route renders again and the reader's error offers a way back", async (t) => {
  const tab = browser(t);
  const backs = [];
  const x = await show(t, new Error("broken page"), {
    back: { label: "Back to the book", action: () => backs.push(1) },
  });
  t.after(tab.restore);
  await click(button(x.view, "Back to the book"));
  assert.deepEqual(backs, [1]);
  await x.rerender(null, "second");
  assert.match(x.text(), /Page content/);
});

test("the text mentions going back only when the page offers a back button", async (t) => {
  const tab = browser(t);
  t.after(tab.restore);
  const shell = await show(t, missingChunk());
  assert.doesNotMatch(shell.text(), /go back/);
  assert.equal(shell.view.container.querySelectorAll("button").length, 1);
  const reader = await show(t, missingChunk(), {
    back: { label: "Back to the book", action: () => {} },
  });
  assert.match(reader.text(), /you can also go back first/);
  assert.ok(button(reader.view, "Back to the book"));
});
