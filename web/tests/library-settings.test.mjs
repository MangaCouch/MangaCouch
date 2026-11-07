import { all, attr, dispatch } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import {
  DuplicateSettings,
  TranslationSettings,
} from "../src/LibrarySettings.tsx";
import { RecycleBin } from "../src/RemovalDialog.tsx";
import { button, click, setupFixture, visibleText } from "./harness.mjs";
async function setup(t, Component, prepare = () => {}) {
  const x = await setupFixture(t, prepare);
  const view = await x.render(
    React.createElement(Component, { t: (_, en) => en, navigate: () => {} }),
  );
  return {
    ...x,
    view,
    text: () => visibleText(view),
    button: (text) => button(view, text),
    click: (text) => click(button(view, text)),
  };
}
test("duplicate resolution applies only when exactly one copy is unchecked", async (t) => {
  const x = await setup(t, DuplicateSettings, (f) => {
    f.duplicatePairs = [["fixture-0", "fixture-1"]];
  });
  const keep = () =>
    all(x.view.container, "input").filter(
      (node) => attr(node, "type") === "checkbox",
    );
  // Both kept: nothing to apply; "Ignore" is the explicit way to keep both.
  assert.equal(attr(x.button("Apply selection"), "disabled"), true);
  await act(async () =>
    dispatch(keep()[0], "change", { target: { checked: false } }),
  );
  assert.equal(attr(x.button("Apply selection"), "disabled"), false);
  await act(async () =>
    dispatch(keep()[1], "change", { target: { checked: false } }),
  );
  assert.equal(attr(x.button("Apply selection"), "disabled"), true);
  assert.match(x.text(), /No copy is marked Keep/);
  await x.click("Ignore");
  assert.equal(
    JSON.stringify(x.fixture.ignoredPairs),
    '[["fixture-0","fixture-1"]]',
  );
  assert.match(x.text(), /No duplicate books found/);
});
test("saving one translation keeps the drafts of other rows and updates only that row", async (t) => {
  const x = await setup(t, TranslationSettings, (f) => {
    f.aliases = [
      { namespace: "artist", value: "a", translation: "甲" },
      { namespace: "artist", value: "b", translation: "乙" },
    ];
  });
  const field = (key) =>
    all(x.view.container, "input").find(
      (node) => attr(node, "aria-label") === `${key} translation`,
    );
  await act(async () =>
    dispatch(field("artist:a"), "change", { target: { value: "甲二" } }),
  );
  await act(async () =>
    dispatch(field("artist:b"), "change", { target: { value: "乙二" } }),
  );
  const reads = () =>
    x.fixture.requests.filter((r) => r.path === "/api/tag-aliases").length;
  const before = reads();
  await act(async () => dispatch(field("artist:a"), "blur"));
  assert.equal(x.fixture.aliasSaves.length, 1);
  assert.equal(attr(field("artist:a"), "value"), "甲二");
  assert.equal(attr(field("artist:b"), "value"), "乙二");
  // Only the PUT; the list is not downloaded again.
  assert.equal(reads(), before + 1);
});
test("deleting a row right after editing it is not swallowed by the blur save", async (t) => {
  const x = await setup(t, TranslationSettings, (f) => {
    f.aliases = [{ namespace: "artist", value: "a", translation: "甲" }];
  });
  const remove = x.button("Delete artist:a");
  let prevented = false;
  dispatch(remove, "mouseDown", { preventDefault: () => (prevented = true) });
  assert.equal(prevented, true);
  await x.click("Delete artist:a");
  assert.deepEqual(x.fixture.aliasSaves.at(-1).translation, "");
  assert.equal(x.fixture.aliases.length, 0);
  assert.doesNotMatch(x.text(), /artist:a/);
});
test("the recycle bin pages back to earlier records with the before cursor", async (t) => {
  const x = await setup(t, RecycleBin, (f) => {
    for (let i = 1; i <= 55; i++)
      f.removals.push({
        id: `removal-${i}`,
        kind: "single",
        book_id: `gone-${i}`,
        book_ids: [`gone-${i}`],
        title: `Removed ${i}`,
        created_at: 1700000000 + i,
        delete_pending: false,
        restored: false,
        can_restore: true,
        cursor: i,
      });
  });
  const rows = () => all(x.view.container, { className: "recycle-row" }).length;
  assert.match(x.text(), /Removed 55/);
  assert.equal(rows(), 50);
  await x.click("Earlier records");
  assert.equal(rows(), 55);
  assert.ok(
    x.fixture.requests.some((r) => r.path === "/api/removals") &&
      /Removed 1/.test(x.text()),
  );
  assert.equal(x.button("Earlier records"), undefined);
});
test("a removal that deleted files lists each book's file result", async (t) => {
  const x = await setup(t, RecycleBin, (f) => {
    f.removals.push({
      id: "batch",
      kind: "selected",
      book_ids: ["x", "y"],
      title: null,
      created_at: 1700000000,
      delete_requested: true,
      delete_pending: false,
      deletion_stopped: "file_changed",
      restored: false,
      can_restore: true,
      cursor: 1,
      items: [
        {
          book_id: "x",
          title: "First",
          filename: "x.cbz",
          file: "deleted",
          restored: false,
        },
        {
          book_id: "y",
          title: "Second",
          filename: "y.cbz",
          file: "kept",
          restored: false,
        },
      ],
    });
  });
  assert.match(x.text(), /First: File deleted/);
  assert.match(x.text(), /Second: File kept/);
  assert.match(x.text(), /File deletion stopped: the file changed/);
});
