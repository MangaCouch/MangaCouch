import { one, all, dispatch, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React, { useState } from "react";
import { act } from "@testing-library/react";
import { Detail } from "../src/Detail.tsx";
import { Notifications, toast } from "../src/feedback.tsx";
import { Pages } from "../src/pages.ts";
import { reviewIssue } from "../src/ReviewDetails.tsx";
import { click, english, nodeText, renderPage } from "./harness.mjs";
/** The detail page of `initial`, holding the book as the app does between saves. */
function BookDetail({ initial, current, ...props }) {
  const [book, setBook] = useState(() => structuredClone(initial));
  current.value = book;
  return React.createElement(Detail, { ...props, book, update: setBook });
}
async function setup(t, prepare = () => {}, options = {}) {
  const searches = [],
    current = {};
  window.matchMedia = () =>
    Object.assign(new EventTarget(), { matches: false });
  const pages = new Pages("fixture-0");
  toast.dismiss();
  const props = {
    current,
    queue: options.queue ?? {},
    pages,
    status: "saved",
    search: (query) => searches.push(query),
    suffix: options.suffix,
    read: () => {},
    dirty: { current: false },
  };
  const x = await renderPage(t, BookDetail, {
    prepare: async (fixture) => {
      await prepare(fixture);
      props.initial = fixture.books[0];
    },
    globals: { document: globalThis.document },
    props,
    wrap: (page) =>
      React.createElement(
        React.Fragment,
        null,
        page,
        React.createElement(Notifications, { t: english }),
      ),
  });
  t.after(() => pages.dispose());
  const open = async (text) => {
    if (text === "Edit") text = "Edit metadata";
    if (text === "Metadata review") text = "Review metadata";
    if (!x.button(text)) await x.click("Book actions");
    if (!x.button(text) && text !== "Review metadata")
      await x.click("Review metadata");
    await x.click(text);
  };
  const title = async (value) => {
    await open("Edit");
    await act(async () =>
      dispatch(
        all(one(x.view.container, { className: "metadata-form" }), "input")[0],
        "change",
        { target: { value } },
      ),
    );
  };
  const save = async () => {
    await act(async () =>
      dispatch(one(x.view.container, "form"), "submit", {
        preventDefault() {},
      }),
    );
  };
  return {
    ...x,
    click: open,
    title,
    save,
    current: () => current.value,
    searches,
    requests: () => x.fixture.requests.map((r) => r.path),
  };
}
const matched = (fixture) => {
  const book = fixture.books[0];
  fixture.reviewProofs[book.id] = JSON.stringify(book.metadata);
};
test("a metadata save reports its file sync from the save itself, without a second request", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.syncOutcomes["fixture-0"] = "pending";
  });
  assert.doesNotMatch(x.text(), /Metadata file synced/);
  await x.title("Saved title");
  await x.save();
  assert.ok(
    !x.fixture.requests.some(
      (item) => item.method === "POST" && item.path === "/api/sidecar/sync",
    ),
  );
  assert.match(x.text(), /Saved; file sync pending/);
  assert.equal(all(x.view.container, { role: "alert" }).length, 0);
});
test("a save whose metadata file differs on disk says it needs attention", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.syncOutcomes["fixture-0"] = "conflict";
  });
  await x.title("Saved title");
  await x.save();
  assert.match(x.text(), /Saved; metadata file sync needs attention/);
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
  assert.match(x.text(), /Saved; file sync pending/);
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
  assert.equal(attr(links[1], "href"), "#read/fixture-0/in/series/page/2");
  assert.match(
    attr(one(links[1], "img"), "src"),
    /\/thumbnails\/1\?v=0000000000000000\.2\.0$/,
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
          items: [
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
test("review mismatch reports both exact byte values and the image comparison basis", () => {
  const text = reviewIssue(
    "declared_byte_size_mismatch",
    {
      metadata: { declared_byte_size: 1048577 },
      compared_byte_size: 1048576,
      actual_byte_size: 900000,
      byte_size_basis: "images",
    },
    english,
  );
  assert.match(text, /1,048,577 B/);
  assert.match(text, /1,048,576 B/);
  assert.match(text, /Uncompressed image total/);
  assert.doesNotMatch(text, /900,000/);
});
test("the book menu reviews metadata interactively and refreshes thumbnails", async (t) => {
  const x = await setup(t, matched);
  await x.click("Book actions");
  assert.ok(x.button("Review metadata"));
  assert.ok(x.button("Refresh thumbnails"));
  const revision = x.current().thumbnail_revision;
  await x.click("Refresh thumbnails");
  for (let i = 0; i < 10 && x.current().thumbnail_revision === revision; i++)
    await act(async () => {});
  // The book is read again, so its pages show the new thumbnails.
  assert.equal(x.current().thumbnail_revision, revision + 1);
  assert.equal(
    x.fixture.requests.filter((r) => r.path === "/api/thumbnails/refresh")
      .length,
    1,
  );
});
test("marking a finished book unread starts it over, as in the library", async (t) => {
  const edits = [];
  const x = await setup(
    t,
    (fixture) => {
      fixture.books[0].personal = {
        ...fixture.books[0].personal,
        completed: true,
        resume_page: 3,
      };
    },
    {
      queue: {
        update: (edit) =>
          edits.push(
            edit({
              completed: true,
              resume_page: 3,
              rating: null,
            }),
          ),
      },
    },
  );
  await x.click("Book actions");
  await x.click("Mark as unread");
  assert.deepEqual(edits, [
    { completed: false, resume_page: null, rating: null },
  ]);
});

/** Holds the metadata PUT until `release()`; `answer` may replace the backend's reply. */
function holdMetadataSave(t, answer) {
  const backend = globalThis.fetch;
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  t.mock.method(globalThis, "fetch", async (input, init = {}) => {
    const path = String(input);
    if (init.method === "PUT" && path.includes("/metadata?")) {
      await held;
      if (answer) return answer();
    }
    return backend(input, init);
  });
  return () => act(async () => release());
}
const titleField = (x) =>
  all(one(x.view.container, { className: "metadata-form" }), "input")[0];
const typeTitle = (x, value) =>
  act(async () => dispatch(titleField(x), "change", { target: { value } }));
const tagField = (x) =>
  one(x.view.container, { className: "tag-editor" }).querySelector("input");
test("a tag typed without Enter counts as a change and is saved with the form", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.books[0].metadata.tags = [{ namespace: "artist", value: "Alice" }];
  });
  await x.click("Edit");
  assert.equal(attr(x.button("Save metadata"), "disabled"), true);
  await act(async () =>
    dispatch(tagField(x), "change", { target: { value: "group:Circle" } }),
  );
  assert.equal(attr(x.button("Save metadata"), "disabled"), false);
  await x.save();
  assert.deepEqual(
    JSON.parse(JSON.stringify(x.fixture.books[0].metadata.tags)),
    [
      { namespace: "artist", value: "Alice" },
      { namespace: "group", value: "Circle" },
    ],
  );
  // A clean save closes the editor; the detail page shows the saved tag.
  assert.equal(x.button("Save metadata"), undefined);
  assert.match(x.text(), /Circle/);
  assert.match(x.text(), /Saved\./);
});
test("a pending tag draft keeps a close from discarding silently", async (t) => {
  const x = await setup(t);
  await x.click("Edit");
  await act(async () =>
    dispatch(tagField(x), "change", { target: { value: "unfinished" } }),
  );
  await click(x.button("Close"));
  assert.match(x.text(), /Discard unsaved changes\?/);
});
test("typing while a save is slow keeps the new input after it succeeds", async (t) => {
  const x = await setup(t);
  await x.title("First");
  const release = holdMetadataSave(t);
  await x.save();
  await typeTitle(x, "First and more");
  await release();
  assert.equal(x.fixture.books[0].metadata.title, "First");
  assert.equal(attr(titleField(x), "value"), "First and more");
  assert.equal(attr(x.button("Save metadata"), "disabled"), false);
  assert.match(x.text(), /Changes made while saving are not saved yet/);
  await x.save();
  assert.equal(x.fixture.books[0].metadata.title, "First and more");
});
test("typing while a save fails keeps the new input", async (t) => {
  const x = await setup(t);
  await x.title("First");
  const release = holdMetadataSave(t, () =>
    Response.json({ error: "storage_unavailable" }, { status: 503 }),
  );
  await x.save();
  await typeTitle(x, "Second");
  await release();
  assert.match(x.text(), /Library storage is unavailable/);
  assert.equal(attr(titleField(x), "value"), "Second");
  assert.notEqual(x.fixture.books[0].metadata.title, "Second");
});
test("typing while a save meets a conflict keeps the new input", async (t) => {
  const x = await setup(t);
  await x.title("First");
  const release = holdMetadataSave(t);
  await x.save();
  x.fixture.books[0].metadata.title = "Other tab";
  x.fixture.books[0].metadata_revision++;
  await typeTitle(x, "Second");
  await release();
  assert.match(x.text(), /Another device changed this item/);
  assert.equal(attr(titleField(x), "value"), "Second");
  await x.save();
  assert.equal(x.fixture.books[0].metadata.title, "Second");
});
test("typing while a saved edit's file sync fails keeps the new input", async (t) => {
  const x = await setup(t);
  await x.title("First");
  x.fixture.syncFailure = 500;
  const release = holdMetadataSave(t);
  await x.save();
  await typeTitle(x, "Second");
  await release();
  assert.equal(x.fixture.books[0].metadata.title, "First");
  assert.match(x.text(), /Saved; file sync pending/);
  assert.equal(attr(titleField(x), "value"), "Second");
  assert.equal(attr(x.button("Save metadata"), "disabled"), false);
});
test("a book confirmed by the online rule says so instead of claiming the owner's review", async (t) => {
  const x = await setup(t, (fixture) => {
    Object.assign(fixture.books[0], {
      review_state: "confirmed",
      confirmation: {
        action: "auto_confirm",
        rule: "online-source/1",
        acknowledged_issues: [],
        evidence_event_id: null,
      },
    });
  });
  await x.click("Metadata review");
  assert.match(x.text(), /Confirmed automatically from the online source/);
  assert.doesNotMatch(x.text(), /Your review choices are recorded/);
});

