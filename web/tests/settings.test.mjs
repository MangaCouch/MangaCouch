import { all, one, dispatch, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import { act, waitFor } from "@testing-library/react";
import { Settings } from "../src/Settings.tsx";
import { renderPage } from "./harness.mjs";
async function setup(t, prepare) {
  const x = await renderPage(t, Settings, {
    prepare,
    globals: { location: { origin: "http://fixture" } },
    props: {
      lang: "en",
      changeLanguage: () => {},
      logout: () => {},
      lock: () => {},
    },
  });
  const section = (title) =>
    all(x.view.container, "details").find((node) =>
      one(node, "h3").textContent.includes(title),
    );
  const toggle = (title, open) =>
    act(async () =>
      dispatch(section(title), "toggle", { currentTarget: { open } }),
    );
  return {
    ...x,
    section,
    open: (title) => toggle(title, true),
    close: (title) => toggle(title, false),
    reads: (path) => x.fixture.requests.filter((r) => r.path === path).length,
    focus: () => act(async () => x.window.dispatchEvent(new Event("focus"))),
  };
}
/** Waits until `predicate` holds: a response takes several event-loop turns to show. */
const until = (predicate) => waitFor(() => assert.ok(predicate()));
test("settings groups are collapsible, file checking is direct, and opening does not mutate", async (t) => {
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
    "Backup & restore",
  ])
    assert.ok(x.section(title), title);
  assert.ok(x.button("Check"));
  // Nested sections use h3/h4 only; the categories own the h2 headings.
  assert.equal(all(x.view.container, "h2").length, 5);
  await until(() => x.text().includes("Version main-0123456"));
  assert.equal(
    one(x.view.container, "main").lastElementChild.tagName,
    "FOOTER",
  );
  // The page title is the only heading above the categories (no eyebrow).
  assert.match(x.text(), /^SettingsReading & display/);
  await x.open("Backup & restore");
  assert.ok(x.button("Download library backup"));
  assert.equal(
    one(x.view.container, "pre").textContent,
    "mangacouch restore /path/backup.zip /path/restored-state\nmangacouch --db /path/restored-state/library.sqlite serve --origin <URL>",
  );
  assert.ok(x.fixture.requests.every((r) => r.method === "GET"));
  assert.deepEqual(x.routes, []);
});
test("diagnostic failure keeps backup available", async (t) => {
  const x = await setup(t, (f) => {
    f.diagnosticsFailure = true;
  });
  await x.focus();
  assert.match(x.text(), /Library storage is unavailable/);
  await x.open("Backup & restore");
  assert.equal(attr(x.button("Download library backup"), "disabled"), false);
});
test("a backup is prepared on the server, then saved by the browser's own download", async (t) => {
  const x = await setup(t, (f) => {
    f.snapshotFailure = "insufficient_storage";
  });
  const saved = [];
  t.mock.method(window.HTMLAnchorElement.prototype, "click", function () {
    saved.push([attr(this, "href"), this.download]);
  });
  await x.open("Backup & restore");
  await x.click("Download library backup");
  await until(() => /low on disk space/.test(x.text()));
  assert.equal(all(x.view.container, { role: "alert" }).length, 1);
  assert.equal(saved.length, 0);
  x.fixture.snapshotFailure = null;
  await x.click("Download library backup");
  await until(() => saved.length === 1);
  assert.deepEqual(saved[0], [
    "/api/snapshots/prepared-1",
    "mangacouch-snapshot.zip",
  ]);
  assert.doesNotMatch(x.text(), /low on disk space|Preparing/);
  const prepares = x.fixture.requests.filter((r) =>
    r.path.startsWith("/api/snapshots"),
  );
  assert.equal(prepares.map((r) => r.method).join(" "), "POST POST");
});
test("the backup button says it is preparing while the server builds it", async (t) => {
  let finish;
  const x = await setup(t, (f) => {
    f.snapshotBuild = new Promise((resolve) => (finish = resolve));
  });
  t.mock.method(window.HTMLAnchorElement.prototype, "click", () => {});
  await x.open("Backup & restore");
  await x.click("Download library backup");
  await until(() => /Preparing…/.test(x.text()));
  assert.equal(attr(x.button("Download library backup"), "disabled"), true);
  finish();
  await until(() => !/Preparing…/.test(x.text()));
  assert.equal(attr(x.button("Download library backup"), "disabled"), false);
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
  await x.focus();
  assert.match(x.text(), /Library storage is unavailable/);
  x.fixture.diagnosticsFailure = false;
  await x.focus();
  assert.doesNotMatch(x.text(), /Library storage is unavailable/);
  assert.match(x.text(), /\/library\/state\/library\.sqlite/);
});
test("cache shows its budget and starts cover and thumbnail warm-ups separately", async (t) => {
  const x = await setup(t);
  await until(() => x.text().includes("3.0 MiB of 20.0 GiB used"));
  await x.open("Cache");
  await until(() =>
    x.text().includes("Finished 6 books: 120 rendered, 0 books failed"),
  );
  assert.match(x.text(), /Finished 6 books: 6 rendered, 0 books failed/);
  assert.equal(all(x.section("Cache"), "progress").length, 0);
  await act(async () =>
    dispatch(x.button("Render missing thumbnails"), "click", {}),
  );
  await until(() => x.text().includes("Running 0/6 books"));
  assert.equal(x.fixture.warmRequests.covers, 0);
  assert.equal(x.fixture.warmRequests.thumbnails, 1);
  assert.equal(all(x.section("Cache"), "progress").length, 1);
  assert.equal(attr(x.button("Render missing thumbnails"), "disabled"), true);
  assert.equal(attr(x.button("Render missing covers"), "disabled"), false);
  await act(async () =>
    dispatch(x.button("Render missing covers"), "click", {}),
  );
  await until(() => x.fixture.warmRequests.covers === 1);
  await until(() => all(x.section("Cache"), "progress").length === 2);
  assert.equal(attr(x.button("Render missing covers"), "disabled"), true);
});
test("unsaved or saving source settings hold navigation and page unload until saved or discarded", async (t) => {
  const x = await setup(t);
  const unloadBlocked = () => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  };
  // The E-Hentai cookie (no other section with one is open).
  const cookie = () =>
    all(x.view.container, "label")
      .find((node) => node.textContent.includes("E-Hentai Cookie"))
      ?.querySelector("input");
  const type = (value) =>
    act(async () => dispatch(cookie(), "change", { target: { value } }));
  const synthetic = "ipb_member_id=0; ipb_pass_hash=synthetic-fixture";
  await x.open("Source accounts & downloads");
  await until(() => !!cookie());
  assert.equal(await x.guard.current(), true);
  assert.equal(unloadBlocked(), false);
  // A typed cookie that was never saved holds navigation and unload…
  await type(synthetic);
  assert.match(await x.guard.current(), /Source settings are unsaved/);
  assert.equal(unloadBlocked(), true);
  assert.match(x.text(), /unsaved changes/);
  // …until it is explicitly discarded.
  await act(async () =>
    dispatch(x.button("Discard unsaved changes"), "click", {}),
  );
  assert.equal(attr(cookie(), "value"), "");
  assert.equal(await x.guard.current(), true);
  assert.equal(unloadBlocked(), false);
  assert.equal(x.fixture.sourceSaves.length, 0);
  // While the save is on its way, leaving still waits.
  const fixtureFetch = globalThis.fetch;
  let release;
  t.mock.method(globalThis, "fetch", async (path, options = {}) => {
    if (options.method === "PUT")
      await new Promise((resolve) => (release = resolve));
    return fixtureFetch(path, options);
  });
  await type(synthetic);
  await act(async () =>
    dispatch(x.button("Save E-Hentai Cookie"), "click", {}),
  );
  await until(() => !!release);
  assert.match(await x.guard.current(), /Source settings are unsaved/);
  assert.equal(unloadBlocked(), true);
  await act(async () => release());
  await until(() => x.fixture.sourceSaves.length === 1);
  await until(() => attr(cookie(), "value") === "");
  assert.equal(x.fixture.sourceSaves[0].secrets.eh_cookie, synthetic);
  assert.equal(await x.guard.current(), true);
  assert.equal(unloadBlocked(), false);
  // A failed save keeps the change held and offers both choices.
  t.mock.method(globalThis, "fetch", async (path, options = {}) =>
    options.method === "PUT"
      ? new Response("{}", { status: 500 })
      : fixtureFetch(path, options),
  );
  await type(synthetic);
  await act(async () =>
    dispatch(x.button("Save E-Hentai Cookie"), "click", {}),
  );
  await until(() => !!x.button("Save my change again"));
  assert.match(await x.guard.current(), /Source settings are unsaved/);
  await act(async () =>
    dispatch(x.button("Discard unsaved changes"), "click", {}),
  );
  assert.equal(await x.guard.current(), true);
});

