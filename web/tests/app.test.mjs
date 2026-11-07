import { one, dispatch, attr, all } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { defaultPreferences } from "../src/PreferenceContext.tsx";
import { App } from "../src/App.tsx";
import { button, installGlobals, visibleText } from "./harness.mjs";
async function setup(t, options = {}) {
  let signedIn = !options.anonymous;
  const window = globalThis.window,
    document = globalThis.document;
  document.hidden = false;
  let hash = "#book/test-book";
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
  const restore = installGlobals({
    window,
    document,
    location,
    history: {
      state: null,
      go: () => {
        hash = "#book/test-book";
        queueMicrotask(() => window.dispatchEvent(new Event("hashchange")));
      },
      replaceState: (state, _title, url) => {
        globalThis.history.state = state;
        hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
      },
    },
    localStorage: { getItem: () => null, setItem: () => {} },
  });
  let book = {
    id: "test-book",
    metadata: {
      title: "Fixture",
      original_title: null,
      language: "中文",
      translated: null,
      work_type: null,
      tags: [],
      source: null,
      uploader: null,
      uploaded_at: null,
      source_rating: null,
      declared_page_count: null,
      declared_byte_size: null,
      parent: null,
      newer_versions: [],
      field_sources: {},
      protected_fields: [],
    },
    metadata_revision: 1,
    personal_revision: 1,
    content: { original_filename: "fixture.cbz", format: "cbz", page_count: 3 },
    personal: {
      rating: null,
      resume_page: null,
      completed: false,
      bookmarks: [],
    },
    review_state: "needs_review",
    sidecar: { pending_revision: 1, synced_revision: 0, last_error: null },
  };
  const json = (data) =>
    new Response(JSON.stringify(data), {
      headers: { "Content-Type": "application/json" },
    });
  let resolveSave;
  t.mock.method(globalThis, "fetch", async (path, options = {}) => {
    if (path === "/api/preferences") return json(defaultPreferences);
    if (path === "/api/auth/me")
      return signedIn
        ? json({ rotation_due: false })
        : new Response(JSON.stringify({ error: "unauthenticated" }), {
            status: 401,
          });
    if (path === "/api/auth/login") {
      signedIn = true;
      return json({});
    }
    if (path.includes("/pages/")) return new Response(new Blob(["image"]));
    if (path.startsWith("/api/sidecar/conflicts"))
      return json({ conflicts: [] });
    if (path.endsWith("/personal")) {
      const data = JSON.parse(options.body);
      const old = { ...book, personal: data.state, personal_revision: 2 };
      return new Promise((resolve) => {
        resolveSave = () => {
          book = { ...book, personal: old.personal, personal_revision: 2 };
          resolve(json(old));
        };
      });
    }
    if (path.endsWith("/review/complete")) {
      book = { ...book, review_state: "confirmed" };
      return json(book);
    }
    if (path.endsWith("/review"))
      return json({
        token: "token",
        state: book.review_state,
        metadata: book.metadata,
        candidates: {},
        issues: ["source_absent"],
        import_sources: [],
        warnings: [],
        ignored_fields: [],
        automatic: {
          rule: "local-confirmation/1",
          eligible: false,
          reasons: ["local_confirmation_missing"],
          evidence_event_id: null,
        },
        confirmation:
          book.review_state === "confirmed"
            ? { acknowledged_issues: ["source_absent"] }
            : null,
      });
    if (path === "/api/books/test-book") return json(book);
    if (path.startsWith("/api/books?"))
      return json({ books: [book], after_id: null });
    if (path === "/api/settings") return json({ max_upload_bytes: 1024 });
    throw Error(`Unexpected request: ${path}`);
  });
  let view;
  await act(async () => {
    view = render(React.createElement(App));
  });
  t.after(async () => {
    await act(async () => view.unmount());
    restore();
  });
  return {
    view,
    button: (text) => button(view, text),
    location,
    text: () => visibleText(view),
    finishSave: () => resolveSave(),
    window,
    signOut: () => {
      signedIn = false;
    },
    login: () =>
      act(async () =>
        dispatch(one(view.container, "form"), "submit", {
          preventDefault() {},
        }),
      ),
  };
}
test("late personal-save responses do not revert a newly confirmed review", async (t) => {
  const x = await setup(t);
  await act(async () =>
    dispatch(one(x.view.container, { className: "rating-input" }), "change", {
      target: { value: "4" },
    }),
  );
  await act(async () => dispatch(x.button("书籍操作"), "click"));
  await act(async () => dispatch(x.button("核对信息"), "click"));
  await act(async () =>
    dispatch(one(x.view.container, { type: "checkbox" }), "change", {
      target: { checked: true },
    }),
  );
  await act(async () => dispatch(x.button("完成核对"), "click"));
  assert.match(x.text(), /已记录你的核对结果/);
  await act(async () => x.finishSave());
  assert.match(x.text(), /已记录你的核对结果/);
  assert.equal(
    attr(one(x.view.container, { className: "rating-input" }), "value"),
    4,
  );
});
test("unsaved metadata blocks navigation until explicitly discarded", async (t) => {
  const x = await setup(t);
  await act(async () => dispatch(x.button("书籍操作"), "click"));
  await act(async () => dispatch(x.button("编辑元数据"), "click"));
  await act(async () =>
    dispatch(
      all(one(x.view.container, { className: "metadata-form" }), "input")[0],
      "change",
      { target: { value: "Draft title" } },
    ),
  );
  await act(async () =>
    dispatch(
      one(x.view.container, { className: "text-button back-link" }),
      "click",
    ),
  );
  assert.equal(x.location.hash, "#book/test-book");
  assert.match(x.text(), /请先完成正在进行的操作或处理未保存的修改/);
  await act(async () =>
    dispatch(one(x.view.container, { "aria-label": "关闭" }), "click"),
  );
  assert.match(x.text(), /放弃未保存的修改？/);
  assert.equal(
    attr(
      all(one(x.view.container, { className: "metadata-form" }), "input")[0],
      "value",
    ),
    "Draft title",
  );
  await act(async () => dispatch(x.button("放弃修改"), "click"));
  await act(async () =>
    dispatch(
      one(x.view.container, { className: "text-button back-link" }),
      "click",
    ),
  );
  assert.match(x.text(), /我的书架/);
});
test("a first anonymous visit shows the login form without a session-expired notice", async (t) => {
  const x = await setup(t, { anonymous: true });
  assert.match(x.text(), /打开书库/);
  assert.doesNotMatch(x.text(), /登录已过期|修改可能未保存/);
  await x.login();
  assert.doesNotMatch(x.text(), /打开书库/);
});
test("an expired session explains itself in the current language until the next sign-in", async (t) => {
  const x = await setup(t);
  x.signOut();
  await act(async () => x.window.dispatchEvent(new Event("mc-locked")));
  assert.match(x.text(), /登录已过期，尚未保存的修改可能未保存。/);
  assert.doesNotMatch(x.text(), /Session expired/);
  await x.login();
  assert.doesNotMatch(x.text(), /登录已过期/);
});
