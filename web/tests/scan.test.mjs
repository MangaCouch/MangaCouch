import assert from "node:assert/strict";
import test from "node:test";
import { act } from "@testing-library/react";
import { Scan } from "../src/Scan.tsx";
import { renderPage } from "./harness.mjs";
import { all } from "./dom.mjs";
function setup(t, prepare = async () => {}) {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  globalThis.document.hidden = false;
  return renderPage(t, Scan, {
    prepare,
    globals: { document: globalThis.document },
  });
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
  const x = await setup(t);
  const page = globalThis.document;
  const reads = () =>
    x.fixture.requests.filter((r) => r.path === "/api/scans").length;
  assert.equal(reads(), 1);
  page.hidden = true;
  await x.tick(60000);
  assert.equal(reads(), 1);
  page.hidden = false;
  await act(async () => page.dispatchEvent(new Event("visibilitychange")));
  assert.equal(reads(), 2);
  assert.match(x.text(), /Start scan/);
});
test("storage loss pauses the server task and can resume after repair", async (t) => {
  const x = await setup(t);
  await x.click("Start scan");
  x.fixture.scanOffline = true;
  await x.tick(1500);
  assert.equal(x.fixture.scan.state, "paused");
  assert.match(x.text(), /Check its location and retry/);
  x.fixture.scanOffline = false;
  await x.click("Continue scan");
  assert.equal(x.fixture.scan.error_code, null);
});

test("a scan paused by a storage read error says why", async (t) => {
  const x = await setup(t, async (fixture, window) => {
    await window.fetch("/api/scans", { method: "POST" });
    Object.assign(fixture.scan, {
      state: "paused",
      error_code: "storage_unavailable",
    });
  });
  assert.match(x.text(), /Library storage is unavailable/);
  assert.match(x.text(), /Continue scan/);
});
test("filtering results preserves the checkbox and replaces the previous result set", async (t) => {
  const x = await setup(t);
  assert.equal(x.view.queryByLabelText("Failed only"), null);
  await x.click("Start scan");
  for (let i = 0; i < 6; i++) await x.tick(1500);
  const filter = x.view.getByLabelText("Failed only");
  filter.focus();
  await act(async () => filter.click());
  assert.equal(x.view.getByLabelText("Failed only"), filter);
  assert.equal(document.activeElement, filter);
  assert.ok(
    x.fixture.requests.some(
      (item) =>
        item.path === "/api/scans" && item.query.get("state") === "error",
    ),
  );
  assert.ok(
    [...x.view.container.querySelectorAll(".scan-state")].every(
      (node) => node.textContent === "Needs attention",
    ),
  );
  await act(async () => filter.click());
  assert.equal(x.view.getByLabelText("Failed only"), filter);
  assert.ok(
    [...x.view.container.querySelectorAll(".scan-state")].some(
      (node) => node.textContent !== "Needs attention",
    ),
  );
});

test("poll refreshes loaded tail pages and discovers new rows beyond an exhausted cursor", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.scan = {
      id: "long",
      created_at: 1700000000,
      ignored: 0,
      state: "paused",
      keep_source: false,
      entries: Array.from({ length: 75 }, (_, i) => ({
        id: i + 1,
        path: `book-${i}.cbz`,
        kind: "archive",
        state: "pending",
        warnings: [],
      })),
    };
  });
  await x.click("Load more results");
  assert.match(x.text(), /book-74/);
  x.fixture.scan.entries[74].state = "error";
  x.fixture.scan.entries[74].error_code = "invalid_archive";
  x.fixture.scan.entries.push({
    id: 76,
    path: "newly-found.cbz",
    kind: "archive",
    state: "indexed",
    warnings: [],
  });
  await x.tick(15000);
  assert.match(x.text(), /damaged or unsupported/);
  assert.match(x.text(), /newly-found/);
  await act(async () => x.view.getByLabelText("Failed only").click());
  assert.match(x.text(), /book-74/);
  assert.doesNotMatch(x.text(), /book-0\.cbz|newly-found/);
});

/** A scan of `count` pending archives in `state`. */
const longScan =
  (count, state = "paused") =>
  (fixture) => {
    fixture.scan = {
      id: "long",
      created_at: 1700000000,
      ignored: 0,
      state,
      keep_source: false,
      error_code: null,
      entries: Array.from({ length: count }, (_, i) => ({
        id: i + 1,
        path: `book-${i}.cbz`,
        kind: "archive",
        state: state === "done" ? "indexed" : "pending",
        book_id: null,
        error_code: null,
        warnings: [],
      })),
    };
  };
/** Loads every result page, then returns the scan requests one poll makes. */
async function pollRequests(x) {
  while (x.button("Load more results")) await x.click("Load more results");
  const before = x.fixture.requests.length;
  await x.tick(15000);
  // Array.from: the fake backend's arrays come from another realm.
  return Array.from(
    x.fixture.requests
      .slice(before)
      .filter((item) => item.path.startsWith("/api/scans")),
    (item) => Number(item.query.get("limit")),
  );
}

test("a poll refreshes all loaded rows with 200-row pages", async (t) => {
  const x = await setup(t, longScan(180));
  await x.click("Load more results");
  const entries = x.fixture.scan.entries;
  assert.deepEqual(await pollRequests(x), [200]);
  entries[119].state = "error";
  entries[119].error_code = "invalid_archive";
  entries[159].state = "indexed";
  for (const item of entries) if (item.state === "pending") item.state = "unchanged";
  entries.push({
    id: 181,
    path: "newly-found.cbz",
    kind: "archive",
    state: "indexed",
    warnings: [],
  });
  assert.deepEqual(await pollRequests(x), [200]);
  assert.doesNotMatch(x.text(), /\.cbzPending/);
  assert.match(x.text(), /damaged or unsupported/);
  assert.match(x.text(), /newly-found/);
  assert.equal(x.button("Load more results"), undefined);
});

test("a poll of a long result list grows pages to 200 rows, not 50", async (t) => {
  const x = await setup(t, longScan(420));
  // 421 rows wanted (all 420 and one more): 200 + 200 + a 50-row tail.
  assert.deepEqual(await pollRequests(x), [200, 200, 50]);
  assert.match(x.text(), /book-419\.cbz/);
});

test("a finished scan is confirmed with one small request and keeps its rows", async (t) => {
  const x = await setup(t, longScan(120, "done"));
  assert.deepEqual(await pollRequests(x), [1]);
  assert.match(x.text(), /book-119\.cbz/);
  assert.equal(all(x.view.container, ".scan-state").length, 120);
  // A new scan started elsewhere replaces the finished one.
  longScan(3, "paused")(x.fixture);
  x.fixture.scan.id = "next";
  await x.tick(15000);
  assert.equal(all(x.view.container, ".scan-state").length, 3);
});
