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
const {
  ConfigError,
  checkConnection,
  galleryURL,
  serverOrigin,
  enqueue,
  openQueue,
} = await import("./common.js");
test("extension canonicalizes gallery identity and rejects credential-bearing origins", () => {
  assert.equal(
    galleryURL("https://e-hentai.org/g/20/abc/?utm=x"),
    "https://e-hentai.org/g/20/abc/",
  );
  assert.throws(() => galleryURL("https://nhentai.net.evil/g/20/"));
  // Every host the library itself accepts.
  for (const host of ["www.e-hentai.org", "g.e-hentai.org", "www.exhentai.org"])
    assert.equal(
      galleryURL(`https://${host}/g/1/abc?p=1`),
      `https://${host}/g/1/abc/`,
    );
  for (const host of ["nhentai.net", "www.nhentai.net"])
    assert.throws(() => galleryURL(`https://${host}/g/20/`));
  assert.throws(() => galleryURL("https://m.e-hentai.org/g/1/abc/"));
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
  await enqueue(storage, "https://e-hentai.org/g/20/abc/");
  assert.equal(request.url, "http://library.test:8780/api/downloads");
  assert.equal(request.headers.Authorization, "Bearer downloads-secret");
  assert.equal(request.credentials, "omit");
  assert.equal(request.redirect, "error");
  assert.deepEqual(JSON.parse(request.body), {
    url: "https://e-hentai.org/g/20/abc/",
  });
  assert.deepEqual(await openQueue(storage), {
    url: "http://library.test:8780/#downloads",
  });
});
test("only settings problems are configuration errors", async (t) => {
  await assert.rejects(
    enqueue({}, "https://e-hentai.org/g/20/abc/"),
    ConfigError,
  );
  await assert.rejects(openQueue(undefined), {
    message: messages.errorConfigure.message,
  });
  const fetch = t.mock.method(globalThis, "fetch", async () =>
    Response.json(
      { error: "root_unavailable", message: "Storage is offline" },
      { status: 503 },
    ),
  );
  await assert.rejects(
    enqueue(storage, "https://e-hentai.org/g/20/abc/"),
    (error) =>
      !(error instanceof ConfigError) && error.message === "Storage is offline",
  );
  fetch.mock.mockImplementation(async () => new Response("", { status: 401 }));
  await assert.rejects(
    enqueue(storage, "https://e-hentai.org/g/20/abc/"),
    ConfigError,
  );
  fetch.mock.mockImplementation(async () => {
    throw new TypeError("Failed to fetch");
  });
  await assert.rejects(enqueue(storage, "https://e-hentai.org/g/20/abc/"), {
    message: "Could not reach the library: Failed to fetch",
  });
});
test("the connection check reads the key's identity and keeps settings problems apart", async (t) => {
  let request;
  const fetch = t.mock.method(globalThis, "fetch", async (url, options) => {
    request = { url, ...options };
    return Response.json({
      id: "k1",
      kind: "downloads",
      label: "laptop",
      expires_at: null,
      rotation_due: false,
    });
  });
  const principal = await checkConnection(storage);
  assert.equal(request.url, "http://library.test:8780/api/auth/me");
  assert.equal(request.method, undefined);
  assert.equal(request.headers.Authorization, "Bearer downloads-secret");
  assert.equal(request.credentials, "omit");
  assert.equal(request.redirect, "error");
  assert.deepEqual([principal.kind, principal.label], ["downloads", "laptop"]);
  fetch.mock.mockImplementation(async () =>
    Response.json({ id: "k2", kind: "readonly", label: "reader" }),
  );
  await assert.rejects(
    checkConnection(storage),
    (error) =>
      error instanceof ConfigError &&
      error.message === messages.errorKeyReadOnly.message,
  );
  fetch.mock.mockImplementation(async () => new Response("", { status: 401 }));
  await assert.rejects(checkConnection(storage), ConfigError);
  fetch.mock.mockImplementation(async () =>
    Response.json(
      { error: "root_unavailable", message: "Storage is offline" },
      { status: 503 },
    ),
  );
  await assert.rejects(
    checkConnection(storage),
    (error) =>
      !(error instanceof ConfigError) && error.message === "Storage is offline",
  );
  fetch.mock.mockImplementation(async () => new Response("not json"));
  await assert.rejects(checkConnection(storage), {
    message: messages.errorResponse.message,
  });
  fetch.mock.mockImplementation(async () => {
    throw new TypeError("Failed to fetch");
  });
  await assert.rejects(checkConnection(storage), {
    message: "Could not reach the library: Failed to fetch",
  });
  await assert.rejects(
    checkConnection({ server: storage.server }),
    ConfigError,
  );
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

for (const [code, status, key] of [
  ["invalid_host", 400, "errorPublicOrigin"],
  ["read_only", 403, "errorKeyReadOnly"],
  ["invalid_credentials", 401, "errorKeyRejected"],
]) {
  test(`reachable configuration failure ${code} has localized guidance`, async (context) => {
    context.mock.method(globalThis, "fetch", async () =>
      Response.json({ error: code, message: "server detail" }, { status }),
    );
    await assert.rejects(enqueue(storage, "https://e-hentai.org/g/20/abc/"), {
      message: messages[key].message,
    });
    assert.ok(locale("zh_CN")[key]);
  });
}
test("busy responses retry at most twice and preserve the server message", async (context) => {
  let count = 0;
  context.mock.method(globalThis, "fetch", async () => {
    count++;
    return Response.json(
      { error: "library_busy", message: "Try later" },
      { status: 503, headers: { "Retry-After": "0" } },
    );
  });
  await assert.rejects(enqueue(storage, "https://e-hentai.org/g/20/abc/"), {
    message: "Try later",
  });
  assert.equal(count, 3);
});

test("all error codes in extension-facing backend layers receive a useful message", async (t) => {
  const codes = new Set();
  for (const file of [
    "errors.py",
    "http.py",
    "api_auth.py",
    "source_api.py",
    "downloads.py",
  ]) {
    const source = readFileSync(
      new URL(`../src/mangacouch/${file}`, import.meta.url),
      "utf8",
    );
    for (const pattern of [
      /\bcode\s*=\s*["']([a-z][a-z0-9_]*)["']/g,
      /\b(?:reject|error_response)\(\s*\d{3}\s*,\s*["']([a-z][a-z0-9_]*)["']/g,
      /["']error["']\s*:\s*["']([a-z][a-z0-9_]*)["']/g,
    ])
      for (const [, code] of source.matchAll(pattern)) codes.add(code);
  }
  assert.ok(codes.size > 10);
  let code;
  t.mock.method(globalThis, "fetch", async () =>
    Response.json(
      { error: code, message: `Server explains ${code}` },
      { status: 422 },
    ),
  );
  // A host the server refuses has its own guidance; every other code shows the server's text.
  for (code of codes)
    await assert.rejects(enqueue(storage, "https://e-hentai.org/g/20/abc/"), {
      message:
        code === "invalid_host"
          ? messages.errorPublicOrigin.message
          : `Server explains ${code}`,
    });
});

test("timeouts explain the deadline without claiming a redirect", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => {
    throw new DOMException("timed out", "TimeoutError");
  });
  await assert.rejects(checkConnection(storage), {
    message: messages.errorTimeout.message,
  });
  assert.equal(fetch.mock.callCount(), 1);
});
