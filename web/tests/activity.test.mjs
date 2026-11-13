import { all, attr, dispatch, one } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import { act } from "@testing-library/react";
import { Activity } from "../src/Activity.tsx";
import { renderPage } from "./harness.mjs";

const DAY = 86400;
/** Noon of the local day `daysAgo` days back, in the UTC offset the page sends. */
function noon(daysAgo) {
  const offset = -new Date().getTimezoneOffset() * 60;
  const today = Math.floor((Date.now() / 1000 + offset) / DAY);
  return (today - daysAgo) * DAY - offset + DAY / 2;
}
/** A reading event on the local day `daysAgo` days back, as the server records it. */
const event = (id, book_id, daysAgo, values = {}) => ({
  id,
  book_id,
  created_at: noon(daysAgo) - id,
  page: 2,
  completed: 0,
  kind: "progress",
  ...values,
});
async function setup(t, prepare = () => {}) {
  const x = await renderPage(t, Activity, { prepare });
  const requests = (path) => [
    ...x.fixture.requests.filter((request) => request.path === path),
  ];
  return {
    ...x,
    requests,
    bars: () => all(x.view.container, { className: "activity-day" }),
    entries: () => all(x.view.container, { className: "history-entry" }),
    period: (days) =>
      act(async () =>
        dispatch(
          one(x.view.container, { "aria-label": "Statistics period" }),
          "change",
          { target: { value: String(days) } },
        ),
      ),
  };
}
const card = (x, label) =>
  all(x.view.container, { className: "stat-card panel" })
    .find((node) => node.textContent.endsWith(label))
    ?.querySelector("strong").textContent;

test("statistics count the library and the reading of the chosen period by local day", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.books[0].personal.completed = true;
    fixture.books[2].personal.resume_page = 1;
    fixture.readingEvents.push(
      event(1, "fixture-0", 0, { kind: "completed", completed: 1, page: 3 }),
      event(2, "fixture-2", 0),
      event(3, "fixture-2", 3),
      event(4, "fixture-3", 3, { kind: "reset", page: null }),
      event(5, "fixture-4", 45),
    );
  });
  const [statistics] = x.requests("/api/statistics");
  assert.equal(statistics.query.get("days"), "30");
  assert.equal(
    statistics.query.get("timezone"),
    Intl.DateTimeFormat().resolvedOptions().timeZone,
  );
  assert.equal(card(x, "Library books"), "6");
  // Resets and events before the period do not count.
  assert.equal(card(x, "Books in period"), "2");
  assert.equal(card(x, "Active days"), "2");
  assert.equal(card(x, "Finished in period"), "1");
  assert.match(x.text(), /Library: 24 pages/);
  assert.match(x.text(), /Finished 1Reading 1Rated 1/);
  assert.match(x.text(), /Chinese 5English 1/);
  // One bar per day of the period, the newest last with today's two books.
  assert.equal(x.bars().length, 30);
  assert.match(attr(x.bars().at(-1), "title"), /^\d{4}-\d{2}-\d{2}: 2$/);
  assert.equal(x.bars().at(-1).querySelector("span").textContent, "2");
  assert.match(attr(x.bars().at(-4), "title"), /: 1$/);
});
test("switching the period reloads only the statistics", async (t) => {
  const x = await setup(t, (fixture) =>
    fixture.readingEvents.push(
      event(1, "fixture-0", 3),
      event(2, "fixture-1", 10),
    ),
  );
  assert.equal(card(x, "Active days"), "2");
  assert.equal(x.bars().length, 30);
  await x.period(7);
  assert.deepEqual(
    x.requests("/api/statistics").map((request) => request.query.get("days")),
    ["30", "7"],
  );
  assert.equal(x.requests("/api/reading-history").length, 1);
  assert.equal(x.bars().length, 7);
  assert.equal(card(x, "Active days"), "1");
  assert.equal(x.entries().length, 2);
  await x.period(365);
  assert.equal(x.bars().length, 365);
  // A long period leaves the per-bar counts out; the axis keeps about eight dates.
  assert.equal(
    x.view.container.querySelectorAll(".activity-day span").length,
    0,
  );
  assert.ok(
    all(x.view.container, ".activity-day small").filter(
      (node) => node.textContent,
    ).length <= 9,
  );
});
test("a period without reading says so instead of drawing bars", async (t) => {
  const x = await setup(t, (fixture) =>
    fixture.readingEvents.push(event(1, "fixture-0", 10)),
  );
  await x.period(7);
  assert.equal(card(x, "Active days"), "0");
  assert.equal(x.bars().length, 0);
  assert.match(x.text(), /No reading activity in this period/);
  assert.equal(x.entries().length, 1);
});
test("an empty history invites reading instead of listing anything", async (t) => {
  const x = await setup(t);
  assert.equal(x.entries().length, 0);
  assert.match(x.text(), /Open a book to start your reading history/);
  assert.match(x.text(), /No reading activity in this period/);
  assert.equal(x.button("Earlier activity"), undefined);
});
test("reading history pages back with the after cursor and each entry opens its reader", async (t) => {
  const x = await setup(t, (fixture) => {
    for (let id = 1; id <= 60; id++)
      fixture.readingEvents.push(
        event(id, `fixture-${id % 6}`, Math.floor(id / 3)),
      );
  });
  assert.equal(x.entries().length, 50);
  await x.click("Earlier activity");
  assert.equal(x.entries().length, 60);
  const [first, second] = x.requests("/api/reading-history");
  assert.equal(first.query.get("after"), "");
  assert.equal(first.query.get("limit"), "50");
  assert.notEqual(second.query.get("after"), "");
  assert.equal(x.button("Earlier activity"), undefined);
  await act(async () => dispatch(x.entries()[0], "click"));
  assert.deepEqual(x.routes, ["read/fixture-1"]);
});
test("history entries show the saved page, a finished book or a reset", async (t) => {
  const x = await setup(t, (fixture) =>
    fixture.readingEvents.push(
      event(1, "fixture-0", 0, { page: 2 }),
      event(2, "fixture-1", 0, { kind: "completed", completed: 1, page: 3 }),
      event(3, "fixture-2", 0, { kind: "reset", page: null }),
    ),
  );
  const labels = x.entries().map((entry) => entry.textContent);
  assert.match(labels[0], /山间来信.*3 \/ 4/);
  assert.match(labels[1], /The Quiet Observatory.*Finished/);
  assert.match(labels[2], /海边的慢日子.*Progress reset/);
});
test("a failed load says why and Retry loads the page again", async (t) => {
  const x = await setup(t, (fixture) => {
    fixture.activityFailure = true;
    fixture.readingEvents.push(event(1, "fixture-0", 0));
  });
  assert.match(x.text(), /Library storage is unavailable/);
  assert.equal(x.entries().length, 0);
  x.fixture.activityFailure = false;
  await x.click("Retry");
  assert.equal(all(x.view.container, { role: "alert" }).length, 0);
  assert.equal(card(x, "Active days"), "1");
  assert.equal(x.entries().length, 1);
});

