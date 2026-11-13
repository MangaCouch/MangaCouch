import assert from "node:assert/strict";
import test from "node:test";
import { PersonalQueue, SAVE_RETRY_DELAYS_MS } from "../src/personal.ts";
const book = () => ({
  id: "fixture",
  personal_revision: 1,
  personal: {
    resume_page: null,
    completed: false,
    rating: null,
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
    rating: 1,
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
    completed: true,
  }));
  assert.equal(await queue.flush(), false);
  queue.update((state) => ({ ...state, resume_page: 3 }));
  assert.equal(calls, 1);
  fail = false;
  await queue.retry();
  assert.equal(await queue.flush(), true);
  assert.equal(queue.current().personal.completed, true);
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

/** One book's personal state behind compare-and-set saves, as the real API keeps it. */
function fakeServer() {
  let stored = book();
  const fail = (status) =>
    Object.assign(Error(`failed (${status})`), { status });
  const server = {
    get book() {
      return stored;
    },
    /** No response at all (offline, connection dropped). */
    down: false,
    /** An HTTP failure status for every save, such as 503. */
    status: null,
    saves: [],
    loads: 0,
    /** Another device saves a change. */
    other(change) {
      stored = {
        ...stored,
        personal: change(stored.personal),
        personal_revision: stored.personal_revision + 1,
      };
    },
    save: async (state, revision, keepalive, recordActivity, baseState) => {
      server.saves.push({
        state,
        revision,
        keepalive,
        recordActivity,
        baseState,
      });
      if (server.down) throw new TypeError("Failed to fetch");
      if (server.status) throw fail(server.status);
      if (revision !== stored.personal_revision) {
        if (!baseState) throw fail(409);
        const changes = Object.fromEntries(
          Object.entries(state).filter(
            ([key, value]) => value !== baseState[key],
          ),
        );
        for (const [key, value] of Object.entries(changes))
          if (
            key !== "resume_page" &&
            stored.personal[key] !== baseState[key] &&
            stored.personal[key] !== value
          )
            throw fail(409);
        state = { ...stored.personal, ...changes };
        revision = stored.personal_revision;
      }
      stored = { ...stored, personal: state, personal_revision: revision + 1 };
      return stored;
    },
    load: async () => {
      server.loads++;
      if (server.down) throw new TypeError("Failed to fetch");
      return stored;
    },
  };
  return server;
}
function queueOn(server) {
  const statuses = [];
  const queue = new PersonalQueue(
    server.book,
    server.save,
    server.load,
    (_, status) => statuses.push(status),
  );
  return { queue, statuses };
}
const progress = (page) => (state) => ({ ...state, resume_page: page });
const rate = (rating) => (state) => ({ ...state, rating });
const finish = () => (state) => ({ ...state, completed: true });
const elapse = async (t, ms) => {
  t.mock.timers.tick(ms);
  await settle();
};

test("two devices: progress and unrelated edits rebase on the other device's save without undoing it", async () => {
  const server = fakeServer();
  const { queue, statuses } = queueOn(server);
  // The phone rated the book and finished it while this tablet kept an old revision.
  server.other((state) => ({ ...state, rating: 5 }));
  queue.update(progress(4));
  queue.update(finish());
  assert.equal(await queue.flush(), true);
  assert.deepEqual(server.book.personal, {
    resume_page: 4,
    completed: true,
    rating: 5,
  });
  // One rejected write, one reload, one replay; the reader never saw an error.
  assert.equal(server.saves.length, 2);
  assert.equal(server.loads, 1);
  assert.ok(!statuses.includes("error"));
  assert.equal(statuses.at(-1), "saved");
});

test("two devices: an edit to a value the other device also changed waits for the reader's choice", async () => {
  const server = fakeServer();
  const { queue, statuses } = queueOn(server);
  server.other(rate(5));
  queue.update(rate(2));
  assert.equal(await queue.flush(), false);
  assert.equal(statuses.at(-1), "error");
  assert.equal(queue.conflicted(), true);
  // Nothing replayed over the other device's rating, and coming online does not either.
  assert.equal(server.saves.length, 1);
  queue.online();
  await settle();
  assert.equal(server.saves.length, 1);
  assert.equal(server.book.personal.rating, 5);
  // Edits made meanwhile wait with the others.
  queue.update(progress(1));
  assert.equal(server.saves.length, 1);
  // Retry is the explicit choice to replay this device's edits on the latest state.
  await queue.retry();
  assert.equal(queue.conflicted(), false);
  assert.equal(server.book.personal.rating, 2);
  assert.equal(server.book.personal.resume_page, 1);
  assert.equal(statuses.at(-1), "saved");
});

