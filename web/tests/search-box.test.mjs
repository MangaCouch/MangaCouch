import { one, dispatch, all, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import {
  SearchBox,
  SEARCH_DEBOUNCE_MS,
  SUGGESTION_DELAY_MS,
  suggestionRange,
  tokenAt,
} from "../src/SearchBox.tsx";
import { english as t, tick, visibleText } from "./harness.mjs";
const tag = {
  namespace: "artist",
  value: "blue sky",
  translation: "蓝天",
  count: 2,
};
test("the caret token is found across CJK text and apostrophes", () => {
  assert.deepEqual(tokenAt("你好 蓝", 4), { start: 3, end: 4 });
  assert.deepEqual(tokenAt("reader's 蓝", 10), { start: 9, end: 10 });
  assert.deepEqual(tokenAt('one artist:"blue s" remaining', 13), {
    start: 4,
    end: 19,
  });
});
test("combobox respects IME and selecting a suggestion searches immediately", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let current = "",
    submitted = "",
    changed = "";
  context.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(JSON.stringify({ items: [tag] }), {
        headers: { "Content-Type": "application/json" },
      }),
  );
  let view;
  function Host() {
    const [value, setValue] = React.useState("");
    current = value;
    return React.createElement(SearchBox, {
      value,
      change: (v) => {
        changed = v;
        setValue(v);
      },
      search: (v) => {
        submitted = v;
      },
      t,
    });
  }
  await act(async () => {
    view = render(React.createElement(Host));
  });
  context.after(async () => {
    await act(async () => view.unmount());
  });
  const input = () => one(view.container, "input");
  await act(async () =>
    dispatch(input(), "change", { target: { value: "蓝", selectionStart: 1 } }),
  );
  assert.equal(submitted, "");
  await tick(context, SUGGESTION_DELAY_MS);
  assert.equal(all(view.container, { role: "option" }).length, 1);
  await tick(context, SEARCH_DEBOUNCE_MS - SUGGESTION_DELAY_MS);
  assert.equal(submitted, "蓝");
  let prevented = 0;
  const key = (value) => ({
    key: value,
    nativeEvent: {},
    preventDefault: () => prevented++,
  });
  await act(async () => dispatch(input(), "compositionStart"));
  await act(async () => dispatch(input(), "keyDown", key("Enter")));
  await act(async () =>
    dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
  );
  await tick(context, SEARCH_DEBOUNCE_MS);
  assert.equal(submitted, "蓝");
  await act(async () =>
    dispatch(input(), "compositionEnd", { currentTarget: { value: "蓝" } }),
  );
  await tick(context, SEARCH_DEBOUNCE_MS);
  await act(async () => dispatch(input(), "keyDown", key("ArrowDown")));
  await act(async () => dispatch(input(), "keyDown", key("Enter")));
  assert.equal(current, 'artist:"blue sky"');
  assert.equal(changed, current);
  assert.equal(submitted, current);
  assert.equal(prevented, 2);
});
test("a chosen tag replaces every typed word of it, and nothing else", () => {
  const breasts = {
    namespace: "female",
    value: "big breasts",
    translation: "巨乳",
    count: 3,
  };
  const replaced = (value, caret, item = breasts) => {
    const { start, end } = suggestionRange(value, caret, item);
    return value.slice(start, end);
  };
  // The words before the caret word that begin the tag go with it.
  assert.equal(replaced("female:big br", 13), "female:big br");
  assert.equal(replaced("Female:Big Br", 13), "Female:Big Br");
  assert.equal(replaced("big br", 6), "big br");
  // Earlier terms that are not part of the tag stay, quoted phrases included.
  assert.equal(replaced("lang:en female:big br", 21), "female:big br");
  assert.equal(replaced("red br", 6), "br");
  assert.equal(replaced('title:"big day" br', 18), "br");
  // An open quote already keeps the typed tag in one word.
  assert.equal(replaced('female:"big br', 14), 'female:"big br');
  // The caret may sit before later terms; they stay.
  assert.equal(replaced("female:big br other", 13), "female:big br");
  // Chinese namespace names and translations count as typed forms of the tag.
  assert.equal(replaced("女性:big br", 10), "女性:big br");
  assert.equal(replaced("女性:巨", 4), "女性:巨");
  assert.equal(
    replaced("作者:blue s", 9, { ...tag, namespace: "artist" }),
    "作者:blue s",
  );
  assert.equal(replaced("你好 蓝", 4, tag), "蓝");
});
test("choosing a multi-word tag leaves no fragment of it behind in the search", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const breasts = {
    namespace: "female",
    value: "big breasts",
    translation: null,
    count: 3,
  };
  context.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(JSON.stringify({ items: [breasts] }), {
        headers: { "Content-Type": "application/json" },
      }),
  );
  let submitted = "";
  let view;
  await act(async () => {
    view = render(
      React.createElement(SearchBox, {
        value: "",
        search: (v) => {
          submitted = v;
        },
        t,
      }),
    );
  });
  context.after(async () => {
    await act(async () => view.unmount());
  });
  const input = () => one(view.container, "input");
  const typed = "lang:en female:big br other";
  await act(async () =>
    dispatch(input(), "change", {
      target: { value: typed, selectionStart: 21 },
    }),
  );
  await tick(context, SUGGESTION_DELAY_MS);
  assert.equal(all(view.container, { role: "option" }).length, 1);
  await act(async () =>
    dispatch(all(view.container, { role: "option" })[0], "click"),
  );
  assert.equal(submitted, 'female:"big breasts" lang:en other');
  assert.equal(attr(input(), "value"), "lang:en other");
});
test("suggestions of the previous fragment disappear as soon as the fragment changes", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  context.mock.method(globalThis, "fetch", async (path) => {
    const query = new URL(path, "http://x").searchParams.get("query");
    return new Response(
      JSON.stringify({
        items: [
          {
            namespace: "artist",
            value: `${query}-tag`,
            translation: null,
            count: 1,
          },
        ],
      }),
      { headers: { "Content-Type": "application/json" } },
    );
  });
  let view;
  await act(async () => {
    view = render(
      React.createElement(SearchBox, { value: "", search() {}, t }),
    );
  });
  context.after(async () => {
    await act(async () => view.unmount());
  });
  const type = (value) =>
    act(async () =>
      dispatch(one(view.container, "input"), "change", {
        target: { value, selectionStart: value.length },
      }),
    );
  await type("ab");
  await tick(context, SUGGESTION_DELAY_MS);
  assert.match(visibleText(view), /ab-tag/);
  await type("xy");
  assert.doesNotMatch(visibleText(view), /ab-tag/);
  assert.match(visibleText(view), /Searching/);
  await tick(context, SUGGESTION_DELAY_MS);
  assert.match(visibleText(view), /xy-tag/);
});
