import { all, dispatch } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { RemovalDialog } from "../src/RemovalDialog.tsx";
import { toast } from "../src/feedback.tsx";
import { button, click, english as t, tick } from "./harness.mjs";
const json = (value, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
const settle = () =>
  act(async () => {
    for (let i = 0; i < 80; i++)
      await new Promise((resolve) => setImmediate(resolve));
  });
async function setup(context, ids, respond) {
  const calls = [],
    removed = [];
  context.mock.method(globalThis, "fetch", async (path, init = {}) => {
    const data = init.body && JSON.parse(init.body);
    calls.push({ path, data });
    return respond(path, data);
  });
  let view;
  await act(async () => {
    view = render(
      React.createElement(RemovalDialog, {
        bookIds: ids,
        t,
        close() {},
        removed: (value) => removed.push(value),
      }),
    );
  });
  await settle();
  context.after(() => act(async () => view.unmount()));
  return { view, calls, removed };
}
const preview = (ids) => ({
  id: `batch-${ids[0]}`,
  token: "frozen",
  book_ids: ids,
  blocked: [],
  items: ids.map((id) => ({
    book_id: id,
    title: id,
    blocked: [],
    delete_blocked: [],
  })),
});
test("20,000 removal previews use 40 requests with at most two in flight and bounded titles", async (context) => {
  let active = 0,
    peak = 0;
  const ids = Array.from({ length: 20000 }, (_, i) => `b${i}`);
  const x = await setup(context, ids, async (path, data) => {
    assert.equal(path, "/api/removals/selected/preview");
    active++;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setImmediate(resolve));
    active--;
    return json(preview(data.book_ids));
  });
  assert.equal(x.calls.length, 40);
  assert.equal(peak, 2);
  assert.equal(all(x.view.container, ".removal-titles li").length, 101);
  assert.equal(button(x.view, "Remove").disabled, false);
});
test("removal retry preserves its receipt and warns when files were kept", async (context) => {
  let attempts = 0;
  const x = await setup(context, ["a", "b"], (path, data) => {
    if (path.endsWith("/preview")) return json(preview(data.book_ids));
    return ++attempts === 1
      ? json({ error: "library_busy" }, 503)
      : json({ deletion_stopped: "file_changed", items: [{ file: "kept" }] });
  });
  await act(async () =>
    dispatch(all(x.view.container, 'input[type="checkbox"]')[0], "change", {
      target: { checked: true },
    }),
  );
  await click(button(x.view, "Remove"));
  await settle();
  await click(button(x.view, "Remove"));
  await settle();
  assert.deepEqual(x.calls[1].data, x.calls[2].data);
  assert.equal(
    x.calls.filter((call) => call.path.endsWith("/preview")).length,
    1,
  );
  assert.deepEqual(x.removed, [["a", "b"]]);
  assert.equal(toast.getHistory().at(-1).type, "warning");
});
test("blocked removal gives its reason and a conflict never auto rechecks", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const x = await setup(context, ["a", "b"], (path, data) => {
    if (path.endsWith("/preview")) return json(preview(data.book_ids));
    return json({ error: "revision_conflict" }, 409);
  });
  await click(button(x.view, "Remove"));
  await settle();
  await tick(context, 30000);
  assert.equal(x.calls.length, 2);
  assert.ok(button(x.view, "Check again"));
});
