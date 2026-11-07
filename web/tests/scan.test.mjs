import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { Scan } from "../src/Scan.tsx";
import { button, click, setupFixture, tick, visibleText } from "./harness.mjs";
async function setup(t, prepare = async () => {}) {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const x = await setupFixture(t, prepare);
  const routes = [];
  const view = await x.render(
    React.createElement(Scan, {
      t: (_, en) => en,
      navigate: (route) => routes.push(route),
    }),
  );
  return {
    fixture: x.fixture,
    view,
    routes,
    button: (text) => button(view, text),
    click: (text) => click(button(view, text)),
    tick: (ms) => tick(t, ms),
    text: () => visibleText(view),
    unmount: () => x.unmount(view),
  };
}
test("scan starts with consume-source policy and polls server work until completion", async (t) => {
  const x = await setup(t);
  assert.ok(x.fixture.requests.every((item) => item.method === "GET"));
  await x.click("Start scan");
  for (let i = 0; i < 6; i++) await x.tick(1500);
  assert.equal(x.fixture.scan.state, "done");
  assert.match(x.text(), /damaged or unsupported/);
  assert.equal(x.fixture.scan.keep_source, false);
  assert.ok(x.fixture.requests.every((item) => !item.path.includes("/step")));
  await x.click("View book");
  assert.equal(x.routes[0], "book/fixture-3");
});
test("reopening an active scan follows background progress without a continue command", async (t) => {
  const x = await setup(t, (_, window) =>
    window.fetch("/api/scans", { method: "POST" }),
  );
  assert.match(x.text(), /Scanning/);
  for (let i = 0; i < 6; i++) await x.tick(1500);
  assert.equal(x.fixture.scan.state, "done");
});
test("pause and resume change the persistent task, leaving the page only stops polling", async (t) => {
  const x = await setup(t);
  await x.click("Start scan");
  await x.click("Pause scan");
  assert.equal(x.fixture.scan.state, "paused");
  const listed = x.fixture.scan.entries.length;
  await x.tick(5000);
  assert.equal(x.fixture.scan.entries.length, listed);
  assert.equal(x.fixture.scan.state, "paused");
  await x.click("Continue scan");
  await x.tick(1500);
  assert.ok(
    x.fixture.scan.entries.some(
      (item) => item.state !== "pending" && item.kind === "archive",
    ),
  );
  await x.unmount();
  const count = x.fixture.requests.length;
  await x.tick(20000);
  assert.equal(x.fixture.requests.length, count);
  assert.equal(x.fixture.scan.state, "active");
});
test("scan item errors show localized text for stable codes and the server message otherwise", async (t) => {
  const x = await setup(t);
  await x.click("Start scan");
  for (let i = 0; i < 6; i++) await x.tick(1500);
  assert.match(x.text(), /The archive is damaged or unsupported/);
  await x.click("Start scan");
  x.fixture.scan.entries.push({
    id: 99,
    path: "odd.cbz",
    kind: "archive",
    state: "error",
    book_id: null,
    error_code: "some_new_reason",
    error_message: "Multiple records refer to this path",
    warnings: [],
  });
  for (let i = 0; i < 3; i++) await x.tick(1500);
  assert.match(x.text(), /Multiple records refer to this path/);
  assert.doesNotMatch(x.text(), /InvalidArchive|ValueError/);
});
test("a hidden tab skips scan polling until it becomes visible again", async (t) => {
  const page = globalThis.document;
  page.hidden = false;
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const x = await setupFixture(t, () => {}, { globals: { document: page } });
  const view = await x.render(
    React.createElement(Scan, { t: (_, en) => en, navigate: () => {} }),
  );
  const reads = () =>
    x.fixture.requests.filter((r) => r.path === "/api/scans").length;
  assert.equal(reads(), 1);
  page.hidden = true;
  await tick(t, 60000);
  assert.equal(reads(), 1);
  page.hidden = false;
  await act(async () => page.dispatchEvent(new Event("visibilitychange")));
  assert.equal(reads(), 2);
  assert.match(visibleText(view), /Start scan/);
});
test("storage loss pauses the server task and can resume after repair", async (t) => {
  const x = await setup(t);
  await x.click("Start scan");
  x.fixture.scanOffline = true;
  await x.tick(1500);
  assert.equal(x.fixture.scan.state, "paused");
  assert.match(x.text(), /Repair the mount or path/);
  x.fixture.scanOffline = false;
  await x.click("Continue scan");
  assert.equal(x.fixture.scan.error_code, null);
});
