import assert from "node:assert/strict";
import test from "node:test";
import { all } from "./dom.mjs";
import { Maintenance } from "../src/Maintenance.tsx";
import { renderPage } from "./harness.mjs";

const setup = (t, prepare = () => {}) =>
  renderPage(t, Maintenance, {
    prepare: (fixture) => {
      fixture.maintenanceStates = {
        "fixture-0": "missing",
        "fixture-1": "missing",
        "fixture-2": "changed",
        "fixture-3": "unreadable",
      };
      fixture.maintenanceBatchSize = 2;
      prepare(fixture);
    },
  });

test("missing-file check starts explicitly, scans all chunks and never removes records", async (t) => {
  const x = await setup(t);
  assert.equal(x.fixture.requests.length, 0);
  await x.click("Check");
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
  await x.click("Check");
  assert.match(x.text(), /Checked 2 books; 2 issues/);
  assert.match(x.text(), /Library storage is unavailable/);
  assert.equal(x.fixture.books.length, 6);
  assert.ok(x.fixture.requests.every((r) => r.path === "/api/removals/check"));
});

test("retry continues a missing-file check from its last successful page", async (t) => {
  const x = await setup(t, (f) => {
    f.maintenanceFailureAfter = 1;
  });
  await x.click("Check");
  x.fixture.maintenanceFailureAfter = null;
  await x.click("Continue check");
  assert.match(x.text(), /Checked 6 books; 4 issues/);
  const calls = x.fixture.requests.filter(
    (r) => r.path === "/api/removals/check",
  );
  assert.equal(calls.length, 4);
  assert.equal(x.fixture.maintenanceChecks, 4);
});
