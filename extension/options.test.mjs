import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const messages = JSON.parse(
  readFileSync(new URL("./_locales/en/messages.json", import.meta.url)),
);
const elements = Object.fromEntries(
  [
    "form",
    "server",
    "key",
    "status",
    "check",
    "connection",
    "indicator",
    "connectionText",
  ].map((name) => [
    name,
    { value: "", textContent: "", dataset: {}, hidden: true, disabled: false },
  ]),
);
let config;
let grants;
// The identity the library reports for the key, or null for a rejected key.
let principal;
const requests = [];
const api = {
  i18n: {
    getUILanguage: () => "en",
    getMessage: (key, substitutions = []) =>
      (messages[key]?.message ?? "").replace(/\$([A-Za-z_]+)\$/g, (_, name) =>
        (messages[key].placeholders[name.toLowerCase()]?.content ?? "").replace(
          /\$(\d)/g,
          (_, index) => [].concat(substitutions)[index - 1] ?? "",
        ),
      ),
  },
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
    contains: async ({ origins }) =>
      origins.every((origin) => grants.has(origin)),
  },
};
globalThis.chrome = api;
globalThis.document = {
  documentElement: {},
  getElementById: (name) => elements[name],
  querySelectorAll: () => [],
};
globalThis.fetch = async (url, options) => {
  requests.push({ url, ...options });
  return principal
    ? Response.json(principal)
    : new Response("", { status: 401 });
};
const settle = async () => {
  for (let i = 0; i < 6; i++)
    await new Promise((resolve) => setImmediate(resolve));
};
config = { server: "https://old-library.example", key: "fixture" };
grants = new Set([config.server + "/*"]);
principal = { id: "k1", kind: "downloads", label: "laptop" };
await import("./options.js");
await settle();
function edit() {
  config = { server: "https://old-library.example", key: "fixture" };
  grants = new Set([config.server + "/*"]);
  principal = { id: "k1", kind: "downloads", label: "laptop" };
  elements.server.value = "https://new-library.example";
  elements.key.value = "replacement-fixture";
  elements.status.textContent = "";
  elements.connectionText.textContent = "";
  elements.indicator.dataset = {};
  requests.length = 0;
}
test("opening Settings checks the configured library and shows the key's identity", () => {
  assert.equal(elements.connection.hidden, false);
  assert.equal(elements.indicator.dataset.state, "ok");
  assert.equal(
    elements.connectionText.textContent,
    "Connected. The API key “laptop” is valid (Downloads only).",
  );
  const [request] = requests;
  assert.equal(request.url, "https://old-library.example/api/auth/me");
  assert.equal(request.headers.Authorization, "Bearer fixture");
  assert.equal(request.credentials, "omit");
});
test("saving a replacement library revokes the previous origin and checks the new one", async () => {
  edit();
  await elements.form.onsubmit({ preventDefault() {} });
  assert.equal(config.server, "https://new-library.example");
  assert.deepEqual([...grants], ["https://new-library.example/*"]);
  assert.match(elements.status.textContent, /Saved/);
  assert.equal(requests.at(-1).url, "https://new-library.example/api/auth/me");
  assert.equal(
    requests.at(-1).headers.Authorization,
    "Bearer replacement-fixture",
  );
  assert.equal(elements.indicator.dataset.state, "ok");
  assert.match(elements.connectionText.textContent, /Connected/);
  assert.equal(elements.check.disabled, false);
});
test("a rejected key after saving turns the light red", async () => {
  edit();
  principal = null;
  await elements.form.onsubmit({ preventDefault() {} });
  assert.match(elements.status.textContent, /Saved/);
  assert.equal(elements.indicator.dataset.state, "error");
  assert.equal(
    elements.connectionText.textContent,
    messages.errorKeyRejected.message,
  );
});
test("a read-only key connects but is reported as unable to add downloads", async () => {
  edit();
  principal = { id: "k2", kind: "readonly", label: "reader" };
  await elements.form.onsubmit({ preventDefault() {} });
  assert.equal(elements.indicator.dataset.state, "error");
  assert.equal(
    elements.connectionText.textContent,
    messages.errorKeyReadOnly.message,
  );
});
test("Check connection verifies the typed values without saving them", async () => {
  edit();
  elements.server.value = "https://unsaved-library.example";
  await elements.check.onclick();
  assert.equal(config.server, "https://old-library.example");
  assert.equal(elements.indicator.dataset.state, "error");
  assert.equal(
    elements.connectionText.textContent,
    messages.errorPermissionMissing.message,
  );
  assert.deepEqual(requests, []);
  elements.server.value = config.server;
  elements.key.value = "typed-key";
  await elements.check.onclick();
  assert.equal(requests.at(-1).headers.Authorization, "Bearer typed-key");
  assert.equal(elements.indicator.dataset.state, "ok");
  elements.key.value = " ";
  await elements.check.onclick();
  assert.equal(
    elements.connectionText.textContent,
    messages.errorKeyRequired.message,
  );
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
  assert.deepEqual(requests, []);
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
  assert.equal(elements.indicator.dataset.state, "ok");
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

test("an older connection success cannot replace a newer key failure", async (t) => {
  edit();
  elements.server.value = config.server;
  let release;
  t.mock.method(globalThis, "fetch", async (_url, init) =>
    init.headers.Authorization === "Bearer replacement-fixture"
      ? new Promise(
          (resolve) =>
            (release = () =>
              resolve(Response.json({ kind: "downloads", label: "old" }))),
        )
      : new Response("", { status: 401 }),
  );
  const older = elements.check.onclick();
  await settle();
  elements.key.value = "bad-new-key";
  elements.key.oninput();
  await elements.check.onclick();
  assert.equal(elements.indicator.dataset.state, "error");
  release();
  await older;
  assert.equal(elements.indicator.dataset.state, "error");
  assert.equal(
    elements.connectionText.textContent,
    messages.errorKeyRejected.message,
  );
  assert.equal(elements.check.disabled, false);
});
