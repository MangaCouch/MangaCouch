import assert from "node:assert/strict";
import test from "node:test";
import {
  advanceTrail,
  defaultTrail,
  rememberTrail,
  restoreTrail,
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
