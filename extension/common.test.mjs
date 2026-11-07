import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const locale = (name) =>
  JSON.parse(
    readFileSync(new URL(`./_locales/${name}/messages.json`, import.meta.url)),
  );
const messages = locale("en");
const getMessage = (key, substitutions = []) => {
  const entry = messages[key];
  if (!entry) return "";
  const values = [].concat(substitutions);
  return entry.message.replace(/\$([A-Za-z_]+)\$/g, (_, name) =>
    (entry.placeholders?.[name.toLowerCase()]?.content ?? "").replace(
      /\$(\d)/g,
      (_, index) => values[index - 1] ?? "",
    ),
  );
};
const storage = {
  server: "http://library.test:8780",
  key: "downloads-secret",
};
globalThis.chrome = {
  i18n: { getMessage },
  storage: { local: { get: async () => storage } },
  permissions: { contains: async () => true },
  tabs: { create: async (value) => value },
};
const { ConfigError, galleryURL, serverOrigin, enqueue, openQueue } =
  await import("./common.js");
test("extension canonicalizes gallery identity and rejects credential-bearing origins", () => {
  assert.equal(
    galleryURL("https://nhentai.net/g/20/?utm=x"),
    "https://nhentai.net/g/20/",
  );
  assert.throws(() => galleryURL("https://nhentai.net.evil/g/20/"));
  assert.throws(() => galleryURL(""), {
    message: messages.errorGallery.message,
  });
  assert.throws(() => serverOrigin("https://user:secret@library.test"));
  assert.throws(() => serverOrigin("https://library.test/path"));
  assert.throws(() => serverOrigin(undefined), ConfigError);
});
test("extension sends the download key as a raw Bearer, omits cookies and never approves fees", async (t) => {
  let request;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    request = { url, ...options };
    return new Response(JSON.stringify({ id: "job", state: "queued" }));
  });
  await enqueue(storage, "https://nhentai.net/g/20/");
  assert.equal(request.url, "http://library.test:8780/api/downloads");
  assert.equal(request.headers.Authorization, "Bearer downloads-secret");
  assert.equal(request.credentials, "omit");
  assert.equal(request.redirect, "error");
  assert.deepEqual(JSON.parse(request.body), {
    url: "https://nhentai.net/g/20/",
    entry: "find_eh",
  });
  assert.deepEqual(await openQueue(storage), {
    url: "http://library.test:8780/#downloads",
  });
});
test("only settings problems are configuration errors", async (t) => {
  await assert.rejects(enqueue({}, "https://nhentai.net/g/20/"), ConfigError);
  await assert.rejects(openQueue(undefined), {
    message: messages.errorConfigure.message,
  });
  const fetch = t.mock.method(globalThis, "fetch", async () =>
    Response.json({ detail: "boom" }, { status: 503 }),
  );
  await assert.rejects(
    enqueue(storage, "https://nhentai.net/g/20/"),
    (error) =>
      !(error instanceof ConfigError) &&
      error.message === "The library returned an error (503).",
  );
  fetch.mock.mockImplementation(async () => new Response("", { status: 401 }));
  await assert.rejects(
    enqueue(storage, "https://nhentai.net/g/20/"),
    ConfigError,
  );
  fetch.mock.mockImplementation(async () => {
    throw new TypeError("Failed to fetch");
  });
  await assert.rejects(enqueue(storage, "https://nhentai.net/g/20/"), {
    message: "Could not reach the library: Failed to fetch",
  });
});
test("every locale defines the same messages as English", () => {
  const keys = Object.keys(messages).sort();
  for (const name of ["zh_CN"]) {
    const other = locale(name);
    assert.deepEqual(Object.keys(other).sort(), keys, name);
    for (const key of keys)
      assert.deepEqual(
        Object.keys(other[key].placeholders ?? {}),
        Object.keys(messages[key].placeholders ?? {}),
        `${name}.${key} placeholders`,
      );
  }
});
