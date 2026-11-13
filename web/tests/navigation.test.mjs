import assert from "node:assert/strict";
import test from "node:test";
import {
  advanceTrail,
  defaultTrail,
  rememberTrail,
  restoreTrail,
  returnRoute,
} from "../src/navigation.tsx";

test("history directly opens a reader, whose details must return to history without a loop", () => {
  let trail = ["", "#activity"];
  trail = advanceTrail(trail, "#read/first");
  trail = advanceTrail(trail, "#book/first");
  assert.equal(trail.at(-2), "#activity");
  assert.deepEqual(advanceTrail(trail, "#activity"), ["", "#activity"]);
});

test("moving between collection books preserves the collection return target", () => {
  let trail = defaultTrail("#read/first/in/series");
  trail = advanceTrail(trail, "#read/second/in/series");
  trail = advanceTrail(trail, "#book/second/in/series");
  assert.equal(trail.at(-2), "#collection/series");
  assert.ok(!trail.some((route) => route.includes("first")));
});

test("stepping back from a collection reader to an earlier book's details returns to the book being read", () => {
  assert.equal(
    returnRoute("#read/second/in/series", "#book/first/in/series"),
    "#book/second/in/series",
  );
  assert.equal(
    returnRoute("#read/second/in/series/page/3", "#book/first/in/series"),
    "#book/second/in/series",
  );
  // Same book, another collection, or not a reader: the entry stays what it was.
  for (const [from, landing] of [
    ["#read/second/in/series", "#book/second/in/series"],
    ["#read/second/in/series", "#book/first/in/other"],
    ["#read/second", "#book/first"],
    ["#book/second/in/series", "#book/first/in/series"],
    ["#read/second/in/series", "#collection/series"],
  ])
    assert.equal(returnRoute(from, landing), landing);
});

test("reload keeps the entry path; unrelated deep links and invalid stored paths use their parent", (t) => {
  const old = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  let value = null;
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    value: { getItem: () => value, setItem: (_key, data) => (value = data) },
  });
  t.after(() =>
    old
      ? Object.defineProperty(globalThis, "sessionStorage", old)
      : delete globalThis.sessionStorage,
  );
  rememberTrail(["", "#collections", "#collection/test"]);
  assert.deepEqual(restoreTrail("#collection/test"), [
    "",
    "#collections",
    "#collection/test",
  ]);
  value = JSON.stringify(["https://untrusted.invalid", "#settings"]);
  assert.deepEqual(restoreTrail("#settings"), ["", "#settings"]);
});

test("history offsets restore each entry state and a new branch drops forward entries", async () => {
  const { hashRouting } = await import("./harness.mjs");
  const { location, history } = hashRouting(new EventTarget(), "#activity");
  history.replaceState({ mcPosition: 7 }, "", "#activity");
  location.hash = "#book/one";
  history.replaceState({ mcPosition: 8 }, "", "#book/one");
  location.hash = "#read/one";
  history.replaceState({ mcPosition: 9 }, "", "#read/one");
  history.go(-1);
  assert.equal(location.hash, "#book/one");
  assert.equal(history.state.mcPosition, 8);
  history.go(-1);
  assert.equal(history.state.mcPosition, 7);
  history.go(2);
  assert.equal(location.hash, "#read/one");
  history.go(-1);
  location.hash = "#settings";
  history.go(1);
  assert.equal(location.hash, "#settings");
});