test("a failed period switch clears the previous period's statistics", async (t) => {
  const x = await setup(t, (f) =>
    f.readingEvents.push(event(1, "fixture-0", 3)),
  );
  assert.equal(card(x, "Active days"), "1");
  const fetch = globalThis.fetch;
  t.mock.method(globalThis, "fetch", async (path, options) =>
    String(path).includes("/api/statistics?days=7")
      ? new Response(JSON.stringify({ error: "storage_unavailable" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        })
      : fetch(path, options),
  );
  await x.period(7);
  assert.equal(card(x, "Active days"), undefined);
  assert.equal(x.bars().length, 0);
  assert.match(x.text(), /Library storage is unavailable/);
  assert.equal(x.entries().length, 1);
});

test("a manual completion counts as finished but not as reading", async (t) => {
  const x = await setup(t, (fixture) =>
    fixture.readingEvents.push(
      event(1, "fixture-1", 0),
      event(2, "fixture-0", 2, {
        kind: "completed",
        completed: 1,
        page: null,
        is_reading: 0,
      }),
    ),
  );
  assert.equal(card(x, "Books in period"), "1");
  assert.equal(card(x, "Active days"), "1");
  assert.equal(card(x, "Finished in period"), "1");
  assert.match(attr(x.bars().at(-3), "title"), /: 0$/);
  assert.equal(x.entries().length, 1);
});

test("statistics count days in the time zone the page names", async (t) => {
  await setup(t, (fixture) =>
    fixture.readingEvents.push(
      event(1, "fixture-0", 0, { created_at: Math.floor(Date.now() / 1000) }),
    ),
  );
  const today = async (zone) => {
    const response = await fetch(
      `/api/statistics?days=1&timezone=${encodeURIComponent(zone)}`,
    );
    return response.ok ? (await response.json()).daily[0] : response.status;
  };
  // UTC+14 and UTC-12 are always on different calendar days.
  const east = await today("Pacific/Kiritimati"),
    west = await today("Etc/GMT+12");
  assert.notEqual(east.day, west.day);
  assert.equal(east.updates, 1);
  assert.equal(west.updates, 1);
  assert.equal(await today("Not/A_Zone"), 422);
});
