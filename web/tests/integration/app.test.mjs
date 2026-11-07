import { all, one, dispatch, attr } from "../dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createInterface } from "node:readline";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import React from "react";
import { render, act } from "@testing-library/react";
import { App } from "../../src/App.tsx";
import { storageKey } from "../../src/device.ts";
import {
  button as findButton,
  installGlobals,
  nodeText,
  visibleText,
} from "../harness.mjs";
const repo = fileURLToPath(new URL("../../../", import.meta.url));
async function setup(t) {
  let view;
  const restores = [];
  const child = spawn(
    process.env.MANGACOUCH_TEST_PYTHON ||
      `${repo}.venv/${process.platform === "win32" ? "Scripts/python.exe" : "bin/python"}`,
    ["-B", `${repo}tests/web_integration.py`],
    {
      cwd: repo,
      env: {
        ...process.env,
        PYTHONPATH: `${repo}src`,
        PYTHONDONTWRITEBYTECODE: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  let stderr = "",
    serial = 0;
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  const pending = new Map();
  let ready;
  const started = new Promise((resolve, reject) => {
    ready = resolve;
    child.once("error", reject);
    child.once("exit", (code) => {
      const error = new Error(`ASGI worker exited (${code}): ${stderr}`);
      reject(error);
      pending.forEach(({ reject }) => reject(error));
    });
  });
  const lines = createInterface({ input: child.stdout });
  lines.on("line", (line) => {
    const result = JSON.parse(line);
    if (result.ready) ready(result);
    else {
      const item = pending.get(result.id);
      pending.delete(result.id);
      item?.resolve(result);
    }
  });
  t.after(async () => {
    try {
      if (view) await act(async () => view.unmount());
    } finally {
      restores.forEach((restore) => restore());
      if (child.exitCode === null && child.signalCode === null) {
        const exited = once(child, "exit");
        child.stdin.end();
        const timer = setTimeout(() => child.kill(), 3000);
        try {
          await exited;
        } finally {
          clearTimeout(timer);
        }
      }
      lines.close();
    }
    assert.equal(child.exitCode, 0, stderr);
  });
  const fixture = await started;
  const calls = [];
  async function fetch(path, options = {}) {
    options.signal?.throwIfAborted();
    const id = ++serial;
    const headers = Object.fromEntries(new Headers(options.headers));
    const method = options.method || "GET";
    if (method !== "GET" && method !== "HEAD")
      headers.Origin = "http://integration.test";
    let body;
    if (options.body instanceof FormData) {
      const encoded = new Response(options.body);
      headers["Content-Type"] = encoded.headers.get("content-type");
      body = Buffer.from(await encoded.arrayBuffer());
    } else
      body =
        options.body instanceof Blob
          ? Buffer.from(await options.body.arrayBuffer())
          : Buffer.from(options.body || "");
    const response = new Promise((resolve, reject) =>
      pending.set(id, { resolve, reject }),
    );
    child.stdin.write(
      JSON.stringify({
        id,
        path,
        method,
        headers,
        body: body.toString("base64"),
      }) + "\n",
    );
    const result = await response;
    calls.push({ path, method, status: result.status });
    options.signal?.throwIfAborted();
    return new Response(
      [204, 304].includes(result.status)
        ? null
        : Buffer.from(result.body, "base64"),
      { status: result.status, headers: result.headers },
    );
  }
  const window = globalThis.window,
    document = globalThis.document;
  document.hidden = false;
  let hash = "";
  const location = {
    pathname: "/",
    search: "",
    get hash() {
      return hash;
    },
    set hash(value) {
      hash = value.startsWith("#") ? value : `#${value}`;
      queueMicrotask(() => window.dispatchEvent(new Event("hashchange")));
    },
  };
  window.matchMedia = () =>
    Object.assign(new EventTarget(), { matches: false });
  const stored = new Map([[storageKey("language"), "en"]]);
  const restore = installGlobals({
    window,
    document,
    location,
    history: {
      replaceState: (_, __, value) => {
        hash = value.includes("#") ? value.slice(value.indexOf("#")) : "";
      },
    },
    localStorage: {
      getItem: (key) => stored.get(key) ?? null,
      setItem: (key, value) => stored.set(key, value),
    },
  });
  restores.push(restore);
  t.mock.method(globalThis, "fetch", fetch);
  await act(async () => {
    view = render(React.createElement(App));
  });
  const text = () => visibleText(view);
  async function until(predicate) {
    for (let attempt = 0; attempt < 300; attempt++) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 10));
      });
      if (predicate()) return;
    }
    assert.fail(
      `UI did not settle; calls: ${JSON.stringify(calls)}; screen: ${text().slice(0, 1000)}`,
    );
  }
  const button = (name) => findButton(view, name);
  const status = (value) =>
    all(view.container, { role: "status" }).some(
      (node) => nodeText(node) === value,
    );
  async function login() {
    await until(() => !!button("Open library"));
    await act(async () =>
      dispatch(one(view.container, { type: "password" }), "change", {
        target: { value: "integration-fixture-only" },
      }),
    );
    await act(async () =>
      dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
    );
    await until(() => !!button("Upload books"));
  }
  async function upload(name, metadata = []) {
    await act(async () =>
      dispatch(one(view.container, { type: "file" }), "change", {
        target: {
          files: [
            new File([Buffer.from(fixture.archive, "base64")], name),
            ...metadata,
          ],
        },
      }),
    );
    await until(() => !!button("Book actions"));
    return location.hash.split("/")[1];
  }
  const json = async (path, options = {}) => {
    let result;
    await act(async () => {
      const response = await fetch(path, {
        ...options,
        headers: {
          "X-MangaCouch-Request": "1",
          "Content-Type": "application/json",
          ...options.headers,
        },
      });
      assert.equal(response.status, 200, `${options.method || "GET"} ${path}`);
      result = await response.json();
    });
    return result;
  };
  return {
    view,
    text,
    status,
    until,
    login,
    upload,
    json,
    fetch,
    calls,
    location,
    button,
    fixture,
    click: async (name) => {
      if (name === "Edit") {
        await act(async () => dispatch(button("Book actions"), "click"));
        name = "Edit metadata";
      }
      await until(() => !!button(name) && !attr(button(name), "disabled"));
      await act(async () => dispatch(button(name), "click"));
    },
  };
}
test(
  "React App to real ASGI: login, long-name upload, edit, rating, sync and duplicate reuse",
  { timeout: 20000 },
  async (t) => {
    const x = await setup(t);
    await x.login();
    const name = "漫画😀".repeat(110) + ".cbz";
    const id = await x.upload(name);
    assert.match(x.text(), /Tags/);
    let book = await x.json(`/api/books/${id}`);
    assert.equal(book.content.original_filename, name);
    assert.equal(book.review_state, "needs_review");
    assert.equal(book.personal.resume_page, null);
    await x.click("Edit");
    await act(async () =>
      dispatch(
        all(one(x.view.container, { className: "metadata-form" }), "input")[0],
        "change",
        { target: { value: "集成测试标题" } },
      ),
    );
    await act(async () =>
      dispatch(
        one(x.view.container, { className: "metadata-form" }),
        "submit",
        { preventDefault() {} },
      ),
    );
    await x.until(() => x.text().includes("Saved."));
    await act(async () =>
      dispatch(one(x.view.container, { className: "rating-input" }), "change", {
        target: { value: "4.5" },
      }),
    );
    await x.until(() =>
      x.calls.some(
        (call) => call.path.endsWith("/personal") && call.status === 200,
      ),
    );
    await x.until(() =>
      x.calls.some(
        (call) => call.path === "/api/sidecar/sync" && call.status === 200,
      ),
    );
    book = await x.json(`/api/books/${id}`);
    assert.equal(book.metadata.title, "集成测试标题");
    assert.equal(book.personal.rating, 4.5);
    assert.equal(book.sidecar.pending_revision, book.sidecar.synced_revision);
    await act(async () => {
      x.location.hash = "";
    });
    await x.until(() => !!x.button("Upload books"));
    assert.equal(await x.upload("different-name.zip"), id);
    assert.equal((await x.json(`/api/books/${id}`)).personal.rating, 4.5);
    assert.equal((await x.json("/api/books")).books.length, 1);
  },
);
test(
  "React App to real ASGI: stale metadata is rejected, the draft is kept and Reload latest shows newer data",
  { timeout: 20000 },
  async (t) => {
    const x = await setup(t);
    await x.login();
    const id = await x.upload("sample.cbz");
    const book = await x.json(`/api/books/${id}`);
    await x.json(`/api/books/${id}/metadata`, {
      method: "PUT",
      body: JSON.stringify({
        expected_revision: book.metadata_revision,
        metadata: { ...book.metadata, title: "Other tab saved" },
      }),
    });
    await x.click("Edit");
    await act(async () =>
      dispatch(
        all(one(x.view.container, { className: "metadata-form" }), "input")[0],
        "change",
        { target: { value: "Stale local draft" } },
      ),
    );
    await act(async () =>
      dispatch(
        one(x.view.container, { className: "metadata-form" }),
        "submit",
        { preventDefault() {} },
      ),
    );
    await x.until(() => x.text().includes("Another device changed this item"));
    assert.equal(
      attr(
        all(one(x.view.container, { className: "metadata-form" }), "input")[0],
        "value",
      ),
      "Stale local draft",
    );
    assert.equal(
      (await x.json(`/api/books/${id}`)).metadata.title,
      "Other tab saved",
    );
    // The newer server version is applied at once; the draft stays until reloaded.
    await x.click("Reload latest");
    await x.until(
      () =>
        attr(
          all(
            one(x.view.container, { className: "metadata-form" }),
            "input",
          )[0],
          "value",
        ) === "Other tab saved",
    );
  },
);
test(
  "React paired native/Eze upload adopts bound metadata, keeps candidate and reports duplicate preservation",
  { timeout: 20000 },
  async (t) => {
    const x = await setup(t);
    await x.login();
    const bytes = Buffer.from(x.fixture.archive, "base64");
    const native = new File(
      [
        JSON.stringify({
          schema: "mangacouch.native/1",
          revision: 8,
          content_sha256: createHash("sha256").update(bytes).digest("hex"),
          metadata: { title: "Paired native title", language: "ja" },
        }),
      ],
      "paired.cbz.mangacouch.json",
    );
    const eze = new File(
      [JSON.stringify({ gallery_info: { title: "Eze candidate" } })],
      "paired.eze.json",
    );
    const id = await x.upload("paired.cbz", [native, eze]);
    assert.equal(
      (await x.json(`/api/books/${id}`)).metadata.title,
      "Paired native title",
    );
    assert.match(x.text(), /Paired native title/);
    const preview = await x.json(`/api/books/${id}/review`);
    assert.ok(preview.import_sources.includes("native"));
    assert.ok(preview.candidates["eze:info"].fields.includes("title"));
    const before = await x.json(`/api/books/${id}`);
    await act(async () => {
      x.location.hash = "";
    });
    await x.until(() => !!x.button("Upload books"));
    await act(async () =>
      dispatch(one(x.view.container, { type: "file" }), "change", {
        target: { files: [new File([bytes], "paired.cbz"), native, eze] },
      }),
    );
    await x.until(() => !!x.button("View existing book"));
    assert.match(x.location.hash, /^#?$/);
    assert.match(x.text(), /JSON files did not overwrite/);
    assert.deepEqual(await x.json(`/api/books/${id}`), before);
    await x.click("View existing book");
    await x.until(() => x.location.hash === `#book/${id}`);
  },
);
test(
  "single settings page autosaves defaults through the real API",
  { timeout: 20000 },
  async (t) => {
    const x = await setup(t);
    await x.login();
    await x.click("Settings");
    assert.equal(
      all(x.view.container, { className: "settings-category panel" }).length,
      5,
    );
    await act(async () =>
      dispatch(
        all(x.view.container, "*").find(
          (n) =>
            n.tagName === "SELECT" && attr(n, "aria-label") === "Title display",
        ),
        "change",
        { target: { value: "original_title" } },
      ),
    );
    await x.until(() =>
      x.calls.some(
        (c) =>
          c.path === "/api/preferences" &&
          c.method === "PUT" &&
          c.status === 200,
      ),
    );
    assert.equal(
      (await x.json("/api/preferences")).settings.title_preference,
      "original_title",
    );
    assert.equal(x.button("Save shared preferences"), undefined);
  },
);
test(
  "bulk rating from library selection persists through real API",
  { timeout: 20000 },
  async (t) => {
    const x = await setup(t);
    await x.login();
    const id = await x.upload("bulk.cbz");
    await x.click("Library");
    await x.until(() =>
      all(x.view.container, "button").some((n) =>
        attr(n, "className")?.includes("book-select"),
      ),
    );
    await act(async () =>
      dispatch(
        all(x.view.container, "button").find((n) =>
          attr(n, "className")?.includes("book-select"),
        ),
        "click",
        { stopPropagation() {} },
      ),
    );
    await x.click("Bulk actions");
    await act(async () =>
      dispatch(one(one(x.view.container, "dialog"), "select"), "change", {
        target: { value: "rating" },
      }),
    );
    await act(async () =>
      dispatch(
        one(one(x.view.container, "dialog"), { className: "rating-input" }),
        "change",
        { target: { value: "4" } },
      ),
    );
    await x.click("Apply");
    // The server applies the job in the background; "Complete" appears once polling
    // GET /api/bulk/{id} reports it done.
    await x.until(() => x.status("Complete"));
    assert.ok(
      x.calls.some(
        (call) =>
          call.method === "GET" &&
          /^\/api\/bulk\/[^/?]+/.test(call.path) &&
          call.status === 200,
      ),
    );
    assert.ok(!x.calls.some((call) => call.path.includes("/step")));
    assert.equal((await x.json(`/api/books/${id}`)).personal.rating, 4);
  },
);
