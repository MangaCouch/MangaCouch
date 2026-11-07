import assert from "node:assert/strict";
import test from "node:test";
import {
  spreads,
  scrollRows,
  scrollWindow,
  scrollHeight,
  anchoredTop,
  pageRatios,
  widePages,
  ROW_GAP,
} from "../src/reading.ts";

test("spreads preserve all original page numbers across skipped, wide and odd-page layouts", () => {
  for (let count = 1; count <= 12; count++)
    for (const cover of [true, false])
      for (let wide = -1; wide < count; wide++) {
        const pages = Array.from({ length: count }, (_, index) => index).filter(
          (index) => index !== 3,
        );
        const groups = spreads(pages, new Set([wide]), cover);
        assert.deepEqual(groups.flat(), pages);
        for (const group of groups) {
          assert.ok(group.length === 1 || group.length === 2);
          if (group.includes(wide) || (cover && group.includes(0)))
            assert.equal(group.length, 1);
        }
      }
});
test("scroll windows remain bounded even for extremely wide pages and large viewports", () => {
  const pages = Array.from({ length: 3000 }, (_, index) => index),
    ratios = new Map(pages.map((page) => [page, 20]));
  for (const height of [300, 600, 1200, 6000]) {
    const rows = scrollRows(pages, 800, ratios, Math.max(120, height / 4));
    for (const top of [0, 81, 1500, scrollHeight(rows) - height]) {
      const range = scrollWindow(rows, top, height);
      assert.ok(range.end - range.start <= 7);
      assert.ok(range.active >= range.start && range.active < range.end);
    }
  }
});
test("dimension and viewport changes preserve the visible page and relative position", () => {
  const before = scrollRows(
    [0, 1, 2],
    700,
    new Map([
      [0, 0.7],
      [1, 0.7],
      [2, 0.7],
    ]),
  );
  const after = scrollRows(
    [0, 1, 2],
    1400,
    new Map([
      [0, 0.7],
      [1, 0.7],
      [2, 0.7],
    ]),
  );
  assert.equal(
    anchoredTop(before, after, before[1].top + 500),
    after[1].top + 1000,
  );
  const without = scrollRows(
    [1, 2],
    700,
    new Map([
      [1, 0.7],
      [2, 0.7],
    ]),
  );
  assert.ok(
    Math.abs(anchoredTop(before, without, before[1].top + 500) - 500) < 0.001,
  );
});
test("one row gap drives fit-height rows and the visible-row lookup", () => {
  const rows = scrollRows([0, 1, 2], 800, new Map(), 120, 600);
  assert.deepEqual(
    rows.map((row) => [row.top, row.height]),
    [
      [0, 600],
      [600 + ROW_GAP, 600],
      [2 * (600 + ROW_GAP), 600],
    ],
  );
  // Row 1 becomes the first visible row exactly where it starts, not a few pixels later.
  assert.equal(scrollWindow(rows, rows[1].top - 1, 600).first, 0);
  assert.equal(scrollWindow(rows, rows[1].top, 600).first, 1);
});
test("page sizes give ratios and mark landscape pages as wide", () => {
  const ratios = pageRatios([
    [700, 1000],
    [1800, 1000],
    [0, 0],
  ]);
  assert.deepEqual([...ratios.keys()], [0, 1]);
  assert.deepEqual([...widePages(ratios)], [1]);
  assert.deepEqual(spreads([0, 1, 2, 3], widePages(ratios), false), [
    [0],
    [1],
    [2, 3],
  ]);
});
