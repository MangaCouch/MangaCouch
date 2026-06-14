// Spread computation for paged double-page mode.
//
// Rules (spec §5.7):
//  - First page (cover) is shown alone.
//  - Last page is shown alone.
//  - A wide/landscape page collapses its spread to a single page.
//  - Otherwise pages are paired.
//
// Because we don't know a page's aspect ratio until its image loads, the
// pairing is computed from a `wideSet` of indices known to be landscape. As
// images load and report their dimensions, the set grows and the spreads are
// recomputed — pages that turn out wide are pulled out of a pair.

import type { PageDescriptor } from '../api/types';

/** A spread is one or two page indices shown together. */
export type Spread = number[];

export function computeSpreads(
  pages: PageDescriptor[],
  doublePage: boolean,
  wideSet: Set<number>,
): Spread[] {
  const n = pages.length;
  if (n === 0) return [];
  if (!doublePage) return pages.map((_, i) => [i]);

  const spreads: Spread[] = [];
  let i = 0;
  while (i < n) {
    // Cover (first page) alone.
    if (i === 0) {
      spreads.push([0]);
      i = 1;
      continue;
    }
    // Last page alone.
    if (i === n - 1) {
      spreads.push([i]);
      i += 1;
      continue;
    }
    // A wide page is shown alone.
    if (wideSet.has(i)) {
      spreads.push([i]);
      i += 1;
      continue;
    }
    // Pair with the next page, unless the next page is wide (then this is alone).
    if (wideSet.has(i + 1)) {
      spreads.push([i]);
      i += 1;
      continue;
    }
    spreads.push([i, i + 1]);
    i += 2;
  }
  return spreads;
}

/** Index of the spread that contains a given page. */
export function spreadIndexForPage(spreads: Spread[], page: number): number {
  for (let s = 0; s < spreads.length; s++) {
    if (spreads[s].includes(page)) return s;
  }
  return 0;
}

/** True when an image's dimensions indicate a landscape/wide page. */
export function isWideImage(img: HTMLImageElement): boolean {
  return img.naturalWidth > img.naturalHeight * 1.05;
}