test("two devices: reload drops this device's conflicting edit and keeps the other device's value", async () => {
  const server = fakeServer();
  const { queue } = queueOn(server);
  server.other(rate(5));
  queue.update(rate(3));
  assert.equal(await queue.flush(), false);
  assert.equal(queue.conflicted(), true);
  await queue.reload();
  assert.equal(queue.current().personal.rating, 5);
  assert.equal(server.book.personal.rating, 5);
  assert.equal(queue.conflicted(), false);
});

test("offline: saves retry with backoff, show as saving, and go out as soon as the connection returns", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  const { queue, statuses } = queueOn(server);
  server.down = true;
  queue.update(rate(3));
  await settle();
  assert.equal(server.saves.length, 1);
  assert.equal(statuses.at(-1), "saving");
  await elapse(t, SAVE_RETRY_DELAYS_MS[0] - 1);
  assert.equal(server.saves.length, 1);
  await elapse(t, 1);
  assert.equal(server.saves.length, 2);
  assert.ok(!statuses.includes("error"));
  // Back online: the waiting retry runs at once instead of after its delay.
  server.down = false;
  queue.online();
  await settle();
  assert.equal(server.saves.length, 3);
  assert.equal(server.book.personal.rating, 3);
  assert.equal(statuses.at(-1), "saved");
});

test("a write whose response was lost is recognised on the retry instead of conflicting", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  const save = server.save;
  let lose = true;
  const queue = new PersonalQueue(
    server.book,
    async (...args) => {
      const saved = await save(...args);
      if (lose) {
        lose = false;
        throw new TypeError("Failed to fetch");
      }
      return saved;
    },
    server.load,
    () => {},
  );
  queue.update(rate(4));
  queue.update(finish());
  await settle();
  await elapse(t, SAVE_RETRY_DELAYS_MS[0]);
  assert.equal(await queue.flush(), true);
  assert.equal(queue.conflicted(), false);
  assert.deepEqual(server.book.personal, {
    resume_page: null,
    completed: true,
    rating: 4,
  });
});

test("consecutive failures stop at the retry cap; only an explicit retry starts again", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  const { queue, statuses } = queueOn(server);
  server.status = 503;
  queue.update(rate(1));
  await settle();
  for (const delay of SAVE_RETRY_DELAYS_MS) {
    assert.equal(statuses.at(-1), "saving");
    await elapse(t, delay);
  }
  assert.equal(server.saves.length, 1 + SAVE_RETRY_DELAYS_MS.length);
  assert.equal(statuses.at(-1), "error");
  assert.equal(queue.conflicted(), false);
  await elapse(t, 10 * 60_000);
  assert.equal(server.saves.length, 1 + SAVE_RETRY_DELAYS_MS.length);
  server.status = null;
  await queue.retry();
  assert.equal(server.book.personal.rating, 1);
  assert.equal(statuses.at(-1), "saved");
});

test("after the cap, coming back online starts one more bounded round", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  const { queue, statuses } = queueOn(server);
  server.down = true;
  queue.update(rate(2));
  await settle();
  for (const delay of SAVE_RETRY_DELAYS_MS) await elapse(t, delay);
  assert.equal(statuses.at(-1), "error");
  server.down = false;
  queue.online();
  await settle();
  assert.equal(statuses.at(-1), "saved");
  assert.equal(server.book.personal.rating, 2);
});

test("a rejected request (4xx) is not retried automatically", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  const { queue, statuses } = queueOn(server);
  server.status = 422;
  queue.update(rate(2));
  await settle();
  await elapse(t, 60_000);
  assert.equal(server.saves.length, 1);
  assert.equal(statuses.at(-1), "error");
});

test("another device writing again and again only costs the bounded retries", async () => {
  const server = fakeServer();
  const load = server.load;
  // Every reload races with another progress write from the other device.
  server.load = async () => {
    const value = await load();
    server.other(progress(value.personal.resume_page + 10));
    return value;
  };
  const { queue, statuses } = queueOn(server);
  server.other(progress(0));
  queue.update(progress(5));
  assert.equal(await queue.flush(), true);
  assert.equal(server.saves.length, 1 + SAVE_RETRY_DELAYS_MS.length);
  assert.equal(statuses.at(-1), "error");
  assert.equal(queue.conflicted(), true);
});

