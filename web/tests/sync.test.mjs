import { dispatch, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { MetadataSync } from "../src/MetadataSync.tsx";
import { button, click, setupFixture, visibleText } from "./harness.mjs";
async function setup(t, prepare = () => {}) {
  const x = await setupFixture(t, prepare);
  const guard = { current: null },
    routes = [];
  const view = await x.render(
    React.createElement(MetadataSync, {
      t: (_, en) => en,
      guard,
      navigate: (route) => routes.push(route),
    }),
  );
  return {
    fixture: x.fixture,
    guard,
    routes,
    view,
    window: x.window,
    button: (text) => button(view, text),
    unmount: () => x.unmount(view),
    click: (text) => click(button(view, text)),
    text: () => visibleText(view),
  };
}
test("sync page only reads persisted status until explicitly started", async (t) => {
  const x = await setup(t);
  assert.equal(x.fixture.syncCalls.length, 0);
  assert.ok(x.fixture.requests.every((r) => r.method === "GET"));
  assert.equal(await x.guard.current(), true);
  await act(async () => x.window.dispatchEvent(new Event("focus")));
  assert.equal(x.fixture.syncCalls.length, 0);
  await x.click("Sync pending");
  // One request covers a whole batch of books; there is no per-book round trip.
  assert.equal(x.fixture.syncCalls.length, 1);
  assert.ok(
    x.fixture.syncCalls.every((r) => r.mode === "pending" && r.limit === 50),
  );
  assert.match(x.text(), /Checked 6Synced 6/);
  assert.ok(x.fixture.books.every((b) => b.sidecar.synced_revision === 1));
});
test("all-library pass checks clean books and leaves conflicts and errors actionable", async (t) => {
  const x = await setup(t, (f) => {
    f.books.forEach((b) => (b.sidecar.synced_revision = 1));
    f.syncOutcomes = { "fixture-0": "conflict", "fixture-1": "error" };
  });
  assert.equal(x.button("Sync pending"), undefined);
  await x.click("Check now");
  assert.match(x.text(), /Checked 6Synced 4Conflicts 1Failed 1/);
  assert.ok(x.button("山间来信"));
  await x.click("山间来信");
  assert.deepEqual(x.routes, ["book/fixture-0"]);
  assert.equal(x.fixture.books[0].metadata.title, "山间来信");
  assert.equal(x.fixture.books[0].sidecar.last_error, "external_conflict");
});
test("pause waits for current response and continue starts after acknowledged cursor", async (t) => {
  const x = await setup(t, (f) => (f.syncBatchCap = 1));
  const original = globalThis.fetch;
  let release, operation;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    const response = await original(path, options);
    if (path === "/api/sidecar/sync" && x.fixture.syncCalls.length === 1)
      await new Promise((resolve) => (release = resolve));
    return response;
  });
  await act(async () => {
    operation = dispatch(x.button("Sync pending"), "click");
  });
  assert.equal(await x.guard.current(), false);
  await x.click("Pause");
  assert.equal(attr(x.button("Pause"), "disabled"), true);
  release();
  await act(async () => operation);
  assert.equal(x.fixture.syncCalls.length, 1);
  assert.match(x.text(), /Checked 1Synced 1/);
  assert.equal(await x.guard.current(), true);
  await x.click("Continue");
  assert.equal(x.fixture.syncCalls[1].after_id, "fixture-0");
  assert.equal(x.fixture.syncCalls[1].through_id, "fixture-5");
  assert.match(x.text(), /Checked 6Synced 6/);
});
test("lost response stops the pass and explicit continue safely uses current saved status", async (t) => {
  const x = await setup(t, (f) => {
    f.syncLoseResponse = true;
    f.syncBatchCap = 1;
  });
  await x.click("Sync pending");
  assert.equal(x.fixture.syncCalls.length, 1);
  assert.equal(x.fixture.books[0].sidecar.synced_revision, 1);
  assert.match(x.text(), /Checked 0/);
  assert.match(x.text(), /Connection failed/);
  await x.click("Continue");
  assert.equal(x.fixture.syncCalls.length, 6);
  assert.ok(x.fixture.books.every((b) => b.sidecar.synced_revision === 1));
  // Counters only include acknowledged responses, not inferred lost work.
  assert.match(x.text(), /Checked 5Synced 5/);
});
test("storage failure stops further writes and status remains readable for recovery", async (t) => {
  const x = await setup(t, (f) => (f.syncFailure = 503));
  await x.click("Sync pending");
  assert.equal(x.fixture.syncCalls.length, 1);
  assert.match(x.text(), /Library storage is unavailable/);
  assert.equal(await x.guard.current(), true);
  x.fixture.syncFailure = null;
  await x.click("Continue");
  assert.match(x.text(), /Checked 6Synced 6/);
});
test("double start is rejected immediately and unmount stops subsequent requests", async (t) => {
  const x = await setup(t);
  const original = globalThis.fetch;
  let release, operation;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    const response = await original(path, options);
    if (path === "/api/sidecar/sync")
      await new Promise((resolve) => (release = resolve));
    return response;
  });
  await act(async () => {
    const start = () => dispatch(x.button("Sync pending"), "click");
    operation = start();
    await start();
  });
  assert.equal(x.fixture.syncCalls.length, 1);
  const event = new Event("beforeunload", { cancelable: true });
  x.window.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true);
  await x.unmount();
  release();
  await act(async () => operation);
  assert.equal(x.fixture.syncCalls.length, 1);
  assert.equal(x.guard.current, null);
});
test("attention list pages forward and back without mutating metadata", async (t) => {
  const x = await setup(t, (f) => {
    f.syncPageSize = 2;
  });
  assert.match(x.text(), /山间来信/);
  assert.equal(attr(x.button("Previous"), "disabled"), true);
  await x.click("Next page");
  assert.doesNotMatch(x.text(), /山间来信/);
  assert.match(x.text(), /海边的慢日子/);
  await x.click("Next page");
  assert.match(x.text(), /小小宇宙旅行记/);
  await x.click("Previous");
  assert.match(x.text(), /海边的慢日子/);
  await x.click("First");
  assert.match(x.text(), /山间来信/);
  assert.equal(x.fixture.syncCalls.length, 0);
});
test("status read failure offers retry without starting file work", async (t) => {
  const x = await setup(t, (f) => (f.syncStatusFailure = true));
  assert.equal(x.button("Sync pending"), undefined);
  assert.match(x.text(), /Library storage is unavailable/);
  x.fixture.syncStatusFailure = false;
  await act(async () => x.window.dispatchEvent(new Event("focus")));
  assert.equal(attr(x.button("Sync pending"), "disabled"), false);
  assert.equal(x.fixture.syncCalls.length, 0);
});
