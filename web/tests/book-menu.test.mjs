import { one, dispatch, all } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { initialBrowse } from "../src/LibraryFilters.tsx";
import { BULK_POLL_MS, useBookActions } from "../src/bookActions.tsx";
import { toast } from "../src/feedback.tsx";
import { button, click, english as t, tick, visibleText } from "./harness.mjs";
const json = (value, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
const settle = () =>
  act(async () => {
    for (let i = 0; i < 30; i++)
      await new Promise((resolve) => setImmediate(resolve));
  });
const exact = (view, text) =>
  all(view.container, "button").find((node) => node.textContent === text);
const latestToast = () => toast.getHistory().at(-1)?.title;
const book = (id, completed = false) => ({
  id,
  metadata: { title: id, source: null },
  content: { original_filename: `${id}.cbz` },
  personal: { completed },
  metadata_revision: 1,
  personal_revision: 1,
});
/** Menu targets for chosen books, the first of which was clicked. */
const chosen = (books, unfinished = true) => ({
  bookIds: books.map((book) => book.id),
  anchor: books[0],
  unfinished,
});
const collection = (index, kind = "static") => ({
  id: `c${index}`,
  name: `Collection ${String(index).padStart(3, "0")}`,
  kind,
  revision: 4,
  count: 0,
});

/**
 * The book menu for `targets` as buttons plus its dialogs, over a fetch answered by
 * `reply(method, url, init)`; a request it leaves unanswered gets an empty list.
 */
async function bookMenu(context, reply, targets) {
  const calls = [];
  context.mock.method(globalThis, "fetch", async (path, init = {}) => {
    const method = init.method ?? "GET";
    calls.push({ method, path, body: init.body && JSON.parse(init.body) });
    return (
      (await reply(method, new URL(path, "http://test"), init)) ??
      json({ items: [], after: null })
    );
  });
  const routes = [];
  let changed = 0,
    view;
  function Harness() {
    const { menu, dialogs } = useBookActions({
      t,
      navigate: (route) => routes.push(route),
      changed: () => changed++,
    });
    const entries = menu(targets)
      .flatMap((entry) =>
        "items" in entry ? entry.items : "separator" in entry ? [] : [entry],
      )
      .map((entry, index) =>
        React.createElement(
          "button",
          { key: index, type: "button", onClick: entry.action },
          entry.label,
        ),
      );
    return React.createElement(React.Fragment, null, entries, dialogs);
  }
  await act(async () => {
    view = render(React.createElement(Harness));
  });
  context.after(async () => {
    await act(async () => view.unmount());
  });
  return {
    view,
    calls,
    routes,
    changed: () => changed,
    labels: () => all(view.container, "button").map((node) => node.textContent),
  };
}
const nothing = () => undefined;

test("menu actions submit complete selections and report server progress in a toast", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let polls = 0;
  const books = [book("a"), book("b")];
  const x = await bookMenu(
    context,
    (method, url) => {
      if (method === "POST" && url.pathname === "/api/bulk-selection")
        return json({ id: "job", state: "running", counts: { pending: 2 } });
      if (url.pathname === "/api/bulk/job")
        return json(
          ++polls < 2
            ? { id: "job", state: "running", counts: { pending: 1, done: 1 } }
            : { id: "job", state: "done", counts: { pending: 0, done: 2 } },
        );
    },
    chosen(books),
  );
  // Two books: no "edit metadata"; one unfinished: "mark as read".
  assert.ok(!x.labels().includes("Edit metadata"));
  assert.ok(x.labels().includes("Mark as read"));
  assert.ok(!x.labels().includes("Mark as unread"));
  await click(button(x.view, "Mark as read"));
  const job = x.calls.find((call) => call.path === "/api/bulk-selection").body;
  assert.deepEqual(job.book_ids, ["a", "b"]);
  assert.ok(!x.calls.some((call) => call.path === "/api/books/lookup"));
  assert.match(latestToast(), /0 \/ 2/);
  await tick(context, BULK_POLL_MS);
  assert.match(latestToast(), /1 \/ 2/);
  await tick(context, BULK_POLL_MS);
  assert.equal(latestToast(), "Marked as read");
  assert.equal(x.changed(), 1);
});
test("leaving the page stops following a bulk job, which keeps running on the server", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let polls = 0;
  const books = Array.from({ length: 1001 }, (_, i) => book(`b${i}`));
  const x = await bookMenu(
    context,
    (method, url) => {
      if (method === "POST" && url.pathname === "/api/bulk-selection")
        return json({
          id: "job",
          state: "running",
          counts: { pending: books.length },
        });
      if (url.pathname === "/api/bulk/job") {
        polls++;
        return json({
          id: "job",
          state: "running",
          counts: { pending: books.length },
        });
      }
    },
    chosen(books),
  );
  await click(button(x.view, "Mark as read"));
  await tick(context, BULK_POLL_MS);
  assert.equal(polls, 1);
  const submissions = x.calls.filter(
    (call) => call.path === "/api/bulk-selection",
  );
  assert.equal(submissions.length, 1);
  assert.deepEqual(
    submissions[0].body.book_ids,
    books.map((item) => item.id),
  );
  await act(async () => x.view.unmount());
  await tick(context, BULK_POLL_MS * 4);
  assert.equal(polls, 1);
  assert.match(latestToast(), /continues on the server/);
});
test("a queued metadata review links to the source review settings", async (context) => {
  const x = await bookMenu(
    context,
    (method, url) => {
      if (method === "POST" && url.pathname === "/api/bulk-selection")
        return json({ id: "job", state: "done", counts: { done: 1 } });
    },
    chosen([book("a")]),
  );
  await click(button(x.view, "Check metadata online"));
  const source = x.calls.find((call) => call.path === "/api/bulk-selection");
  assert.deepEqual(source.body.action, { kind: "source" });
  const notice = render(latestToast());
  context.after(() => notice.unmount());
  const link = one(notice.container, "a");
  assert.equal(link.getAttribute("href"), "#settings/source-checks");
  assert.equal(link.textContent, "Settings → Source review");
  assert.doesNotMatch(notice.container.textContent, /paus/i);
});
test("copying links works without the async clipboard of secure origins", async (context) => {
  const x = await bookMenu(context, nothing, chosen([book("a")]));
  const copied = [];
  document.execCommand = (command) => {
    copied.push([command, document.querySelector("textarea")?.value]);
    return true;
  };
  context.after(() => delete document.execCommand);
  assert.equal(navigator.clipboard, undefined);
  await click(button(x.view, "Copy link"));
  assert.equal(copied.length, 1);
  assert.equal(copied[0][0], "copy");
  assert.match(copied[0][1], /#book\/a$/);
  assert.equal(latestToast(), "Link copied");
  document.execCommand = () => false;
  await click(button(x.view, "Copy link"));
  assert.match(latestToast(), /^Copy failed/);
});
test("one book's menu offers editing and opens its reader", async (context) => {
  const x = await bookMenu(context, nothing, chosen([book("a", true)], false));
  assert.deepEqual(
    x.labels().filter((label) => !/★|rating/i.test(label)),
    [
      "Read",
      "Copy link",
      "Mark as unread",
      "Add to collection…",
      "Edit metadata",
      "Refresh thumbnails",
      "Check metadata online",
      "Remove…",
    ],
  );
  await click(button(x.view, "Read"));
  await click(button(x.view, "Edit metadata"));
  assert.deepEqual(x.routes, ["read/a", "book/a"]);
});
test("removing every result asks the server for the ids, then opens the removal dialog", async (context) => {
  const x = await bookMenu(
    context,
    (method, url) => {
      if (method !== "POST") return;
      if (url.pathname === "/api/removals/search/selection")
        return json({ items: ["a", "b"] });
      if (url.pathname === "/api/removals/selected/preview")
        return json({
          id: "r",
          book_id: "a",
          token: "t",
          title: "a",
          blocked: [],
          book_ids: ["a", "b"],
          items: [],
        });
    },
    {
      books: [],
      all: {
        total: 2,
        exclude: [],
        browse: { ...initialBrowse(), query: "x" },
      },
      anchor: book("a"),
      unfinished: true,
    },
  );
  await click(button(x.view, "Remove 2…"));
  assert.ok(
    x.calls.some(
      (call) =>
        call.method === "POST" &&
        call.path === "/api/removals/search/selection",
    ),
  );
  assert.ok(one(x.view.container, "dialog"));
});
/** Collections served 200 per page in name order, as the server pages them. */
function pages(collections) {
  return (url) => {
    const after = url.searchParams.get("after") ?? "";
    const limit = Number(url.searchParams.get("limit"));
    const start = after ? collections.findIndex((c) => c.id === after) + 1 : 0;
    const page = collections.slice(start, start + limit);
    return json({
      items: page,
      after: start + limit < collections.length ? page.at(-1).id : null,
    });
  };
}
test("the collection picker reads every page and finds a static target past the first 200", async (context) => {
  // 200 dynamic collections first, then a mix: the only static targets are on later pages.
  const collections = [
    ...Array.from({ length: 200 }, (_, i) => collection(i, "dynamic")),
    ...Array.from({ length: 150 }, (_, i) =>
      collection(200 + i, i % 2 ? "static" : "dynamic"),
    ),
    collection(350),
  ];
  const listed = pages(collections);
  const x = await bookMenu(
    context,
    (method, url) => {
      if (url.pathname === "/api/collections") return listed(url);
      if (method === "POST" && url.pathname === "/api/bulk-selection")
        return json({ id: "job", state: "done", counts: { done: 1 } });
      throw new Error(`unexpected ${method} ${url}`);
    },
    chosen([book("a")]),
  );
  await act(async () => dispatch(exact(x.view, "Add to collection…"), "click"));
  await settle();
  assert.equal(
    x.calls.filter((call) => call.path.startsWith("/api/collections")).length,
    2,
  );
  assert.doesNotMatch(visibleText(x.view), /No static collections/);
  const select = one(x.view.container, "select");
  const options = all(select, "option").map((node) => node.value);
  assert.equal(options.length, 1 + 75 + 1);
  assert.ok(options.includes("c350") && !options.includes("c0"));
  await act(async () =>
    dispatch(select, "change", { target: { value: "c350" } }),
  );
  await act(async () => dispatch(exact(x.view, "Add"), "click"));
  await settle();
  const posted = x.calls.find((call) => call.path === "/api/bulk-selection");
  assert.deepEqual(
    [posted.body.action.target_id, posted.body.action.target_revision],
    ["c350", 4],
  );
  assert.equal(latestToast(), "Added to “Collection 350”");
});
test("the collection picker says there is no static collection only after the last page", async (context) => {
  const listed = pages(
    Array.from({ length: 201 }, (_, i) => collection(i, "dynamic")),
  );
  const x = await bookMenu(
    context,
    (method, url) => listed(url),
    chosen([book("a")]),
  );
  await act(async () => dispatch(exact(x.view, "Add to collection…"), "click"));
  await settle();
  assert.equal(x.calls.length, 2);
  assert.match(visibleText(x.view), /No static collections yet/);
});
/** Adds the entire selection to collection c1 (revision 4) in one job. */
async function addMany(context, count, bulk) {
  const books = Array.from({ length: count }, (_, i) => book(`b${i}`));
  const x = await bookMenu(
    context,
    (method, url, init) => {
      if (url.pathname === "/api/collections")
        return json({ items: [collection(1)], after: null });
      if (method === "POST" && url.pathname === "/api/bulk-selection")
        return bulk(JSON.parse(init.body));
      throw new Error(`unexpected ${method} ${url}`);
    },
    chosen(books),
  );
  await act(async () => dispatch(exact(x.view, "Add to collection…"), "click"));
  await settle();
  await act(async () =>
    dispatch(one(x.view.container, "select"), "change", {
      target: { value: "c1" },
    }),
  );
  await act(async () => dispatch(exact(x.view, "Add"), "click"));
  await settle();
  return x.calls
    .filter((call) => call.path === "/api/bulk-selection")
    .map((call) => [
      call.body.action.target_revision,
      call.body.book_ids.length,
    ]);
}
for (const count of [500, 501, 1001]) {
  test(`adding ${count} chosen books submits one complete server job`, async (context) => {
    const sent = await addMany(context, count, (body) =>
      json({
        id: body.id,
        state: "done",
        counts: { pending: 0, done: body.book_ids.length },
      }),
    );
    assert.deepEqual(sent, [[4, count]]);
    assert.equal(latestToast(), "Added to “Collection 001”");
  });
}
test("a server-side collection conflict is reported for the complete selection", async (context) => {
  const sent = await addMany(context, 1001, (body) =>
    json({ id: body.id, state: "done", counts: { done: 500, conflict: 501 } }),
  );
  assert.deepEqual(sent, [[4, 1001]]);
  assert.match(latestToast(), /^501 not done/);
});

test("refreshing more than 500 books completes every server batch before reloading", async (context) => {
  const ids = Array.from({ length: 501 }, (_, i) => `book-${i}`);
  const x = await bookMenu(
    context,
    (method, url) => {
      if (url.pathname === "/api/removals/search/selection")
        return json({ items: ids });
      if (url.pathname === "/api/thumbnails/refresh")
        return json({ refreshed: 1 });
    },
    {
      books: [],
      all: { total: ids.length, exclude: [], browse: initialBrowse() },
      anchor: book(ids[0]),
      unfinished: true,
    },
  );
  await click(button(x.view, "Refresh thumbnails"));
  await settle();
  const batches = x.calls.filter(
    (call) => call.path === "/api/thumbnails/refresh",
  );
  assert.deepEqual(
    batches.map((call) => call.body.book_ids.length),
    [500, 1],
  );
  assert.deepEqual(
    batches.flatMap((call) => call.body.book_ids),
    ids,
  );
  assert.equal(x.changed(), 1);
});

test("a failed later thumbnail batch still reloads the revisions already changed", async (context) => {
  const books = Array.from({ length: 501 }, (_, i) => book(`book-${i}`));
  let batches = 0;
  const x = await bookMenu(
    context,
    (method, url) => {
      if (url.pathname === "/api/thumbnails/refresh") {
        batches++;
        return batches === 1
          ? json({ refreshed: 500 })
          : json({ detail: "offline" }, 503);
      }
    },
    chosen(books),
  );
  await click(button(x.view, "Refresh thumbnails"));
  await settle();
  assert.equal(batches, 2);
  assert.equal(x.changed(), 1);
  assert.notEqual(latestToast(), "Thumbnails will be rendered again");
});

test("bulk progress offers cancellation and reports only completed changes", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let cancelled = false;
  const x = await bookMenu(
    context,
    (method, url) => {
      if (url.pathname === "/api/bulk-selection")
        return json({
          id: "job",
          state: "running",
          counts: { pending: 2, done: 1 },
        });
      if (url.pathname === "/api/bulk/job/cancel") {
        assert.equal(method, "POST");
        cancelled = true;
        return json({});
      }
      if (url.pathname === "/api/bulk/job")
        return json({
          id: "job",
          state: cancelled ? "cancelled" : "running",
          counts: {
            pending: cancelled ? 0 : 2,
            done: 1,
            cancelled: cancelled ? 2 : 0,
          },
        });
    },
    chosen([book("a"), book("b"), book("c")]),
  );
  await click(button(x.view, "Mark as read"));
  const action = toast.getHistory().at(-1).action;
  assert.equal(action.label, "Stop remaining");
  await act(async () => action.onClick());
  await tick(context, BULK_POLL_MS);
  assert.equal(latestToast(), "Stopped: 1 completed, 2 not applied.");
  assert.equal(x.changed(), 1);
});