test("edits made while a retry waits go out with it in one request", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  const { queue } = queueOn(server);
  server.down = true;
  queue.update(rate(3));
  await settle();
  queue.update(finish());
  queue.update(progress(2), 1500);
  await settle();
  assert.equal(server.saves.length, 1);
  server.down = false;
  await elapse(t, SAVE_RETRY_DELAYS_MS[0]);
  assert.equal(server.saves.length, 2);
  assert.deepEqual(server.saves[1].state, {
    resume_page: null,
    completed: true,
    rating: 3,
  });
  await elapse(t, 1500);
  assert.equal(server.book.personal.resume_page, 2);
});

test("flush sends a waiting retry at once, and flush(true) marks its request keepalive", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  const { queue } = queueOn(server);
  server.down = true;
  queue.update(rate(3));
  await settle();
  server.down = false;
  queue.update(progress(8), 1500);
  // What the reader does on pagehide.
  assert.equal(await queue.flush(true), true);
  assert.equal(server.saves.length, 2);
  assert.equal(server.saves[0].keepalive, false);
  assert.equal(server.saves[1].keepalive, true);
  assert.equal(server.book.personal.resume_page, 8);
  queue.update(progress(9), 1500);
  assert.equal(await queue.flush(), true);
  assert.equal(server.saves.at(-1).keepalive, false);
});

test("closing the queue cancels a waiting retry", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  const { queue } = queueOn(server);
  server.down = true;
  queue.update(rate(3));
  await settle();
  queue.close();
  server.down = false;
  await elapse(t, 60_000);
  assert.equal(server.saves.length, 1);
});

for (const [field, remote, local, delay] of [
  ["rating", 3, 5, 0],
  ["completed", true, false, 1500],
]) {
  test(`409 reload checks ${field} edits added while the reload is in flight`, async () => {
    const server = fakeServer();
    const loading = defer();
    server.load = () => loading.promise;
    const { queue } = queueOn(server);
    server.other((state) => ({ ...state, [field]: remote }));
    queue.update((state) => ({
      ...state,
      resume_page: 1,
      ...(field === "completed" ? { completed: true } : {}),
    }));
    await settle();
    queue.update((state) => ({ ...state, [field]: local }), delay);
    loading.resolve(server.book);
    await settle();
    assert.equal(await queue.flush(), false);
    assert.equal(queue.conflicted(), true);
    assert.equal(server.saves.length, 1);
    assert.deepEqual(server.book.personal[field], remote);
    await queue.retry();
    assert.deepEqual(server.book.personal[field], local);
    queue.close();
  });
}

test("409 reload preserves unrelated edits added while loading", async () => {
  const server = fakeServer();
  const loading = defer();
  server.load = () => loading.promise;
  const { queue } = queueOn(server);
  server.other(rate(3));
  queue.update(progress(1));
  await settle();
  queue.update(finish());
  loading.resolve(server.book);
  assert.equal(await queue.flush(), true);
  assert.equal(server.book.personal.rating, 3);
  assert.equal(server.book.personal.completed, true);
  assert.equal(server.book.personal.resume_page, 1);
  queue.close();
});

test("keepalive covers queued writes behind an in-flight save and concurrent flushes", async () => {
  const server = fakeServer();
  const first = defer();
  const saved = server.save;
  let hold = true;
  server.save = async (...args) => {
    const result = await saved(...args);
    if (hold) {
      hold = false;
      await first.promise;
    }
    return result;
  };
  const { queue } = queueOn(server);
  queue.update(progress(1));
  await settle();
  queue.update(progress(2), 1500);
  const flushes = [queue.flush(true), queue.flush(), queue.flush(true)];
  assert.equal(server.saves.length, 2);
  assert.equal(server.saves[1].state.resume_page, 2);
  assert.deepEqual(server.saves[1].baseState, book().personal);
  first.resolve();
  assert.deepEqual(await Promise.all(flushes), [true, true, true]);
  assert.equal(server.saves.length, 2);
  assert.equal(server.saves[0].keepalive, false);
  assert.equal(server.saves[1].keepalive, true);
  assert.equal(server.book.personal.resume_page, 2);
  queue.update(progress(3));
  await queue.flush();
  assert.equal(server.saves.at(-1).keepalive, false);
  queue.close();
});

