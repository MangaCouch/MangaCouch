// DOM test harness with an in-memory fake backend.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import React from "react";
import { act, render as renderDOM } from "@testing-library/react";
import { dispatch } from "./dom.mjs";

const fixtureSource = readFileSync(
  new URL("./fake-backend.js", import.meta.url),
  "utf8",
);

/** The translator the tests render with: every string in English. */
export const english = (_zh, en) => en;

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
 * Boots the fake backend (fake-backend.js) into `window`, installs it (and any extra `globals`) on
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
    URL,
    URLSearchParams,
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

/**
 * Boots the fake backend (see `setupFixture`, which takes `prepare`, `window` and
 * `globals`) and renders `Component` with the props pages share: the English translator,
 * a navigation `guard` and a `navigate` that records each route in `routes`. `props` add
 * to or replace those, `fetch` replaces the fake backend's answers and `wrap(page)` may
 * render more around the page. Returns the fixture context with shortcuts for the view.
 */
export async function renderPage(
  t,
  Component,
  { props = {}, prepare, fetch, wrap = (page) => page, ...options } = {},
) {
  const x = await setupFixture(t, prepare, options);
  if (fetch) t.mock.method(globalThis, "fetch", fetch);
  const guard = { current: null },
    routes = [];
  const view = await x.render(
    wrap(
      React.createElement(Component, {
        t: english,
        guard,
        navigate: (route) => routes.push(route),
        ...props,
      }),
    ),
  );
  return {
    ...x,
    view,
    guard,
    routes,
    text: () => visibleText(view),
    button: (text) => button(view, text),
    click: (text) => click(button(view, text)),
    tick: (ms) => tick(t, ms),
    unmount: () => x.unmount(view),
  };
}

/**
 * A hash-routed `location` and `history` for rendering the whole App: setting the hash
 * fires `hashchange` as a browser does, `replaceState` rewrites it silently and
 * `history.go` moves by its requested offset and restores that entry's state.
 */
export function hashRouting(window, start = "") {
  const entries = [
    { url: new URL(start || "/", "http://localhost/"), state: null },
  ];
  let position = 0;
  const changed = () =>
    queueMicrotask(() => {
      window.dispatchEvent(new Event("popstate"));
      window.dispatchEvent(new Event("hashchange"));
    });
  const location = {
    get href() {
      return entries[position].url.href;
    },
    get pathname() {
      return entries[position].url.pathname;
    },
    get search() {
      return entries[position].url.search;
    },
    get hash() {
      return entries[position].url.hash;
    },
    set hash(value) {
      const url = new URL(this.href);
      url.hash = value;
      if (url.href === this.href) return;
      entries.splice(position + 1);
      entries.push({ url, state: null });
      position++;
      changed();
    },
  };
  const history = {
    get state() {
      return entries[position].state;
    },
    get length() {
      return entries.length;
    },
    go(offset = 0) {
      const next = position + offset;
      if (next < 0 || next >= entries.length || next === position) return;
      position = next;
      changed();
    },
    replaceState(state, _title, url = location.href) {
      entries[position] = { state, url: new URL(url, location.href) };
    },
    pushState(state, _title, url = location.href) {
      const next = { state, url: new URL(url, location.href) };
      entries.splice(position + 1);
      entries.push(next);
      position++;
    },
  };
  return { location, history };
}

export const visibleText = (view) => view.container.textContent;
export const nodeText = (node) => node.textContent;

export function button(view, text) {
  return [...view.container.querySelectorAll("button, a[href]")].find(
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
