import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const messages = JSON.parse(
  readFileSync(new URL("./_locales/en/messages.json", import.meta.url)),
);
const elements = Object.fromEntries(
  ["settings", "queue", "send", "gallery", "choice", "status"].map((name) => [
    name,
    { textContent: "", disabled: name === "send", hidden: false },
  ]),
);
const tabs = [];
globalThis.chrome = {
  i18n: { getMessage: (key) => messages[key]?.message ?? "" },
  runtime: { openOptionsPage: async () => {} },
  storage: {
    local: { get: async () => ({}) },
    session: { get: async () => ({}), remove: async () => {} },
  },
  tabs: {
    query: async () => [{ url: "https://nhentai.net/g/20/" }],
    create: async (value) => tabs.push(value),
  },
  action: { setBadgeText: async () => {}, setTitle: async () => {} },
};
globalThis.document = {
  documentElement: {},
  getElementById: (name) => elements[name],
  querySelectorAll: () => [],
};
await import("./popup.js");
await new Promise((resolve) => setImmediate(resolve));
test("an unconfigured library disables sending and explains why", () => {
  assert.equal(elements.status.textContent, messages.errorConfigure.message);
  assert.equal(elements.send.disabled, true);
});
test("Open downloads without a configured library shows a message instead of a TypeError", async () => {
  elements.status.textContent = "";
  await elements.queue.onclick();
  assert.deepEqual(tabs, []);
  assert.equal(elements.status.textContent, messages.errorConfigure.message);
});
