import { all, attr, dispatch } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import { act } from "@testing-library/react";
import {
  DuplicateSettings,
  SourceReviewList,
  TranslationSettings,
} from "../src/LibrarySettings.tsx";
import { RecycleBin } from "../src/RemovalDialog.tsx";
import { renderPage } from "./harness.mjs";
const setup = (t, Component, prepare) => renderPage(t, Component, { prepare });
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
test("source review lists failed source checks with a retry", async (t) => {
  const x = await setup(t, SourceReviewList, (f) => {
    const check = (state, error_code) => ({
      state,
      error_code,
      generation: 3,
      reasons: error_code ? [error_code] : [],
      retry_at: 0,
      updated_at: 1,
    });
    f.books[0].metadata.source = {
      site: "ehentai",
      gallery_id: 1,
      gallery_token: "abc",
    };
    f.books[0].source_check = check("needs_review", "gallery_unavailable");
    f.books[1].source_check = check("queued", null);
    f.books[2].source_check = check("done", null);
  });
  assert.match(x.text(), /Unfinished source checks/);
  assert.match(x.text(), /Source check failed: The gallery has been removed/);
  assert.doesNotMatch(x.text(), /Source check paused/);
  assert.equal(x.text().match(/Source check/g).length, 1);
  await act(async () =>
    dispatch(
      all(x.view.container, "button").find(
        (node) => node.textContent === "Retry",
      ),
      "click",
    ),
  );
  assert.equal(x.fixture.books[0].source_check.state, "queued");
  assert.equal(x.fixture.books[0].source_check.generation, 4);
  assert.match(x.text(), /Queued again; the check runs in the background/);
});

test("finishing an alias save leaves the next alias being typed intact", async (t) => {
  const x = await setup(t, TranslationSettings);
  const field = (label) => x.view.getByLabelText(label);
  const type = (label, value) =>
    act(async () => dispatch(field(label), "change", { target: { value } }));
  await type("New tag", "artist:first");
  await type("Translation", "第一");
  const fetch = globalThis.fetch;
  let release;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    if (options?.method === "PUT")
      await new Promise((resolve) => (release = resolve));
    return fetch(path, options);
  });
  await act(async () =>
    dispatch(field("New tag").closest("form"), "submit", {}),
  );
  await type("New tag", "artist:second");
  await type("Translation", "第二");
  await act(async () => release());
  assert.equal(attr(field("New tag"), "value"), "artist:second");
  assert.equal(attr(field("Translation"), "value"), "第二");
  assert.equal(x.fixture.aliasSaves.length, 1);
});
