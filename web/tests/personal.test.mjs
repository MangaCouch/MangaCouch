import assert from "node:assert/strict";
import test from "node:test";
import { PersonalQueue } from "../src/personal.ts";
const book = () => ({
  id: "fixture",
  personal_revision: 1,
  personal: {
    resume_page: null,
    completed: false,
    rating: null,
    bookmarks: [],
  },
});
const defer = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
const settle = () => new Promise((resolve) => setImmediate(resolve));
test("personal edits serialize, preserve unrelated edits, and use fresh revisions", async () => {
  const calls = [],
    pending = [],
    notifications = [];
  const initial = book();
  const queue = new PersonalQueue(
    initial,
    (state, revision) => {
      calls.push({ state, revision });
      const d = defer();
      pending.push(d);
      return d.promise;
    },
    async () => initial,
    (value, status) => notifications.push({ value, status }),
  );
  queue.update((state) => ({ ...state, resume_page: 1 }));
  await settle();
  queue.update((state) => ({
    ...state,
    bookmarks: [...new Set([...state.bookmarks, 1])],
  }));
  queue.update((state) => ({ ...state, resume_page: 2, completed: true }));
  queue.update((state) => ({ ...state, rating: 4.5 }));
  assert.equal(calls.length, 1);
  pending[0].resolve({
    ...initial,
    personal: calls[0].state,
    personal_revision: 2,
  });
  await settle();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].revision, 2);
  assert.deepEqual(calls[1].state, {
    resume_page: 2,
    completed: true,
    rating: 4.5,
    bookmarks: [1],
  });
  pending[1].resolve({
    ...initial,
    personal: calls[1].state,
    personal_revision: 3,
  });
  assert.equal(await queue.flush(), true);
  assert.equal(notifications.at(-1).status, "saved");
});
test("failed saves stop until explicit retry, rebase idempotent actions on latest state", async () => {
  const initial = book();
  let server = initial,
    fail = true,
    calls = 0;
  const queue = new PersonalQueue(
    initial,
    async (state, revision) => {
      calls++;
      assert.equal(revision, server.personal_revision);
      server = { ...server, personal: state, personal_revision: revision + 1 };
      if (fail) throw Error("response lost after server committed");
      return server;
    },
    async () => ({ ...server, personal: { ...server.personal, rating: 5 } }),
    () => {},
  );
  queue.update((state) => ({
    ...state,
    bookmarks: [...new Set([...state.bookmarks, 2])],
  }));
  assert.equal(await queue.flush(), false);
  queue.update((state) => ({ ...state, resume_page: 3 }));
  assert.equal(calls, 1);
  fail = false;
  await queue.retry();
  assert.equal(await queue.flush(), true);
  assert.deepEqual(queue.current().personal.bookmarks, [2]);
  assert.equal(queue.current().personal.rating, 5);
  assert.equal(queue.current().personal.resume_page, 3);
});
test("retry with no changes does not prevent later edits", async () => {
  const initial = book();
  let count = 0;
  const q = new PersonalQueue(
    initial,
    async (state) => {
      count++;
      return { ...initial, personal: state };
    },
    async () => initial,
    () => {},
  );
  await q.retry();
  q.update((state) => ({ ...state, rating: 4 }));
  assert.equal(await q.flush(), true);
  assert.equal(count, 1);
});
test("reload discards earlier unsaved edits but preserves edits made while it loads", async () => {
  const initial = book();
  let fail = true;
  const load = defer();
  const q = new PersonalQueue(
    initial,
    async (state) => {
      if (fail) throw Error();
      return { ...initial, personal: state };
    },
    () => load.promise,
    () => {},
  );
  q.update((state) => ({ ...state, resume_page: 2 }));
  await q.flush();
  const reload = q.reload();
  await settle();
  q.update((state) => ({ ...state, rating: 3 }));
  fail = false;
  load.resolve(initial);
  await reload;
  assert.equal(q.current().personal.resume_page, null);
  assert.equal(q.current().personal.rating, 3);
});
test("closing a queue ignores late responses", async () => {
  const d = defer(),
    notifications = [];
  const q = new PersonalQueue(
    book(),
    () => d.promise,
    async () => book(),
    (_, status) => notifications.push(status),
  );
  q.update((state) => ({ ...state, rating: 2 }));
  await settle();
  q.close();
  d.resolve(book());
  await q.flush();
  assert.deepEqual(notifications, ["saving"]);
});
test("no-op edits do not issue writes", async () => {
  let count = 0;
  const q = new PersonalQueue(
    book(),
    async () => {
      count++;
      return book();
    },
    async () => book(),
    () => {},
  );
  q.update((state) => ({ ...state }));
  await q.flush();
  assert.equal(count, 0);
});

test("delayed edits collapse into one save, flush sends them at once and close drops them", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const saves = [];
  let server = book();
  const queue = new PersonalQueue(
    server,
    async (state, revision) => {
      saves.push(state.resume_page);
      server = { ...server, personal: state, personal_revision: revision + 1 };
      return server;
    },
    async () => server,
    () => {},
  );
  for (const page of [1, 2, 3])
    queue.update((state) => ({ ...state, resume_page: page }), 1500);
  assert.equal(queue.current().personal.resume_page, 3);
  t.mock.timers.tick(1499);
  await settle();
  assert.deepEqual(saves, []);
  t.mock.timers.tick(1);
  await settle();
  assert.deepEqual(saves, [3]);
  queue.update((state) => ({ ...state, resume_page: 4 }), 1500);
  assert.equal(await queue.flush(), true);
  assert.deepEqual(saves, [3, 4]);
  queue.update((state) => ({ ...state, resume_page: 5 }), 1500);
  queue.close();
  t.mock.timers.tick(5000);
  await settle();
  assert.deepEqual(saves, [3, 4]);
});

test("flush right before close still sends the delayed progress edit", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const saves = [],
    notifications = [];
  let server = book();
  const queue = new PersonalQueue(
    server,
    async (state, revision) => {
      saves.push(state.resume_page);
      server = { ...server, personal: state, personal_revision: revision + 1 };
      return server;
    },
    async () => server,
    (_, status) => notifications.push(status),
  );
  queue.update((state) => ({ ...state, resume_page: 7 }), 1500);
  // What BookView does on unmount, before the Reader's own cleanup can run.
  void queue.flush();
  queue.close();
  t.mock.timers.tick(5000);
  await settle();
  assert.deepEqual(saves, [7]);
  assert.deepEqual(notifications, ["saving"]);
});
