import assert from "node:assert/strict";
import test from "node:test";
import { restoreScroll } from "../src/viewState.ts";
import { tagQuery } from "../src/Pills.tsx";
import { installGlobals } from "./harness.mjs";

function layout(t) {
  let height = 100;
  let resize;
  let disconnected = 0;
  let settled = 0;
  const scrolls = [];
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.mock.method(window, "scrollTo", (_x, y) => scrolls.push(y));
  const descriptor = Object.getOwnPropertyDescriptor(
    document.documentElement,
    "scrollHeight",
  );
  Object.defineProperty(document.documentElement, "scrollHeight", {
    configurable: true,
    get: () => height,
  });
  t.after(() =>
    descriptor
      ? Object.defineProperty(
          document.documentElement,
          "scrollHeight",
          descriptor,
        )
      : delete document.documentElement.scrollHeight,
  );
  t.after(
    installGlobals({
      ResizeObserver: class {
        constructor(callback) {
          resize = callback;
        }
        observe() {}
        disconnect() {
          disconnected++;
        }
      },
    }),
  );
  const stop = restoreScroll(900, () => settled++);
  t.after(stop);
  return {
    scrolls,
    grow() {
      height = 3000;
      resize();
    },
    get settled() {
      return settled;
    },
    get disconnected() {
      return disconnected;
    },
  };
}
test("a delayed taller layout restores the saved position once", (t) => {
  const x = layout(t);
  x.grow();
  assert.equal(x.settled, 1);
  assert.equal(x.scrolls.at(-1), 900);
  assert.equal(x.disconnected, 1);
  const count = x.scrolls.length;
  x.grow();
  assert.equal(x.scrolls.length, count);
});
test("an unreachable scroll expires and later growth cannot drag the page", (t) => {
  const x = layout(t);
  t.mock.timers.tick(1500);
  assert.equal(x.settled, 1);
  const count = x.scrolls.length;
  x.grow();
  assert.equal(x.scrolls.length, count);
});
test("mouse interaction immediately owns scroll even when the old position is unreachable", (t) => {
  const x = layout(t);
  window.dispatchEvent(new Event("pointerdown"));
  assert.equal(x.settled, 1);
  const count = x.scrolls.length;
  x.grow();
  assert.equal(x.scrolls.length, count);
});
test("custom tag namespaces use matching quoted search syntax", () => {
  for (const namespace of [
    "custom group",
    "custom:group",
    'custom"group',
    "custom\\group",
  ])
    assert.equal(
      tagQuery(namespace, 'a "value"'),
      `${JSON.stringify(namespace)}:${JSON.stringify('a "value"')}`,
    );
  assert.equal(tagQuery("female", "glasses"), 'female:"glasses"');
});
