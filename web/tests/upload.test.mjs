import { one, dispatch, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { App } from "../src/App.tsx";
import { ApiError } from "../src/api.ts";
import { storageKey } from "../src/device.ts";
import { button, click, setupFixture, visibleText } from "./harness.mjs";
async function setup(t) {
  const window = globalThis.window,
    document = globalThis.document;
  document.hidden = false;
  window.matchMedia = () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  });
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
  const x = await setupFixture(t, () => {}, {
    window,
    globals: {
      document,
      location,
      history: {
        state: null,
        go: () => {
          hash = "";
          queueMicrotask(() => window.dispatchEvent(new Event("hashchange")));
        },
        replaceState: (state, __, value) => {
          globalThis.history.state = state;
          hash = value.includes("#") ? value.slice(value.indexOf("#")) : "";
        },
      },
      localStorage: {
        getItem: (key) => (key === storageKey("language") ? "en" : null),
        setItem: () => {},
      },
    },
  });
  const pending = [];
  const fixtureFetch = x.window.fetch;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    if (path.startsWith("/api/import?")) {
      let resolve, reject;
      const response = new Promise((yes, no) => {
        resolve = yes;
        reject = no;
      });
      pending.push({
        resolve,
        reject,
        signal: options.signal,
        body: options.body,
      });
      return { ok: true, json: () => response };
    }
    return fixtureFetch(path, options);
  });
  const view = await x.render(React.createElement(App));
  const change = (files = [new File(["synthetic"], "sample.cbz")]) =>
    dispatch(one(view.container, { type: "file" }), "change", {
      target: { files },
    });
  return {
    view,
    window,
    location,
    pending,
    change,
    button: (text) => button(view, text),
    click: (name) => click(button(view, name)),
    finish: async (created = true) =>
      act(async () =>
        pending[0].resolve({ book: x.fixture.books[0], created }),
      ),
    text: () => visibleText(view),
  };
}
test("upload rejects immediate duplicate selection and guards navigation and tab close", async (t) => {
  const x = await setup(t);
  await act(async () => {
    x.change();
    x.change();
  });
  assert.equal(x.pending.length, 1);
  const leaving = new Event("beforeunload", { cancelable: true });
  x.window.dispatchEvent(leaving);
  assert.equal(leaving.defaultPrevented, true);
  await x.click("Collections");
  assert.equal(x.location.hash, "");
  assert.match(x.text(), /Uploading and checking/);
  await x.finish();
  assert.equal(x.location.hash, "#book/fixture-0");
});
test("cancelled upload cannot navigate when a parsed success response arrives late", async (t) => {
  const x = await setup(t);
  await act(async () => x.change());
  await x.click("Cancel");
  assert.equal(x.pending[0].signal.aborted, true);
  await x.finish();
  assert.equal(x.location.hash, "");
  assert.match(x.text(), /Upload stopped/);
  assert.equal(attr(x.button("Upload books"), "disabled"), false);
});
test("forced session lock during upload clears page and ignores late upload success", async (t) => {
  const x = await setup(t);
  await act(async () => x.change());
  await act(async () => x.window.dispatchEvent(new Event("mc-locked")));
  assert.equal(x.pending[0].signal.aborted, true);
  await x.finish();
  assert.equal(x.location.hash, "");
  assert.ok(x.button("Open library"));
  assert.doesNotMatch(x.text(), /fixture-0/);
});
test("lost upload response reports uncertain result without retrying automatically", async (t) => {
  const x = await setup(t);
  await act(async () => x.change());
  await act(async () =>
    x.pending[0].reject(new TypeError("fixture connection lost")),
  );
  assert.equal(x.pending.length, 1);
  assert.match(x.text(), /may already be imported/);
  assert.equal(attr(x.button("Upload books"), "disabled"), false);
});
test("paired upload submits all selected files and duplicate feedback precedes opening existing book", async (t) => {
  const x = await setup(t);
  await act(async () =>
    x.change([
      new File(["archive"], "sample.cbz"),
      new File(["{}"], "sample.cbz.mangacouch.json"),
      new File(["{}"], "sample.eze.json"),
    ]),
  );
  const body = x.pending[0].body;
  assert.ok(body instanceof FormData);
  assert.deepEqual([...body.keys()], ["native", "eze", "archive"]);
  assert.equal(body.get("archive").name, "upload.cbz");
  await x.finish(false);
  assert.equal(x.location.hash, "");
  assert.match(x.text(), /JSON files did not overwrite/);
  assert.equal(attr(x.button("Upload books"), "disabled"), false);
  await x.click("View existing book");
  assert.equal(x.location.hash, "#book/fixture-0");
});
test("mismatched JSON selection is rejected without uploading or blocking retry", async (t) => {
  const x = await setup(t);
  await act(async () =>
    x.change([
      new File(["archive"], "sample.cbz"),
      new File(["{}"], "another.json"),
    ]),
  );
  assert.equal(x.pending.length, 0);
  assert.match(x.text(), /JSON names must match/);
  await act(async () => x.change());
  assert.equal(x.pending.length, 1);
  await x.finish();
});
test("disk-full response keeps upload retry explicit and does not claim rollback", async (t) => {
  const x = await setup(t);
  await act(async () => x.change());
  await act(async () => x.pending[0].reject(new ApiError(507)));
  assert.match(x.text(), /server is low on disk space/);
  assert.match(x.text(), /may already be imported/);
  assert.equal(x.pending.length, 1);
  assert.equal(x.location.hash, "");
  assert.equal(attr(x.button("Upload books"), "disabled"), false);
  await x.click("Refresh library");
  assert.equal(x.pending.length, 1);
  assert.doesNotMatch(x.text(), /server is low on disk space/);
});
