import { all, attr, one, dispatch } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { TagEditor } from "../src/TagEditor.tsx";
import { namespaceKey } from "../src/Pills.tsx";
import { english as t } from "./harness.mjs";
const json = (value, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
test("translated namespace names resolve to their keys", () => {
  assert.equal(namespaceKey("作者"), "artist");
  assert.equal(namespaceKey(" 社团 "), "group");
  assert.equal(namespaceKey("艺术家"), "artist");
  assert.equal(namespaceKey("Coser"), "cosplayer");
  assert.equal(namespaceKey("分类"), "category");
  assert.equal(namespaceKey("artist"), "artist");
  assert.equal(namespaceKey("custom"), "custom");
  assert.equal(namespaceKey(""), "other");
});
async function editor(context, initial = []) {
  context.mock.method(globalThis, "fetch", async () => json({ items: [] }));
  let tags = initial,
    unsent = null,
    view;
  function Host() {
    const [value, setValue] = React.useState(initial);
    tags = value;
    return React.createElement(TagEditor, {
      value,
      change: setValue,
      pending: (draft) => (unsent = draft),
      t,
    });
  }
  await act(async () => {
    view = render(React.createElement(Host));
  });
  context.after(async () => {
    await act(async () => view.unmount());
  });
  return {
    view,
    tags: () => tags,
    pending: () => unsent,
    input: () => one(view.container, "input"),
    tag: (index) => all(view.container, ".tag-edit")[index],
    key: (node, key) =>
      act(async () => dispatch(node, "keyDown", { key, nativeEvent: {} })),
    type: (value) =>
      act(async () =>
        dispatch(one(view.container, "input"), "change", {
          target: { value },
        }),
      ),
  };
}
const two = [
  { namespace: "artist", value: "a" },
  { namespace: "group", value: "b" },
];
test("a single click on a tag opens it for editing; Enter replaces it in place", async (context) => {
  const x = await editor(context, two);
  assert.equal(attr(x.tag(0), "aria-label"), "Edit tag artist:a");
  await act(async () => dispatch(x.tag(0), "click"));
  assert.equal(attr(x.input(), "value"), "a");
  assert.equal(attr(x.input(), "aria-label"), "Tag value");
  assert.equal(globalThis.document.activeElement, x.input());
  // Opening a tag without changing it leaves nothing unsent.
  assert.equal(x.pending(), null);
  await x.type("a2");
  assert.deepEqual(x.pending(), {
    tags: [{ namespace: "artist", value: "a2" }],
    replace: 0,
  });
  await x.key(x.input(), "Enter");
  assert.deepEqual(x.tags(), [
    { namespace: "artist", value: "a2" },
    { namespace: "group", value: "b" },
  ]);
  assert.equal(x.pending(), null);
});
test("tags are keyboard reachable: Enter or F2 edits, Escape cancels, Delete removes", async (context) => {
  const x = await editor(context, two);
  // Each tag is a button, so Tab reaches it and Enter or Space activates it.
  assert.equal(x.tag(1).tagName, "BUTTON");
  await x.key(x.tag(1), "F2");
  assert.equal(attr(x.input(), "value"), "b");
  await x.type("changed");
  assert.ok(x.pending());
  // One Escape leaves the tag as it was and returns focus to it.
  await x.key(x.input(), "Escape");
  assert.deepEqual(x.tags(), two);
  assert.equal(attr(x.input(), "value"), "");
  assert.equal(x.pending(), null);
  assert.equal(globalThis.document.activeElement, x.tag(1));
  await x.key(x.tag(0), "Delete");
  assert.deepEqual(x.tags(), [{ namespace: "group", value: "b" }]);
  await x.key(x.tag(0), "Backspace");
  assert.deepEqual(x.tags(), []);
});
test("removing the tag being edited drops its draft; removing an earlier one keeps it", async (context) => {
  const x = await editor(context, [
    ...two,
    { namespace: "parody", value: "c" },
  ]);
  await act(async () => dispatch(x.tag(2), "click"));
  await x.type("c2");
  await x.key(x.tag(0), "Delete");
  // The draft still replaces the same tag, now one place earlier.
  assert.deepEqual(x.pending(), {
    tags: [{ namespace: "parody", value: "c2" }],
    replace: 1,
  });
  await x.key(x.tag(1), "Delete");
  assert.equal(x.pending(), null);
  assert.deepEqual(x.tags(), [{ namespace: "group", value: "b" }]);
});
test("a tag typed with a Chinese namespace is stored under its English key", async (context) => {
  const x = await editor(context);
  await act(async () =>
    dispatch(x.input(), "change", { target: { value: "作者:蓝天" } }),
  );
  await act(async () =>
    dispatch(x.input(), "keyDown", { key: "Enter", nativeEvent: {} }),
  );
  assert.deepEqual(x.tags(), [{ namespace: "artist", value: "蓝天" }]);
  await x.type("社团:blue");
  await x.key(x.input(), "Enter");
  assert.deepEqual(x.tags(), [
    { namespace: "artist", value: "蓝天" },
    { namespace: "group", value: "blue" },
  ]);
});
test("pasting comma or line separated tags adds each of them", async (context) => {
  const x = await editor(context, [{ namespace: "artist", value: "a" }]);
  await act(async () =>
    dispatch(x.input(), "paste", {
      clipboardData: {
        getData: () => "artist:a, group:b\n作者:c, plain,  , parody: d e ",
      },
    }),
  );
  assert.deepEqual(x.tags(), [
    { namespace: "artist", value: "a" },
    { namespace: "group", value: "b" },
    { namespace: "artist", value: "c" },
    { namespace: "other", value: "plain" },
    { namespace: "parody", value: "d e" },
  ]);
  // A single tag pastes into the input as plain text.
  await act(async () =>
    dispatch(x.input(), "paste", { clipboardData: { getData: () => "x:y" } }),
  );
  assert.equal(x.tags().length, 5);
});

test("a bare value suggests full tags across namespaces and choosing stores the namespace", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const x = await editor(context);
  const queries = [];
  context.mock.method(globalThis, "fetch", async (url) => {
    queries.push(String(url));
    return json({
      items: [
        { namespace: "artist", value: "blue", translation: "蓝", count: 2 },
        {
          namespace: "group",
          value: "blue sky",
          translation: "蓝天",
          count: 1,
        },
      ],
    });
  });
  await x.type("blue");
  await act(async () => context.mock.timers.tick(120));
  assert.ok(queries[0].includes("query=blue&"));
  const options = all(x.view.container, '[role="option"]');
  assert.equal(options.length, 2);
  assert.match(options[1].textContent, /group:/);
  await act(async () => dispatch(options[1], "click"));
  assert.deepEqual(x.tags(), [{ namespace: "group", value: "blue sky" }]);
  await x.type("custom value");
  await x.key(x.input(), "Enter");
  assert.deepEqual(x.tags()[1], { namespace: "other", value: "custom value" });
});

