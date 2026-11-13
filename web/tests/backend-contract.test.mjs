import test from "node:test";
import assert from "node:assert/strict";
import { setupFixture } from "./harness.mjs";

const send = (path, body, method = "POST") =>
  fetch(path, { method, body: JSON.stringify(body) });
test("fake backend rejects unknown paths and stale dynamic rules and evaluates membership", async (t) => {
  const { fixture } = await setupFixture(t);
  fixture.books[0].metadata.tags = [{ namespace: "artist", value: "Blue Sky" }];
  fixture.books[1].metadata.tags = [
    { namespace: "artist", value: "Blue Skies" },
  ];
  const created = await (
    await send("/api/collections", {
      name: "Exact",
      rule: { query: 'artist:"Blue Sky"', options: {} },
    })
  ).json();
  assert.equal(created.count, 1);
  assert.equal(created.items[0].book_id, fixture.books[0].id);
  const stale = await send(
    `/api/collections/${created.id}/rule?expected_revision=9`,
    { rule: { query: "", options: {} } },
    "PUT",
  );
  assert.equal(stale.status, 409);
  assert.equal((await stale.json()).error, "revision_conflict");
  assert.equal(
    (await fetch(`/api/books/${fixture.books[0].id}/unknown`)).status,
    404,
  );
  assert.equal(
    (await fetch(`/api/collections/${created.id}/unknown`)).status,
    404,
  );
});

test("fake scan filters failed rows and loads previous-book links only on demand", async (t) => {
  const { fixture } = await setupFixture(t);
  fixture.scan = {
    id: "scan",
    state: "done",
    entries: [
      {
        id: 1,
        kind: "archive",
        state: "indexed",
        previous_books: ["fixture-0"],
      },
      { id: 2, kind: "archive", state: "error", previous_books: ["fixture-1"] },
    ],
  };
  const page = await (await fetch("/api/scans/scan?state=error")).json();
  assert.deepEqual(
    page.items.map((item) => item.id),
    [2],
  );
  assert.equal(page.items[0].previous_books, undefined);
  const item = await (await fetch("/api/scans/scan/items/2")).json();
  assert.deepEqual(item.previous_books, ["fixture-1"]);
  assert.equal((await fetch("/api/scans/scan/not-a-route")).status, 404);
});

test("fake actions enforce production source and review token contracts", async (t) => {
  const { fixture } = await setupFixture(t);
  const id = fixture.books[0].id;
  assert.equal(
    (
      await send("/api/downloads", {
        url: "https://e-hentai.org/g/1/abc/",
        entry: "legacy",
      })
    ).status,
    422,
  );
  assert.equal(
    (await (await fetch("/api/downloads/missing")).json()).error,
    "download_not_found",
  );
  fixture.books[0].personal.resume_page = 3;
  const rev = fixture.books[0].personal_revision;
  await send("/api/bulk-selection", {
    id: "unread",
    book_ids: [id],
    action: { kind: "completed", completed: false },
  });
  assert.equal(fixture.books[0].personal.resume_page, null);
  const result = await (
    await send("/api/bulk-selection", {
      id: "source",
      book_ids: [id],
      action: { kind: "source" },
    })
  ).json();
  assert.equal(result.counts.error, 1);
  assert.equal(fixture.books[0].personal_revision, rev + 1);
  const preview = await (await fetch(`/api/books/${id}/review`)).json();
  assert.equal(
    (await send(`/api/books/${id}/review/automatic`, { token: preview.token }))
      .status,
    200,
  );
  assert.equal(
    (await send(`/api/books/${id}/review/automatic`, { token: preview.token }))
      .status,
    409,
  );
  const context = await (
    await fetch("/api/collections/fixture-series/context")
  ).json();
  assert.ok(
    ["unread", "reading", "completed"].includes(context.continue_state),
  );
  const dynamic = await (
    await send("/api/collections", {
      name: "Dynamic",
      rule: { query: "", options: {} },
    })
  ).json();
  const frozen = await (
    await send(
      `/api/collections/${dynamic.id}/freeze?expected_revision=${dynamic.revision}`,
      {},
    )
  ).json();
  assert.equal(frozen.kind, "static");
  assert.equal(frozen.count, fixture.books.length);
});

test("fake personal saves rebase snapshots like the server", async (t) => {
  const { fixture } = await setupFixture(t);
  const book = fixture.books[0];
  const path = `/api/books/${book.id}/personal?expected_revision=`;
  const base = { resume_page: 10, completed: false, rating: null };
  book.personal = { ...base };
  book.personal_revision = 2;
  // Another device read to the end; a page-unload snapshot based on revision 2 arrives late.
  book.personal = { resume_page: 29, completed: true, rating: null };
  book.personal_revision = 3;
  const snapshot = {
    state: { resume_page: 11, completed: true, rating: null },
    base_state: base,
  };
  const rebased = await send(path + 2, snapshot, "PUT");
  assert.equal(rebased.status, 200);
  assert.deepEqual((await rebased.json()).personal, {
    resume_page: 29,
    completed: true,
    rating: null,
  });
  const future = await send(path + 99, snapshot, "PUT");
  assert.equal(future.status, 409);
  assert.equal((await future.json()).error, "revision_conflict");
});
