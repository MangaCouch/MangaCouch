import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const messages = JSON.parse(
  readFileSync(new URL("./_locales/en/messages.json", import.meta.url)),
);
const message = (key, substitutions = []) =>
  (messages[key]?.message ?? "").replace(/\$([A-Za-z_]+)\$/g, (_, name) =>
    (messages[key].placeholders[name.toLowerCase()]?.content ?? "").replace(
      /\$(\d)/g,
      (_, index) => [].concat(substitutions)[index - 1] ?? "",
    ),
  );
const names = [
  "settings",
  "queue",
  "send",
  "gallery",
  "status",
];
let elements, tabs, config, tabURL;
globalThis.chrome = {
  i18n: { getUILanguage: () => "en", getMessage: message },
  runtime: { openOptionsPage: async () => {} },
  storage: {
    local: { get: async () => ({ ...config }) },
  },
  permissions: { contains: async () => true },
  tabs: {
    query: async () => [{ url: tabURL }],
    create: async (value) => tabs.push(value),
  },
};
globalThis.document = {
  documentElement: {},
  getElementById: (name) => elements[name],
  querySelectorAll: () => [],
};
const settle = () => new Promise((resolve) => setImmediate(resolve));
let loads = 0;
// Opens a fresh popup: popup.js runs once per module instance.
async function open(state = {}) {
  elements = Object.fromEntries(
    names.map((name) => [
      name,
      { textContent: "", disabled: name === "send", hidden: false },
    ]),
  );
  tabs = [];
  config = state.config ?? {};
  tabURL = state.url ?? "https://e-hentai.org/g/20/abc/";
  await import(`./popup.js?open=${++loads}`);
  await settle();
}
const configured = { server: "http://library.test:8780", key: "secret" };

test("an unconfigured library disables sending and explains why", async () => {
  await open();
  assert.equal(elements.status.textContent, messages.errorConfigure.message);
  assert.equal(elements.send.disabled, true);
});

test("Open downloads without a configured library shows a message instead of a TypeError", async () => {
  await open();
  elements.status.textContent = "";
  await elements.queue.onclick();
  assert.deepEqual(tabs, []);
  assert.equal(elements.status.textContent, messages.errorConfigure.message);
});

test("a successful add says whether the gallery was new", async (t) => {
  let outcome = "created",
    state = "queued";
  t.mock.method(globalThis, "fetch", async () =>
    Response.json({ id: "job", state, outcome }),
  );
  await open({ config: configured });
  assert.equal(elements.send.disabled, false);
  await elements.send.onclick();
  assert.equal(elements.status.textContent, messages.statusAdded.message);
  outcome = "existing";
  await elements.send.onclick();
  assert.equal(elements.status.textContent, messages.statusExisting.message);
  state = "done";
  await elements.send.onclick();
  assert.equal(elements.status.textContent, messages.statusDownloaded.message);
  outcome = "requeued";
  state = "queued";
  await elements.send.onclick();
  assert.equal(elements.status.textContent, messages.statusRequeued.message);
});

test("other sites' pages are refused and E-Hentai pages can be sent", async () => {
  await open({ config: configured, url: "https://example.org/g/20/" });
  assert.equal(elements.send.disabled, true);
  assert.equal(elements.status.textContent, messages.errorGallery.message);
  await open({ config: configured, url: "https://g.e-hentai.org/g/1/abc/" });
  assert.equal(elements.send.disabled, false);
});

test("a failed add shows the library's failure", async (t) => {
  t.mock.method(
    globalThis,
    "fetch",
    async () => new Response("", { status: 500 }),
  );
  await open({ config: configured });
  await elements.send.onclick();
  assert.equal(elements.status.textContent, message("errorLibrary", ["500"]));
});
