import assert from "node:assert/strict";
import test from "node:test";
import { storageKey } from "../src/device.ts";
import { initialBrowse } from "../src/LibraryFilters.tsx";
import {
  forgetRetiredLibraryStorage,
  rememberLibraryView,
  restoreLibraryView,
} from "../src/libraryView.ts";
import { hashRouting, installGlobals } from "./harness.mjs";

function browser(t) {
  const routing = hashRouting(window);
  t.after(installGlobals(routing));
  return routing;
}
const unread = {
  ...initialBrowse(),
  query: "artist:lin",
  reading: "unread",
  language: "japanese",
  rating: "4.5",
  order: "random",
};

test("a reloaded URL gets its whole library view back", (t) => {
  browser(t);
  const scroll = { query: "q&page=3", y: 840 };
  rememberLibraryView({ browse: unread, page: 3, scroll });
  assert.deepEqual(restoreLibraryView(), {
    browse: { ...unread, category: undefined },
    page: 3,
    scroll,
  });
});

test("a plain library URL has default filters independent of old device storage", (t) => {
  browser(t);
  localStorage.setItem(storageKey("library-filters"), JSON.stringify(unread));
  sessionStorage.setItem(
    storageKey("library-view"),
    JSON.stringify({ browse: unread, page: 3 }),
  );
  const view = restoreLibraryView();
  assert.equal(view.browse.reading, "all");
  assert.equal(view.browse.query, "");
  assert.equal(view.browse.order, "default");
  assert.equal(view.page, 1);
  assert.equal(view.scroll, null);
});

test("startup removes the retired filter keys and keeps reader settings", (t) => {
  browser(t);
  localStorage.setItem(storageKey("library-filters"), JSON.stringify(unread));
  sessionStorage.setItem(
    storageKey("library-view"),
    JSON.stringify({ page: 3 }),
  );
  localStorage.setItem(storageKey("reading-mode"), "scroll");
  localStorage.setItem(storageKey("fit"), "width");
  sessionStorage.setItem(storageKey("navigation"), JSON.stringify([""]));
  forgetRetiredLibraryStorage();
  assert.equal(localStorage.getItem(storageKey("library-filters")), null);
  assert.equal(sessionStorage.getItem(storageKey("library-view")), null);
  assert.equal(localStorage.getItem(storageKey("reading-mode")), "scroll");
  assert.equal(localStorage.getItem(storageKey("fit")), "width");
  assert.equal(
    sessionStorage.getItem(storageKey("navigation")),
    JSON.stringify([""]),
  );
});

test("removing retired keys tolerates unavailable storage", (t) => {
  const old = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new DOMException("blocked", "SecurityError");
    },
  });
  t.after(() =>
    old
      ? Object.defineProperty(globalThis, "localStorage", old)
      : delete globalThis.localStorage,
  );
  assert.doesNotThrow(() => forgetRetiredLibraryStorage());
});

test("invalid URL values and damaged history state use defaults", () => {
  const view = restoreLibraryView(
    "?reading=maybe&category=Novel&language=Klingon&source=eh&rating=9&sourceRating=0.25&order=sideways&seed=bad&page=-2",
    { mcScroll: { query: "x", y: "far" } },
  );
  assert.deepEqual(
    { ...view.browse, seed: "" },
    {
      ...initialBrowse(),
      seed: "",
      category: undefined,
      language: undefined,
    },
  );
  assert.equal(view.page, 1);
  assert.equal(view.scroll, null);
});

test("1024 character searches round trip without truncation", (t) => {
  browser(t);
  const query = "汉".repeat(1024);
  rememberLibraryView({ browse: { ...unread, query }, page: 1, scroll: null });
  assert.equal(restoreLibraryView().browse.query, query);
  assert.equal(
    restoreLibraryView("?query=" + "x".repeat(1025), null).browse.query,
    "",
  );
});