test("metadata conflict preserves my title and merges untouched language and tags", async (t) => {
  const x = await setup(t);
  await x.title("My title");
  Object.assign(x.fixture.books[0].metadata, {
    title: "Their title",
    language: "Japanese",
    tags: [{ namespace: "artist", value: "New" }],
  });
  x.fixture.books[0].metadata_revision++;
  await x.save();
  await x.save();
  assert.equal(x.fixture.books[0].metadata.title, "My title");
  assert.equal(x.fixture.books[0].metadata.language, "Japanese");
  assert.deepEqual(structuredClone(x.fixture.books[0].metadata.tags), [
    { namespace: "artist", value: "New" },
  ]);
});

test("review waits for a fresh event token before enabling its next action", async (t) => {
  const x = await setup(t);
  await x.click("Metadata review");
  const fetch = globalThis.fetch;
  let release;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    if (String(path).endsWith("/review"))
      await new Promise((resolve) => {
        release = resolve;
      });
    return fetch(path, options);
  });
  await x.click("Run automatic review");
  assert.equal(attr(x.button("Run automatic review"), "disabled"), true);
  assert.equal(x.fixture.reviewChecks.length, 1);
  await act(async () => release());
  assert.equal(attr(x.button("Run automatic review"), "disabled"), false);
  t.mock.restoreAll();
  await x.click("Run automatic review");
  assert.equal(x.fixture.reviewChecks.length, 2);
  assert.doesNotMatch(x.text(), /Another device changed/);
});

test("online auto confirmation is not described as a reused local confirmation", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.books[0].review_state = "confirmed";
    fixture.books[0].confirmation = {
      action: "auto_confirm",
      rule: "eh-metadata/1",
      acknowledged_issues: [],
    };
  });
  await x.click("Metadata review");
  assert.doesNotMatch(x.text(), /local confirmation was reused/i);
});
