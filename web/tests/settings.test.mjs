import { all, one, dispatch, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { Settings } from "../src/Settings.tsx";
import { render, act } from "@testing-library/react";
import { button, setupFixture, visibleText } from "./harness.mjs";
async function setup(t, prepare = () => {}) {
  const x = await setupFixture(t, prepare, {
    globals: { location: { origin: "http://fixture" } },
  });
  const guard = { current: null },
    routes = [];
  const view = await x.render(
    React.createElement(Settings, {
      t: (_, en) => en,
      guard,
      navigate: (route) => routes.push(route),
    }),
  );
  const section = (title) =>
    all(view.container, "details").find((node) =>
      one(node, "h3").textContent.includes(title),
    );
  const toggle = (title, open) =>
    act(async () =>
      dispatch(section(title), "toggle", { currentTarget: { open } }),
    );
  return {
    fixture: x.fixture,
    guard,
    routes,
    view,
    section,
    open: (title) => toggle(title, true),
    close: (title) => toggle(title, false),
    reads: (path) => x.fixture.requests.filter((r) => r.path === path).length,
    focus: () => act(async () => x.window.dispatchEvent(new Event("focus"))),
    text: () => visibleText(view),
    button: (text) => button(view, text),
  };
}
test("every settings area is a collapsible section and nothing mutates on open", async (t) => {
  const x = await setup(t);
  assert.equal(
    all(x.view.container, { className: "settings-category panel" }).length,
    5,
  );
  for (const title of [
    "Scan library",
    "Source accounts & downloads",
    "API keys",
    "Change library passcode",
    "External readers (OPDS)",
    "Check missing files",
    "Backup & restore",
  ])
    assert.ok(x.section(title), title);
  // Nested sections use h3/h4 only; the categories own the h2 headings.
  assert.equal(all(x.view.container, "h2").length, 5);
  assert.match(x.text(), /^SETTINGS/);
  await x.open("Backup & restore");
  const link = all(x.view.container, "a").find(
    (n) => attr(n, "href") === "/api/snapshots",
  );
  assert.equal(attr(link, "download"), true);
  assert.equal(
    one(x.view.container, "pre").textContent,
    "python -m mangacouch restore /path/backup.zip /path/restored-library\npython -m mangacouch --db /path/restored-library/library.sqlite serve",
  );
  assert.ok(x.fixture.requests.every((r) => r.method === "GET"));
  assert.deepEqual(x.routes, []);
});
test("diagnostic failure keeps backup available", async (t) => {
  const x = await setup(t, (f) => {
    f.diagnosticsFailure = true;
  });
  assert.match(x.text(), /Library storage is unavailable/);
  await x.open("Backup & restore");
  assert.ok(
    all(x.view.container, "a").some(
      (n) => attr(n, "href") === "/api/snapshots",
    ),
  );
});
test("a collapsed section stops polling and refreshes when opened again", async (t) => {
  const x = await setup(t);
  assert.equal(x.reads("/api/duplicates"), 0);
  await x.open("Duplicate books");
  assert.equal(x.reads("/api/duplicates"), 1);
  await x.focus();
  assert.equal(x.reads("/api/duplicates"), 2);
  await x.close("Duplicate books");
  await x.focus();
  assert.equal(x.reads("/api/duplicates"), 2);
  await x.open("Duplicate books");
  assert.equal(x.reads("/api/duplicates"), 3);
});
test("a failed storage poll clears its error after the next successful one", async (t) => {
  const x = await setup(t, (f) => {
    f.diagnosticsFailure = true;
  });
  assert.match(x.text(), /Library storage is unavailable/);
  x.fixture.diagnosticsFailure = false;
  await x.focus();
  assert.doesNotMatch(x.text(), /Library storage is unavailable/);
  assert.match(x.text(), /\/library\/state\/library\.sqlite/);
});
