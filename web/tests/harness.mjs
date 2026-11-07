// DOM test harness with an offline fixture backend.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { act, render as renderDOM } from "@testing-library/react";
import { dispatch } from "./dom.mjs";

const fixtureSource = readFileSync(
  new URL("./browser-fixture.js", import.meta.url),
  "utf8",
);

/**
 * Defines `values` on globalThis and returns the function that restores the previous
 * globals in reverse order. The caller decides when to restore, because React views must
 * unmount before `window` and `document` disappear.
 */
export function installGlobals(values) {
  const restores = [];
  for (const [name, value] of Object.entries(values)) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {
      value,
      configurable: true,
      writable: true,
    });
    restores.push(() =>
      original
        ? Object.defineProperty(globalThis, name, original)
        : delete globalThis[name],
    );
  }
  return () => restores.reverse().forEach((restore) => restore());
}

/**
 * Boots the fixture backend into `window`, installs it (and any extra `globals`) on
 * globalThis, mocks fetch with the fixture and tears everything down after the test.
 * `prepare(fixture, window)` runs before fetch is mocked so tests can seed state; it may
 * be async. Views created with the returned `render` are unmounted before the globals are
 * restored.
 */
export async function setupFixture(
  t,
  prepare = () => {},
  { window = globalThis.window, globals = {} } = {},
) {
  const views = [];
  const restore = installGlobals({ ...globals, window });
  t.after(async () => {
    for (const view of views.splice(0).reverse())
      await act(async () => view.unmount());
    restore();
  });
  vm.runInNewContext(fixtureSource, {
    window,
    document: {},
    URL,
    Response,
    Blob,
    crypto,
    Date,
    structuredClone,
    setTimeout,
  });
  const fixture = window.fixture;
  fixture.authenticated = true;
  await prepare(fixture, window);
  t.mock.method(globalThis, "fetch", window.fetch);
  return {
    window,
    fixture,
    async render(element, options) {
      let view;
      await act(async () => {
        view = renderDOM(element, options);
      });
      views.push(view);
      return view;
    },
    async unmount(view) {
      const index = views.indexOf(view);
      if (index >= 0) views.splice(index, 1);
      await act(async () => view.unmount());
    },
  };
}

export const visibleText = (view) => view.container.textContent;
export const nodeText = (node) => node.textContent;

export function button(view, text) {
  return [...view.container.querySelectorAll("button")].find(
    (node) =>
      node.textContent.includes(text) ||
      node.getAttribute("aria-label") === text,
  );
}

export function click(node, event = {}) {
  return act(async () => dispatch(node, "click", event));
}

/** Advances mocked timers by `ms` and lets the promise chains they trigger settle. */
export const tick = (t, ms) =>
  act(async () => {
    t.mock.timers.tick(ms);
    await new Promise((resolve) => setImmediate(resolve));
  });
