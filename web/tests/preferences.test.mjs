import { one, dispatch, all, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { act } from "@testing-library/react";
import { PreferenceSession, useBookTitle } from "../src/PreferenceContext.tsx";
import { Preferences } from "../src/PreferenceSettings.tsx";
import { defaultDevice, readDevice, storageKey } from "../src/device.ts";
import { title } from "../src/types.ts";
import { english, installGlobals, renderPage } from "./harness.mjs";
function storage(values = {}, fail = false) {
  const data = new Map(Object.entries(values));
  const localStorage = {
    getItem: (key) => {
      if (fail) throw Error("blocked");
      return data.get(key) ?? null;
    },
    setItem: (key, value) => {
      if (fail) throw Error("quota");
      data.set(key, value);
    },
  };
  return { data, localStorage };
}
test("each device reads its own reader settings; missing or invalid ones take the defaults", (t) => {
  const { data, localStorage } = storage({
    [storageKey("direction")]: "rtl",
    [storageKey("cover-solo")]: "false",
    [storageKey("reading-mode")]: "bad",
  });
  t.after(installGlobals({ localStorage }));
  assert.deepEqual(readDevice(), {
    ...defaultDevice,
    direction: "rtl",
    cover_solo: false,
  });
  assert.equal(data.size, 3); // reading never writes
});
test("blocked local storage uses the built-in defaults", (t) => {
  t.after(installGlobals({ localStorage: storage({}, true).localStorage }));
  assert.deepEqual(readDevice(), defaultDevice);
});
test("title preference has deterministic other-title and filename fallback without changing metadata", () => {
  const book = {
    metadata: { title: "Translated", original_title: "原題" },
    content: { original_filename: "fallback.cbz" },
  };
  assert.equal(title(book, "original_title"), "原題");
  assert.equal(title(book), "Translated");
  assert.equal(
    title({ ...book, metadata: { title: "", original_title: "原題" } }),
    "原題",
  );
  assert.equal(
    title(
      { ...book, metadata: { title: "Translated", original_title: "" } },
      "original_title",
    ),
    "Translated",
  );
  assert.equal(
    title({ ...book, metadata: { title: null, original_title: null } }),
    "fallback.cbz",
  );
  assert.deepEqual(book.metadata, {
    title: "Translated",
    original_title: "原題",
  });
});
/** Shows the configured title of a sample book, as the library does. */
function Display() {
  const name = useBookTitle();
  return React.createElement(
    "output",
    null,
    name({
      metadata: { title: "Visible title", original_title: "原題" },
      content: { original_filename: "fallback.cbz" },
    }),
  );
}
async function setup(t, prepare, options = {}) {
  const { data, localStorage } = storage(options.storage, options.blockStorage);
  const x = await renderPage(t, Preferences, {
    prepare,
    globals: { localStorage },
    wrap: (page) =>
      React.createElement(
        PreferenceSession,
        { t: english },
        React.createElement(Display),
        page,
      ),
  });
  return {
    ...x,
    data,
    change: async (label, value) => {
      await act(async () =>
        dispatch(one(x.view.container, { "aria-label": label }), "change", {
          target: { value },
        }),
      );
    },
  };
}
test("the title preference saves for every device and leaves reader settings alone", async (t) => {
  const x = await setup(t, () => {}, {
    storage: { [storageKey("reading-mode")]: "spread" },
  });
  await x.change("Title display", "original_title");
  assert.equal(await x.guard.current(), true);
  assert.equal(x.fixture.preferences.revision, 1);
  assert.equal(one(x.view.container, "output").textContent, "原題");
  assert.deepEqual(x.fixture.preferences.settings, {
    title_preference: "original_title",
  });
  assert.deepEqual([...x.data], [[storageKey("reading-mode"), "spread"]]);
  assert.doesNotMatch(x.text(), /New device defaults|Reading mode/);
});
test("a failed preference load can be retried", async (t) => {
  const x = await setup(t, (f) => {
    f.preferencesFailure = true;
    f.preferences.settings.title_preference = "original_title";
  });
  assert.match(one(x.view.container, "output").textContent, /Visible title/);
  x.fixture.preferencesFailure = false;
  await x.click("Retry preferences");
  assert.equal(one(x.view.container, "output").textContent, "原題");
});
test("late preferences after unmount cannot initialize storage", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const x = await setup(t, (f) => {
    f.preferencesDelay = 100;
  });
  assert.equal(x.data.size, 0);
  await x.unmount();
  await act(async () => t.mock.timers.tick(100));
  assert.equal(x.data.size, 0);
});
test("autosave conflict reloads current server preferences without overwriting them", async (t) => {
  const x = await setup(t);
  x.fixture.preferences.revision++;
  x.fixture.preferences.settings.title_preference = "original_title";
  await x.change("Title display", "title");
  assert.match(x.text(), /Another device changed/);
  assert.equal(
    attr(one(x.view.container, { "aria-label": "Title display" }), "value"),
    "original_title",
  );
  assert.equal(await x.guard.current(), true);
});
test("lost autosave response is reconciled by reading the server", async (t) => {
  const x = await setup(t, (f) => {
    f.preferencesLoseResponse = true;
  });
  await x.change("Title display", "original_title");
  assert.equal(x.fixture.preferences.revision, 1);
  assert.equal(one(x.view.container, "output").textContent, "原題");
  assert.match(x.text(), /Connection failed/);
});
test("pending autosave blocks leaving and duplicate mutations", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const x = await setup(t);
  x.fixture.preferencesDelay = 100;
  await x.change("Title display", "original_title");
  await x.change("Title display", "title");
  assert.equal(await x.guard.current(), false);
  assert.equal(x.fixture.requests.filter((r) => r.method === "PUT").length, 1);
  await act(async () => t.mock.timers.tick(100));
  assert.equal(await x.guard.current(), true);
});
test("unavailable browser storage still permits shared autosave", async (t) => {
  const x = await setup(t, () => {}, { blockStorage: true });
  await x.change("Title display", "original_title");
  assert.equal(
    x.fixture.preferences.settings.title_preference,
    "original_title",
  );
  assert.equal(x.button("Save shared preferences"), undefined);
});
test("the app renders with local defaults while shared preferences load", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const x = await setup(t, (f) => {
    f.preferencesDelay = 100;
    f.preferences.settings.title_preference = "original_title";
  });
  assert.match(x.text(), /Visible title/);
  assert.doesNotMatch(x.text(), /Loading reading preferences/);
  await act(async () => t.mock.timers.tick(100));
  assert.match(x.text(), /原題/);
});
