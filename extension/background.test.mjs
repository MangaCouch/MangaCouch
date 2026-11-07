import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const messages = JSON.parse(
  readFileSync(new URL("./_locales/en/messages.json", import.meta.url)),
);
let config = { server: "http://library.test:8780", key: "downloads-secret" };
const session = {};
const calls = { options: 0, tabs: [], badge: [], title: [] };
let onClicked;
globalThis.chrome = {
  i18n: {
    getMessage: (key, substitutions = []) =>
      (messages[key]?.message ?? "").replace(/\$([A-Za-z_]+)\$/g, (_, name) =>
        (messages[key].placeholders[name.toLowerCase()]?.content ?? "").replace(
          /\$(\d)/g,
          (_, index) => [].concat(substitutions)[index - 1] ?? "",
        ),
      ),
  },
  runtime: {
    onInstalled: { addListener() {} },
    openOptionsPage: async () => {
      calls.options += 1;
    },
  },
  contextMenus: {
    onClicked: {
      addListener(listener) {
        onClicked = listener;
      },
    },
  },
  storage: {
    local: { get: async () => ({ ...config }) },
    session: {
      get: async (key) => ({ [key]: session[key] }),
      set: async (value) => Object.assign(session, value),
      remove: async (key) => delete session[key],
    },
  },
  permissions: { contains: async () => true },
  tabs: { create: async (value) => calls.tabs.push(value) },
  action: {
    setBadgeText: async ({ text }) => calls.badge.push(text),
    setTitle: async ({ title }) => calls.title.push(title),
  },
};
await import("./background.js");
const click = () =>
  onClicked({ menuItemId: "queue", linkUrl: "https://nhentai.net/g/20/" });
test("a successful context-menu request clears the badge and opens the queue", async (t) => {
  t.mock.method(globalThis, "fetch", async () =>
    Response.json({ id: "job", state: "queued" }),
  );
  session.lastError = "stale";
  await click();
  assert.equal(calls.options, 0);
  assert.equal(session.lastError, undefined);
  assert.equal(calls.badge.at(-1), "");
  assert.deepEqual(calls.tabs, [
    { url: "http://library.test:8780/#downloads" },
  ]);
});
test("a library failure is kept for the popup and does not open Settings", async (t) => {
  t.mock.method(globalThis, "fetch", async () =>
    Response.json({}, { status: 503 }),
  );
  await click();
  assert.equal(calls.options, 0);
  assert.equal(session.lastError, "The library returned an error (503).");
  assert.equal(calls.badge.at(-1), "!");
  assert.match(calls.title.at(-1), /MangaCouch: The library returned/);
});
test("a configuration problem opens Settings", async () => {
  config = {};
  await click();
  assert.equal(calls.options, 1);
  assert.equal(session.lastError, messages.errorConfigure.message);
});
