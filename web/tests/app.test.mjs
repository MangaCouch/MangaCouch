import { one, dispatch, attr, all } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { defaultPreferences } from "../src/PreferenceContext.tsx";
import { App } from "../src/App.tsx";
import {
  button,
  hashRouting,
  installGlobals,
  visibleText,
} from "./harness.mjs";
async function setup(t, options = {}) {
  let signedIn = !options.anonymous;
  const authError = options.authError;
  const window = globalThis.window,
    document = globalThis.document;
  document.hidden = false;
  const { location, history } = hashRouting(
    window,
    options.route ?? "#book/test-book",
  );
  window.matchMedia = () =>
    Object.assign(new EventTarget(), { matches: false });
  const restore = installGlobals({
    window,
    document,
    location,
    history,
    localStorage: { getItem: () => null, setItem: () => {} },
    // A first visit follows the browser's language; these tests read the Chinese UI.
    navigator: {
      language: options.language ?? "zh-CN",
      languages: [options.language ?? "zh-CN"],
      userAgent: globalThis.navigator.userAgent,
    },
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
    content: {
      original_filename: "fixture.cbz",
      format: "cbz",
      page_count: options.count ?? 3,
    },
    personal: {
      rating: null,
      resume_page: null,
      completed: false,
    },
    review_state: "needs_review",
    ...(options.availability && { availability: options.availability }),
  };
  const json = (data) =>
    new Response(JSON.stringify(data), {
      headers: { "Content-Type": "application/json" },
    });
  let resolveSave;
  t.mock.method(globalThis, "fetch", async (path, options = {}) => {
    if (path === "/api/preferences") return json(defaultPreferences);
    if (path === "/api/auth/me" && authError)
      return new Response(JSON.stringify({ error: authError }), {
        status: 403,
      });
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
    if (path.startsWith("/api/sidecar/conflicts")) return json({ items: [] });
    if (path.includes("/personal?")) {
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
            ? {
                action: "manual_confirm",
                acknowledged_issues: ["source_absent"],
              }
            : null,
      });
    if (path === "/api/books/test-book") return json(book);
    if (path.startsWith("/api/books?"))
      return json({ items: [book], after: null });
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
    history,
    resumeAndRemount: async (page) => {
      await act(async () => view.unmount());
      book = { ...book, personal: { ...book.personal, resume_page: page } };
      await act(async () => {
        view = render(React.createElement(App));
      });
    },
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
  await act(async () => dispatch(x.button("核对元数据"), "click"));
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

test("a first visit from an English browser opens in English", async (t) => {
  const x = await setup(t, { anonymous: true, language: "en-GB" });
  assert.match(x.text(), /Open library/);
  assert.doesNotMatch(x.text(), /打开书库/);
});

for (const [availability, label] of [
  ["missing", "文件缺失"],
  ["changed", "文件已被修改"],
  ["unreadable", "文件无法读取"],
])
  test(`a ${availability} book's reader route names the book and the reason instead of reading`, async (t) => {
    const x = await setup(t, { route: "#read/test-book", availability });
    const text = x.text();
    assert.match(text, /Fixture/);
    assert.match(text, new RegExp(label));
    // The raw availability value is not shown as the reason.
    assert.doesNotMatch(text, new RegExp(`[:：]\\s*${availability}`));
    assert.ok(x.button("检查文件"));
  });

test("explicit reader page is consumed without losing history position", async (t) => {
  const x = await setup(t, { route: "#read/test-book/page/4", count: 12 });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 100));
  });
  assert.equal(x.location.hash, "#read/test-book");
  assert.match(x.text(), /4\s*\/\s*12/);
  const position = x.history.state.mcPosition;
  // The server now has progress saved by a later turn; the consumed URL must not override it.
  await x.resumeAndRemount(9);
  assert.equal(x.location.hash, "#read/test-book");
  assert.equal(x.history.state.mcPosition, position);
  assert.match(x.text(), /10\s*\/\s*12/);
});

test("initial authentication retains the server failure reason", async (t) => {
  const x = await setup(t, { authError: "storage_unavailable" });
  assert.match(x.text(), /存储/);
  assert.doesNotMatch(x.text(), /无法连接服务器/);
});

test("the logo on the plain library adds no history entry; elsewhere it opens the library once", async (t) => {
  const x = await setup(t, { route: "" });
  const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
  const click = () =>
    act(async () => {
      dispatch(one(x.view.container, { className: "wordmark" }), "click", {
        button: 0,
        preventDefault() {},
      });
      await settle();
    });
  const length = x.history.length;
  await click();
  await click();
  assert.equal(x.history.length, length);
  assert.equal(x.location.href, "http://localhost/");
  // With a search in the URL, the logo clears it with exactly one new entry.
  await act(async () => {
    x.history.pushState(null, "", "/?query=artist%3Alin");
    x.window.dispatchEvent(new Event("popstate"));
    await settle();
  });
  const filtered = x.history.length;
  await click();
  assert.equal(x.location.search, "");
  assert.equal(x.history.length, filtered + 1);
  await click();
  assert.equal(x.history.length, filtered + 1);
});
