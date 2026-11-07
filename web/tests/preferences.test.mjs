import { one, dispatch, all, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { PreferenceSession, useBookTitle } from "../src/PreferenceContext.tsx";
import { Preferences } from "../src/PreferenceSettings.tsx";
import {
  defaultDevice,
  initializeDevice,
  readDevice,
  storageKey,
} from "../src/device.ts";
import { title } from "../src/types.ts";
import {
  button,
  click,
  installGlobals,
  setupFixture,
  visibleText,
} from "./harness.mjs";
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
test("device initialization fills only missing or invalid fields and keeps stored values", (t) => {
  const { data, localStorage } = storage({
    [storageKey("direction")]: "rtl",
    [storageKey("cover-solo")]: "false",
    [storageKey("reading-mode")]: "bad",
  });
  t.after(installGlobals({ localStorage }));
  const values = initializeDevice({
    ...defaultDevice,
    reading_mode: "spread",
    autoplay_seconds: 12,
  });
  assert.deepEqual(values, {
    ...defaultDevice,
    reading_mode: "spread",
    direction: "rtl",
    cover_solo: false,
    autoplay_seconds: 12,
  });
  assert.deepEqual([...data].sort(), [
    [storageKey("autoplay-seconds"), "12"],
    [storageKey("cover-solo"), "false"],
    [storageKey("direction"), "rtl"],
    [storageKey("fit"), "height"],
    [storageKey("reading-mode"), "spread"],
  ]);
  assert.deepEqual(readDevice(defaultDevice), values);
  assert.equal(data.has(storageKey("playing")), false);
});
test("blocked local storage uses session defaults", (t) => {
  t.after(installGlobals({ localStorage: storage({}, true).localStorage }));
  const values = initializeDevice({ ...defaultDevice, reading_mode: "scroll" });
  assert.equal(values.reading_mode, "scroll");
  assert.deepEqual(readDevice(values), values);
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
async function setup(t, prepare = () => {}, options = {}) {
  const { data, localStorage } = storage(options.storage, options.blockStorage);
  const x = await setupFixture(t, prepare, { globals: { localStorage } });
  const guard = { current: null };
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
  const view = await x.render(
    React.createElement(
      PreferenceSession,
      { t: (_, en) => en },
      React.createElement(
        React.Fragment,
        null,
        React.createElement(Display),
        React.createElement(Preferences, { t: (_, en) => en, guard }),
      ),
    ),
  );
  return {
    fixture: x.fixture,
    data,
    view,
    guard,
    unmount: () => x.unmount(view),
    button: (text) => button(view, text),
    click: (text) => click(button(view, text)),
    change: async (label, value) => {
      await act(async () =>
        dispatch(one(view.container, { "aria-label": label }), "change", {
          target: { value },
        }),
      );
    },
    text: () => visibleText(view),
  };
}
test("login initializes defaults before child render; saving global defaults does not reset this device", async (t) => {
  const x = await setup(t, (f) => {
    f.preferences.settings.device_defaults = {
      ...defaultDevice,
      reading_mode: "spread",
      direction: "rtl",
    };
  });
  assert.equal(x.data.get(storageKey("reading-mode")), "spread");
  assert.equal(x.data.get(storageKey("direction")), "rtl");
  await x.change("Title display", "original_title");
  await x.change("Reading mode", "scroll");
  assert.equal(await x.guard.current(), true);
  assert.equal(x.fixture.preferences.revision, 2);
  assert.equal(one(x.view.container, "output").textContent, "原題");
  assert.equal(x.data.get(storageKey("reading-mode")), "spread");
  assert.equal(await x.guard.current(), true);
  assert.equal(x.button("Initialize unset fields only"), undefined);
});
test("default loading failure does not seed fallback values and can be retried", async (t) => {
  const x = await setup(t, (f) => {
    f.preferencesFailure = true;
    f.preferences.settings.device_defaults.direction = "rtl";
  });
  assert.equal(x.data.size, 0);
  assert.equal(all(x.view.container, "form").length, 0);
  x.fixture.preferencesFailure = false;
  await x.click("Retry preferences");
  assert.equal(x.data.get(storageKey("direction")), "rtl");
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
  x.fixture.preferences.settings.device_defaults.direction = "rtl";
  await x.change("Title display", "original_title");
  assert.match(x.text(), /Another device changed/);
  assert.equal(
    attr(one(x.view.container, { "aria-label": "Direction" }), "value"),
    "rtl",
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
  await x.change("Reading mode", "scroll");
  await x.change("Direction", "rtl");
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
test("new device defaults explain their scope and can be applied to this device", async (t) => {
  const x = await setup(t, () => {}, {
    storage: { [storageKey("reading-mode")]: "single" },
  });
  assert.match(x.text(), /devices already in use keep their own/);
  await x.change("Reading mode", "scroll");
  assert.equal(x.data.get(storageKey("reading-mode")), "single");
  await x.click("Apply to this device");
  assert.equal(x.data.get(storageKey("reading-mode")), "scroll");
  assert.match(x.text(), /Applied to this device/);
});