test("keyboard suggestion selection saves only the tag identity", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const x = await editor(context);
  context.mock.method(globalThis, "fetch", async () =>
    json({
      items: [
        { namespace: "artist", value: "blue", translation: "蓝", count: 2 },
      ],
    }),
  );
  await x.type("blu");
  await act(async () => context.mock.timers.tick(120));
  await x.key(x.input(), "ArrowDown");
  await x.key(x.input(), "Enter");
  assert.deepEqual(x.tags(), [{ namespace: "artist", value: "blue" }]);
});

test("renaming a namespace keeps the existing value and rejects 129 characters", async (context) => {
  const x = await editor(context, [
    { namespace: "artist", value: "Keep this" },
  ]);
  await act(async () => dispatch(one(x.tag(0), "strong"), "doubleClick", {}));
  await x.type("x".repeat(129));
  await x.key(x.input(), "Enter");
  assert.equal(x.tags()[0].namespace, "artist");
  assert.match(x.view.container.textContent, /128/);
  await x.type("x".repeat(128));
  await x.key(x.input(), "Enter");
  assert.equal(attr(x.input(), "value"), "Keep this");
  await x.key(x.input(), "Enter");
  assert.deepEqual(x.tags(), [
    { namespace: "x".repeat(128), value: "Keep this" },
  ]);
});

test("renaming a namespace by typing a colon keeps the existing value", async (context) => {
  const x = await editor(context, [
    { namespace: "artist", value: "Keep this" },
  ]);
  await act(async () => dispatch(one(x.tag(0), "strong"), "doubleClick", {}));
  await x.type("group:");
  assert.equal(attr(x.input(), "value"), "Keep this");
  await x.key(x.input(), "Enter");
  assert.deepEqual(x.tags(), [{ namespace: "group", value: "Keep this" }]);
  // A value typed after the colon replaces the old one, as when adding a tag.
  await act(async () => dispatch(one(x.tag(0), "strong"), "doubleClick", {}));
  await x.type("parody:New value");
  await x.key(x.input(), "Enter");
  assert.deepEqual(x.tags(), [{ namespace: "parody", value: "New value" }]);
});

for (const character of ["n", "😀"]) {
  test(`an invalid namespace cannot be committed or saved as other (${character})`, async (context) => {
    const x = await editor(context);
    await x.type(character.repeat(129) + ":value");
    assert.equal(x.input().validity.valid, false);
    assert.equal(x.pending(), null);
    await x.key(x.input(), "Enter");
    assert.deepEqual(x.tags(), []);
    assert.equal(x.input().validity.valid, false);
    assert.equal(x.pending(), null);
    await x.type(character.repeat(128) + ":value");
    assert.equal(x.input().validity.valid, true);
    assert.deepEqual(x.pending()?.tags, [
      { namespace: character.repeat(128), value: "value" },
    ]);
    await x.key(x.input(), "Enter");
    assert.deepEqual(x.tags(), [
      { namespace: character.repeat(128), value: "value" },
    ]);
  });
}

test("Escape discards an invalid namespace and clears its validation error", async (context) => {
  const x = await editor(context);
  await x.type("n".repeat(129) + ":value");
  await x.key(x.input(), "Escape");
  assert.equal(x.input().validity.valid, true);
  assert.equal(x.input().value, "");
  assert.equal(x.pending(), null);
  await x.type("plain");
  await x.key(x.input(), "Enter");
  assert.deepEqual(x.tags(), [{ namespace: "other", value: "plain" }]);
});
