import assert from "node:assert/strict";
import test from "node:test";
import { Autoplay, adjacent, readingStatus, startingPage } from "../src/reading.ts";

function setup() {
  let now = 0,
    id = 0,
    turns = 0;
  const tasks = new Map(),
    history = [];
  const context = {
    key: "book:0",
    ready: false,
    atEnd: false,
    visible: true,
    blocked: false,
  };
  const clock = {
    set(fn, delay) {
      const key = ++id;
      tasks.set(key, { fn, at: now + delay });
      history.push(fn);
      return key;
    },
    clear(key) {
      tasks.delete(key);
    },
  };
  const player = new Autoplay(
    () => turns++,
    () => {},
    clock,
  );
  const update = (change) => {
    Object.assign(context, change);
    player.update({ ...context });
  };
  const tick = (amount) => {
    now += amount;
    for (const [key, task] of [...tasks])
      if (task.at <= now) {
        tasks.delete(key);
        task.fn();
      }
  };
  update({});
  return { player, update, tick, tasks, history, turns: () => turns };
}
test("waits for image readiness, turns once, then waits for next image", () => {
  const x = setup();
  x.player.start();
  x.tick(90_000);
  assert.equal(x.turns(), 0);
  x.update({ ready: true });
  x.tick(7999);
  assert.equal(x.turns(), 0);
  x.tick(1);
  assert.equal(x.turns(), 1);
  x.tick(120_000);
  assert.equal(x.turns(), 1);
  x.update({ key: "book:1", ready: false });
  x.tick(9000);
  assert.equal(x.turns(), 1);
  x.update({ ready: true });
  x.tick(8000);
  assert.equal(x.turns(), 2);
});
for (const [name, change] of Object.entries({
  hidden: { visible: false },
  end: { atEnd: true },
  failureOrSettings: { blocked: true },
})) {
  test(`${name} cancels playback and never resumes automatically`, () => {
    const x = setup();
    x.update({ ready: true });
    x.player.start();
    x.update(change);
    x.tick(9000);
    assert.equal(x.turns(), 0);
    assert.equal(x.player.playing, false);
    x.update({ visible: true, atEnd: false, blocked: false });
    x.tick(9000);
    assert.equal(x.turns(), 0);
  });
}
test("manual pause rejects a callback already queued before cancellation", () => {
  const x = setup();
  x.update({ ready: true });
  x.player.start();
  const stale = x.history.at(-1);
  x.player.pause();
  stale();
  assert.equal(x.turns(), 0);
});
test("page change cancels old deadline and interval changes restart a full interval", () => {
  const x = setup();
  x.update({ ready: true });
  x.player.start();
  x.tick(7000);
  x.update({ key: "book:1", ready: false });
  x.tick(5000);
  assert.equal(x.turns(), 0);
  x.update({ ready: true });
  x.player.setSeconds(3);
  x.tick(2000);
  x.player.setSeconds(5);
  x.tick(4999);
  assert.equal(x.turns(), 0);
  x.tick(1);
  assert.equal(x.turns(), 1);
});
test("disposal cancels pending work and invalid intervals are rejected", () => {
  const x = setup();
  x.update({ ready: true });
  x.player.start();
  x.player.dispose();
  x.tick(9000);
  assert.equal(x.turns(), 0);
  for (const value of [NaN, Infinity, 0, 61])
    assert.throws(() => x.player.setSeconds(value));
});
test("adjacent navigation stays within the page list and resume positions are clamped", () => {
  const pages = [0, 1, 2, 3, 4];
  assert.equal(adjacent(pages, 2, 1), 3);
  assert.equal(adjacent(pages, 2, -1), 1);
  assert.equal(adjacent(pages, 4, 1), undefined);
  assert.equal(adjacent(pages, 0, -1), undefined);
  assert.equal(startingPage(5, null), 0);
  assert.equal(startingPage(5, 3), 3);
  assert.equal(startingPage(5, 9), 4);
  assert.equal(startingPage(5, -2), 0);
});
test("zooming keeps the point under the fingers in place", async () => {
  const { zoomedOffset } = await import("../src/readerInput.ts");
  // Content point 100 + 50 = 150 doubles to 300; keeping it 50 px from the edge needs 250.
  assert.equal(zoomedOffset(100, 70, 20, 1, 2), 250);
  assert.equal(zoomedOffset(250, 70, 20, 2, 1), 100);
  // Zooming out at the very start never scrolls before the origin.
  assert.equal(zoomedOffset(0, 70, 20, 2, 1), 0);
});
test("reading status stays finished independent of saved progress", () => {
  const state = (completed, resume_page) => ({completed, resume_page});
  for (const page of [null, 0, 4, 9]) assert.equal(readingStatus(state(true, page)), "finished");
  assert.equal(readingStatus(state(false, null)), "unread");
  assert.equal(readingStatus(state(false, 0)), "reading");
});