test("the cache deep link expands cache and mounts its controls", async (t) => {
  const x = await renderPage(t, Settings, {
    props: {
      section: "cache",
      lang: "en",
      changeLanguage() {},
      logout() {},
      lock() {},
    },
    globals: { location: { origin: "http://fixture" } },
  });
  const cache = [...x.view.container.querySelectorAll("details")].find(
    (item) => item.querySelector("h3").textContent === "Cache",
  );
  assert.equal(cache.open, true);
  assert.ok(cache.querySelector(".settings-section-body"));
  assert.ok(x.fixture.requests.some((r) => r.path === "/api/cache"));
});

test("duplicate comparison progress does not claim an empty result before it finishes", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.duplicateComparing = { done: 2, total: 5 };
  });
  await x.open("Duplicate books");
  await until(() => /Comparing 2 \/ 5 books/.test(x.text()));
  assert.doesNotMatch(x.text(), /No duplicate books found/);
});

test("source save keeps text edited while its earlier request is in flight", async (t) => {
  const x = await setup(t);
  await x.open("Source accounts & downloads");
  const cookie = () =>
    all(x.view.container, "label")
      .find((node) => node.textContent.includes("E-Hentai Cookie"))
      ?.querySelector("input");
  await until(() => !!cookie());
  const original = globalThis.fetch;
  let release;
  t.mock.method(globalThis, "fetch", async (path, options = {}) => {
    if (options.method === "PUT")
      await new Promise((resolve) => (release = resolve));
    return original(path, options);
  });
  await act(async () =>
    dispatch(cookie(), "change", { target: { value: "first-cookie" } }),
  );
  await act(async () =>
    dispatch(x.button("Save E-Hentai Cookie"), "click", {}),
  );
  await until(() => !!release);
  await act(async () =>
    dispatch(cookie(), "change", { target: { value: "second-cookie" } }),
  );
  await act(async () => release());
  await until(() => x.fixture.sourceSaves.length === 1);
  assert.equal(x.fixture.sourceSaves[0].secrets.eh_cookie, "first-cookie");
  assert.equal(attr(cookie(), "value"), "second-cookie");
  assert.match(await x.guard.current(), /Source settings are unsaved/);
});

test("returning to settings restores the expanded duplicate section", async (t) => {
  const x = await setup(t);
  await x.open("Duplicate books");
  await act(async () => x.view.unmount());
  const y = await setup(t);
  assert.equal(y.section("Duplicate books").open, true);
});