test("404 discards edits and invokes the missing-book navigation once", async () => {
  let missing = 0;
  const q = new PersonalQueue(
    book(),
    async () => {
      throw { status: 404 };
    },
    async () => book(),
    () => {},
    () => missing++,
  );
  q.update((state) => ({ ...state, rating: 4 }));
  assert.equal(await q.flush(), true);
  assert.equal(missing, 1);
  assert.equal(q.current().personal.rating, null);
  q.close();
});

test("reload discards failed edits even when the reload request itself fails", async () => {
  const q = new PersonalQueue(
    book(),
    async () => {
      throw { status: 422 };
    },
    async () => {
      throw { status: 503 };
    },
    () => {},
  );
  q.update((state) => ({ ...state, rating: 5 }));
  assert.equal(await q.flush(), false);
  await q.reload();
  assert.equal(q.current().personal.rating, null);
  q.close();
});

test("failed progress can leave but toggled completion remains a guarded edit", async () => {
  const q = new PersonalQueue(
    book(),
    async () => {
      throw { status: 422 };
    },
    async () => book(),
    () => {},
  );
  q.update((state) => ({ ...state, resume_page: 2 }));
  await q.flush();
  assert.equal(q.canDiscardProgress(), true);
  q.update((state) => ({ ...state, completed: true }));
  q.update((state) => ({ ...state, completed: false }));
  assert.equal(q.canDiscardProgress(), false);
  q.close();
});

test("reading activity belongs to the queued edit even when sent after leaving the reader", async () => {
  const server = fakeServer();
  server.down = true;
  const { queue } = queueOn(server);
  queue.update(progress(1), 0, true);
  await settle();
  server.down = false;
  queue.online();
  await queue.flush();
  assert.equal(server.saves.at(-1).recordActivity, true);
  queue.update((state) => ({ ...state, rating: 4 }));
  await queue.flush();
  assert.equal(server.saves.at(-1).recordActivity, false);
  queue.close();
});

/** A queue whose saves each wait for the test to answer them. */
function heldServer() {
  const server = fakeServer();
  const answers = [];
  const save = server.save;
  server.save = (...args) =>
    new Promise((resolve, reject) =>
      answers.push({
        ok: () => save(...args).then(resolve, reject),
        fail: (status) =>
          reject(
            status
              ? Object.assign(Error(`failed (${status})`), { status })
              : new TypeError("Failed to fetch"),
          ),
      }),
    );
  return { server, answers };
}

for (const [failure, status] of [
  ["503", 503],
  ["a network error", null],
]) {
  test(`a hidden tab's snapshot failing with ${failure} retries on the usual schedule`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const { server, answers } = heldServer();
    const { queue, statuses } = queueOn(server);
    queue.update(progress(4));
    await settle();
    queue.update(progress(5), 1500);
    void queue.flush(true); // hidden, not closed: the page stays
    await settle();
    assert.equal(answers.length, 2);
    answers[1].fail(status);
    await settle();
    answers[0].ok();
    await settle();
    // The earlier save succeeded, so the remaining edit goes out at once.
    assert.equal(answers.length, 3);
    assert.equal(statuses.at(-1), "saving");
    answers[2].ok();
    await settle();
    assert.equal(server.book.personal.resume_page, 5);
    assert.equal(statuses.at(-1), "saved");
    queue.close();
  });
}

test("a failed snapshot after the earlier save finished waits for the retry delay", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { server, answers } = heldServer();
  const { queue, statuses } = queueOn(server);
  queue.update(progress(4));
  await settle();
  queue.update(progress(5), 1500);
  void queue.flush(true);
  await settle();
  answers[0].ok();
  await settle();
  answers[1].fail(503);
  await settle();
  assert.equal(answers.length, 2);
  assert.equal(statuses.at(-1), "saving");
  assert.equal(queue.conflicted(), false);
  await elapse(t, SAVE_RETRY_DELAYS_MS[0]);
  assert.equal(answers.length, 3);
  answers[2].ok();
  await settle();
  assert.equal(server.book.personal.resume_page, 5);
  assert.equal(statuses.at(-1), "saved");
  queue.close();
});

test("a snapshot conflict stops the queue and the status says so", async () => {
  const { server, answers } = heldServer();
  const { queue, statuses } = queueOn(server);
  queue.update(progress(4));
  await settle();
  queue.update(rate(2));
  void queue.flush(true);
  await settle();
  answers[1].fail(409);
  await settle();
  answers[0].ok();
  await settle();
  assert.equal(answers.length, 2);
  assert.equal(statuses.at(-1), "error");
  assert.equal(queue.conflicted(), true);
  queue.close();
});
