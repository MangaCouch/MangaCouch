import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const messages = JSON.parse(
  readFileSync(new URL("./_locales/en/messages.json", import.meta.url)),
);
const elements = Object.fromEntries(
  ["form", "server", "key", "status"].map((name) => [
    name,
    { value: "", textContent: "" },
  ]),
);
let config;
let grants;
const api = {
  i18n: { getMessage: (key) => messages[key]?.message ?? "" },
  storage: {
    local: {
      get: async () => ({ ...config }),
      set: async (value) => {
        config = value;
      },
      setAccessLevel: async () => {},
    },
  },
  permissions: {
    request: async ({ origins }) => {
      origins.forEach((origin) => grants.add(origin));
      return true;
    },
    remove: async ({ origins }) => {
      origins.forEach((origin) => grants.delete(origin));
      return true;
    },
  },
};
globalThis.chrome = api;
globalThis.document = {
  documentElement: {},
  getElementById: (name) => elements[name],
  querySelectorAll: () => [],
};
config = { server: "https://old-library.example", key: "fixture" };
grants = new Set([config.server + "/*"]);
await import("./options.js");
await new Promise((resolve) => setImmediate(resolve));
function edit() {
  config = { server: "https://old-library.example", key: "fixture" };
  grants = new Set([config.server + "/*"]);
  elements.server.value = "https://new-library.example";
  elements.key.value = "replacement-fixture";
  elements.status.textContent = "";
}
test("saving a replacement library revokes the previous origin", async () => {
  edit();
  await elements.form.onsubmit({ preventDefault() {} });
  assert.equal(config.server, "https://new-library.example");
  assert.deepEqual([...grants], ["https://new-library.example/*"]);
  assert.match(elements.status.textContent, /Saved/);
});
test("a failed settings write preserves the configured library permission", async (t) => {
  edit();
  t.mock.method(api.storage.local, "set", async () => {
    throw new Error("Storage unavailable");
  });
  await elements.form.onsubmit({ preventDefault() {} });
  assert.equal(config.server, "https://old-library.example");
  assert.ok(grants.has("https://old-library.example/*"));
  assert.match(elements.status.textContent, /Storage unavailable/);
});
test("a failed revocation reports that new settings were saved", async (t) => {
  edit();
  t.mock.method(api.permissions, "remove", async () => {
    throw new Error("Permission unavailable");
  });
  await elements.form.onsubmit({ preventDefault() {} });
  assert.equal(config.server, "https://new-library.example");
  assert.match(
    elements.status.textContent,
    /Saved, but the old library permission/,
  );
});
test("a declined revocation does not report full success", async (t) => {
  edit();
  t.mock.method(api.permissions, "remove", async () => false);
  await elements.form.onsubmit({ preventDefault() {} });
  assert.equal(config.server, "https://new-library.example");
  assert.match(
    elements.status.textContent,
    /Saved, but the old library permission/,
  );
});
test("saving the same origin retains its permission", async (t) => {
  edit();
  elements.server.value = config.server;
  const remove = t.mock.method(api.permissions, "remove");
  await elements.form.onsubmit({ preventDefault() {} });
  assert.equal(remove.mock.callCount(), 0);
  assert.ok(grants.has(config.server + "/*"));
});
test("an invalid address is reported in the configured language, not as a TypeError", async () => {
  edit();
  elements.server.value = "not a url";
  await elements.form.onsubmit({ preventDefault() {} });
  assert.equal(elements.status.textContent, messages.errorOrigin.message);
  assert.equal(config.server, "https://old-library.example");
});
