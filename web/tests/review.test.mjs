import { one, all, dispatch, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React, { useState } from "react";
import { render, act } from "@testing-library/react";
import { Detail } from "../src/Detail.tsx";
import { Notifications, toast } from "../src/feedback.tsx";
import { Pages } from "../src/pages.ts";
import {
  button,
  click,
  nodeText,
  setupFixture,
  visibleText,
} from "./harness.mjs";
async function setup(t, prepare = () => {}, options = {}) {
  const searches = [];
  const window = globalThis.window;
  window.matchMedia = () =>
    Object.assign(new EventTarget(), { matches: false });
  const x = await setupFixture(t, prepare, {
    window,
    globals: { document: globalThis.document },
  });
  const fixtureFetch = x.window.fetch;
  t.mock.method(globalThis, "fetch", (path, options) =>
    path.includes("/pages/")
      ? Promise.resolve(new Response(new Blob(["fixture"])))
      : fixtureFetch(path, options),
  );
  const pages = new Pages("fixture-0"),
    dirty = { current: false };
  let current;
  function Harness() {
    const [book, setBook] = useState(structuredClone(x.fixture.books[0]));
    current = book;
    return React.createElement(Detail, {
      book,
      update: setBook,
      queue: {},
      pages,
      status: "saved",
      t: (_, en) => en,
      navigate: () => {},
      search: (query) => searches.push(query),
      suffix: options.suffix,
      read: () => {},
      dirty,
    });
  }
  toast.dismiss();
  const view = await x.render(
    React.createElement(
      React.Fragment,
      null,
      React.createElement(Harness),
      React.createElement(Notifications, { t: (_, en) => en }),
    ),
  );
  t.after(() => pages.dispose());
  const find = (text) => button(view, text);
  const open = async (text) => {
    if (text === "Edit") text = "Edit metadata";
    if (text === "Metadata review") text = "Review metadata";
    if (!find(text)) await click(find("Book actions"));
    if (!find(text) && text !== "Review metadata")
      await click(find("Review metadata"));
    await click(find(text));
  };
  const title = async (value) => {
    await open("Edit");
    await act(async () =>
      dispatch(
        all(one(view.container, { className: "metadata-form" }), "input")[0],
        "change",
        { target: { value } },
      ),
    );
  };
  const save = async () => {
    await act(async () =>
      dispatch(one(view.container, "form"), "submit", { preventDefault() {} }),
    );
  };
  return {
    view,
    fixture: x.fixture,
    button: find,
    click: open,
    title,
    save,
    current: () => current,
    searches,
    requests: () => x.fixture.requests.map((r) => r.path),
    text: () => visibleText(view),
  };
}
const matched = (fixture) => {
  const book = fixture.books[0];
  fixture.reviewProofs[book.id] = JSON.stringify(book.metadata);
};
test("metadata saves automatically request file sync without a permanent sync status", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.syncOutcomes["fixture-0"] = "pending";
  });
  assert.doesNotMatch(x.text(), /Metadata file synced/);
  await x.title("Saved title");
  await x.save();
  assert.ok(
    x.fixture.requests.some(
      (item) => item.method === "POST" && item.path === "/api/sidecar/sync",
    ),
  );
  assert.match(x.text(), /Saved/);
});
test("detail omits raw archive and metadata export controls while retaining an unsaved draft", async (t) => {
  const x = await setup(t);
  await x.title("Unsaved draft");
  assert.ok(
    all(x.view.container, "a").every(
      (node) => !attr(node, "href")?.startsWith("/api/exports/"),
    ),
  );
  assert.notEqual(x.current().metadata.title, "Unsaved draft");
});
test("review opening is read only and missing proof keeps explicit manual acknowledgement", async (t) => {
  const x = await setup(t);
  await x.click("Metadata review");
  assert.equal(x.fixture.reviewChecks.length, 0);
  assert.equal(attr(x.button("Complete review"), "disabled"), true);
  await x.click("Run automatic review");
  assert.equal(x.fixture.reviewChecks.length, 1);
  assert.notEqual(x.current().review_state, "confirmed");
  assert.match(x.text(), /Manual review is needed/);
  await act(async () =>
    dispatch(
      all(x.view.container, "input").find(
        (node) => attr(node, "type") === "checkbox",
      ),
      "change",
      { target: { checked: true } },
    ),
  );
  assert.equal(attr(x.button("Complete review"), "disabled"), false);
  await x.click("Complete review");
  assert.equal(x.current().review_state, "confirmed");
  assert.match(x.text(), /Acknowledged items retained: /);
  assert.match(x.text(), /No source/);
});
test("matching local proof is applied only after request and unknown source remains visible", async (t) => {
  const x = await setup(t, matched);
  await x.click("Metadata review");
  assert.equal(x.fixture.reviewChecks.length, 0);
  assert.match(x.text(), /archive will be checked again/);
  await x.click("Run automatic review");
  assert.equal(x.current().review_state, "confirmed");
  assert.equal(x.button("Review metadata"), undefined);
  assert.match(x.text(), /No source/);
  assert.equal(x.button("Run automatic review"), undefined);
});
test("closing metadata editor discards the draft before review", async (t) => {
  const x = await setup(t, matched);
  const before = x.current().metadata.title;
  await x.title("Unsaved");
  await click(x.button("Close"));
  await click(x.button("Discard changes"));
  await x.click("Metadata review");
  assert.equal(x.current().metadata.title, before);
  assert.equal(x.fixture.reviewChecks.length, 0);
});
test("stale preview is shown as conflict and never as a successful confirmation", async (t) => {
  const x = await setup(t, (fixture) => {
    matched(fixture);
    fixture.failReview = true;
  });
  await x.click("Run automatic review");
  assert.match(x.text(), /Another device changed this item/);
  assert.ok(one(one(x.view.container, "dialog"), { role: "alert" }));
  assert.notEqual(x.current().review_state, "confirmed");
  assert.equal(attr(x.button("Run automatic review"), "disabled"), false);
  assert.doesNotMatch(x.text(), /local confirmation was reused/);
  x.fixture.failReview = false;
  await x.click("Refresh review");
  await x.click("Run automatic review");
  assert.equal(x.current().review_state, "confirmed");
});
test("saving a restored metadata value shows server automatic confirmation", async (t) => {
  const x = await setup(t, matched);
  const original = x.current().metadata.title;
  await x.title("Temporary title");
  await x.save();
  await x.click("Metadata review");
  assert.equal(x.current().review_state, "needs_review");
  assert.match(x.text(), /No local confirmation matches/);
  await x.title(original);
  await x.save();
  assert.equal(x.current().review_state, "confirmed");
  assert.equal(x.button("Review metadata"), undefined);
  assert.equal(x.fixture.reviewChecks.length, 0);
});
test("native import origin is visible and sparse Eze candidates only show supplied fields", async (t) => {
  const x = await setup(t, (fixture) => {
    const book = fixture.books[0];
    book.importSources = ["native", "eze:info", "eze:full"];
    book.reviewCandidates = {
      "eze:info": {
        metadata: { ...book.metadata, title: "Source title" },
        fields: ["title"],
        decision: null,
      },
      "eze:full": { metadata: {}, fields: [], decision: null },
    };
  });
  await x.click("Metadata review");
  assert.match(x.text(), /Native metadata/);
  assert.match(x.text(), /There are unresolved candidates/);
  assert.equal(attr(x.button("Complete review"), "disabled"), true);
  const fields = all(x.view.container, { className: "field-choice" });
  assert.equal(fields.length, 1);
  assert.equal(one(fields[0], "legend").textContent, "Title");
  assert.equal(x.fixture.reviewChecks.length, 0);
  assert.notEqual(x.current().review_state, "confirmed");
});
test("invalid native fallback remains readable and reports failure without exposing rejected data", async (t) => {
  const x = await setup(t, (fixture) => {
    const book = fixture.books[0];
    book.importSources = ["eze:info", "eze:full"];
    book.importWarnings = ["native_unreadable_or_invalid"];
    book.ignoredFields = ["private_fixture_field"];
  });
  await x.click("Metadata review");
  assert.match(
    x.text(),
    /native file failed format, content-binding or stability checks/,
  );
  assert.match(x.text(), /book remains readable/);
  assert.match(x.text(), /1 unsupported external fields were ignored/);
  assert.doesNotMatch(x.text(), /private_fixture_field/);
  assert.ok(x.button("Start reading"));
  assert.notEqual(x.current().review_state, "confirmed");
});
test("Eze read and field warnings remain distinct from native metadata origins", async (t) => {
  const x = await setup(t, (fixture) => {
    const book = fixture.books[0];
    book.importSources = ["native"];
    book.importWarnings = [
      "eze_unreadable_or_invalid",
      "gallery_info.rating: invalid; needs review",
    ];
  });
  await x.click("Metadata review");
  assert.match(x.text(), /Eze file could not be read/);
  assert.match(x.text(), /Some source fields did not pass validation/);
  assert.doesNotMatch(x.text(), /native file failed/);
});
test("closing a dirty metadata editor asks first and can keep the draft", async (t) => {
  const x = await setup(t);
  await x.title("Draft kept");
  const input = () =>
    all(one(x.view.container, { className: "metadata-form" }), "input")[0];
  await click(x.button("Close"));
  assert.match(x.text(), /Discard unsaved changes\?/);
  await click(x.button("Keep editing"));
  assert.doesNotMatch(x.text(), /Discard unsaved changes\?/);
  assert.equal(attr(input(), "value"), "Draft kept");
  await click(x.button("Close"));
  await click(x.button("Discard changes"));
  assert.equal(all(x.view.container, { className: "metadata-form" }).length, 0);
});
test("a metadata conflict applies the newer book at once, keeps the draft and can reload", async (t) => {
  const x = await setup(t);
  await x.title("My draft");
  x.fixture.books[0].metadata.title = "Other tab";
  x.fixture.books[0].metadata_revision++;
  await x.save();
  assert.match(x.text(), /Another device changed this item/);
  assert.equal(x.current().metadata.title, "Other tab");
  const input = () =>
    all(one(x.view.container, { className: "metadata-form" }), "input")[0];
  assert.equal(attr(input(), "value"), "My draft");
  await click(x.button("Reload latest"));
  assert.equal(attr(input(), "value"), "Other tab");
  await act(async () =>
    dispatch(input(), "change", { target: { value: "Final title" } }),
  );
  await x.save();
  assert.equal(x.fixture.books[0].metadata.title, "Final title");
  assert.doesNotMatch(x.text(), /Another device changed/);
});
test("a committed metadata save stays saved when the file sync fails", async (t) => {
  const x = await setup(t);
  await x.title("Committed");
  x.fixture.syncFailure = 500;
  await x.save();
  assert.equal(x.fixture.books[0].metadata.title, "Committed");
  assert.match(x.text(), /Saved; metadata file sync failed/);
  assert.equal(all(x.view.container, { role: "alert" }).length, 0);
});
test("review preview and conflicts load only when the review dialog opens", async (t) => {
  const x = await setup(t);
  const reviewReads = () =>
    x
      .requests()
      .filter(
        (path) => path.endsWith("/review") || path === "/api/sidecar/conflicts",
      ).length;
  assert.equal(reviewReads(), 0);
  await x.title("Saved title");
  await x.save();
  assert.equal(reviewReads(), 0);
  await x.click("Metadata review");
  assert.equal(reviewReads(), 2);
  assert.match(x.text(), /Run automatic review/);
});
test("page links keep the collection route and versioned thumbnails; tag pills search", async (t) => {
  const x = await setup(
    t,
    (fixture) => {
      fixture.books[0].metadata.tags = [
        { namespace: "artist", value: "Alice" },
      ];
    },
    { suffix: "/in/series" },
  );
  const links = all(x.view.container, "a").filter((node) =>
    attr(node, "href")?.includes("/page/"),
  );
  assert.equal(attr(links[1], "href"), "#read/fixture-0/in/series/page/1");
  assert.match(
    attr(one(links[1], "img"), "src"),
    /\/thumbnails\/1\?v=0000000000000000$/,
  );
  await click(x.button("Alice"));
  assert.deepEqual(x.searches, ['artist:"Alice"']);
});
test("the tag editor works on tags directly and an unchanged tag list is not a change", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.books[0].metadata.tags = [
      { namespace: "artist", value: "Alice" },
      { namespace: "parody", value: "a:b" },
    ];
  });
  await x.click("Edit");
  assert.equal(attr(x.button("Save metadata"), "disabled"), true);
  await click(x.button("Remove Alice"));
  assert.equal(attr(x.button("Save metadata"), "disabled"), false);
  await x.save();
  assert.equal(
    JSON.stringify(x.fixture.books[0].metadata.tags),
    JSON.stringify([{ namespace: "parody", value: "a:b" }]),
  );
});
test("a two-way sidecar conflict is resolved per field as library or file", async (t) => {
  const x = await setup(t);
  const book = x.current();
  const side = (title) => ({
    schema: 1,
    content_sha256: "0".repeat(64),
    revision: 1,
    metadata: { ...book.metadata, title },
  });
  const resolved = [];
  const original = globalThis.fetch;
  t.mock.method(globalThis, "fetch", async (path, options = {}) => {
    if (path.startsWith("/api/sidecar/conflicts?"))
      return new Response(
        JSON.stringify({
          conflicts: [
            {
              id: "c1",
              book_id: book.id,
              fields: ["title"],
              token: "tok",
              library: side("Library title"),
              file: side("File title"),
            },
          ],
        }),
        { headers: { "Content-Type": "application/json" } },
      );
    if (path === "/api/sidecar/conflicts/c1/resolve") {
      resolved.push(JSON.parse(options.body));
      return new Response(JSON.stringify(book), {
        headers: { "Content-Type": "application/json" },
      });
    }
    return original(path, options);
  });
  await x.click("Metadata review");
  assert.match(x.text(), /Library title/);
  assert.match(x.text(), /File title/);
  const take = all(x.view.container, "label").find((node) =>
    nodeText(node).includes("Take file"),
  );
  await act(async () =>
    dispatch(one(take, "input"), "change", { target: { checked: true } }),
  );
  await click(x.button("Save conflict choices"));
  assert.deepEqual(resolved, [{ fields: { title: "file" } }]);
});
