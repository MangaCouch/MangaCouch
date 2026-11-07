import assert from "node:assert/strict";
import test from "node:test";
import { all } from "./dom.mjs";
import React from "react";
import { Maintenance } from "../src/Maintenance.tsx";
import { button, click, setupFixture, visibleText } from "./harness.mjs";

async function setup(t, prepare = () => {}) {
  const x = await setupFixture(t, (fixture) => {
    fixture.maintenanceStates = {
      "fixture-0": "missing",
      "fixture-1": "missing",
      "fixture-2": "changed",
      "fixture-3": "unreadable",
    };
    fixture.maintenanceBatchSize = 2;
    prepare(fixture);
  });
  const routes = [];
  const view = await x.render(
    React.createElement(Maintenance, {
      t: (_, en) => en,
      navigate: (route) => routes.push(route),
    }),
  );
  return {
    fixture: x.fixture,
    routes,
    view,
    click: (text) => click(button(view, text)),
    text: () => visibleText(view),
  };
}

test("missing-file check starts explicitly, scans all chunks and never removes records", async (t) => {
  const x = await setup(t);
  assert.equal(x.fixture.requests.length, 0);
  await x.click("Check missing files");
  assert.match(x.text(), /Checked 6 books; 4 issues/);
  assert.equal(
    x.fixture.requests.filter((r) => r.path === "/api/removals/check").length,
    3,
  );
  assert.equal(x.fixture.books.length, 6);
  assert.equal(all(x.view.container, "input").length, 0);
});
test("missing-file check keeps partial results when storage disappears", async (t) => {
  const x = await setup(t, (f) => {
    f.maintenanceFailureAfter = 1;
  });
  await x.click("Check missing files");
  assert.match(x.text(), /Checked 2 books; 2 issues/);
  assert.match(x.text(), /Library storage is unavailable/);
  assert.equal(x.fixture.books.length, 6);
  assert.ok(x.fixture.requests.every((r) => r.path === "/api/removals/check"));
});
