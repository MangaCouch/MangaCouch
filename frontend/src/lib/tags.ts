// Tag helpers: grouping by namespace and resolving display names.

import type { Tag } from '../api/types';

/** Namespaces treated as author/circle, surfaced separately on the detail page. */
export const AUTHOR_NAMESPACES = ['artist', 'group'];
export const LANGUAGE_NAMESPACES = ['language'];

/** Display string for a tag — prefers the localized `translated` value. */
export function tagDisplay(tag: Tag): string {
  return tag.translated?.trim() || tag.value;
}

/** Full `namespace:value` label for a tag (raw, used in search building). */
export function tagToken(tag: Tag): string {
  return `${tag.namespace}:${tag.value}`;
}

/** Group tags by namespace, preserving a sensible namespace ordering. */
export function groupByNamespace(tags: Tag[]): [string, Tag[]][] {
  const map = new Map<string, Tag[]>();
  for (const tag of tags) {
    const ns = tag.namespace || 'other';
    const arr = map.get(ns);
    if (arr) arr.push(tag);
    else map.set(ns, [tag]);
  }
  // Stable, human-friendly namespace ordering; unknowns sorted alphabetically.
  const order = [
    'artist',
    'group',
    'parody',
    'series',
    'character',
    'female',
    'male',
    'mixed',
    'language',
    'event',
    'other',
    'source',
  ];
  return [...map.entries()].sort((a, b) => {
    const ia = order.indexOf(a[0]);
    const ib = order.indexOf(b[0]);
    if (ia !== -1 && ib !== -1) return ia - ib;
    if (ia !== -1) return -1;
    if (ib !== -1) return 1;
    return a[0].localeCompare(b[0]);
  });
}

export function authorTags(tags: Tag[]): Tag[] {
  return tags.filter((t) => AUTHOR_NAMESPACES.includes(t.namespace));
}

export function languageTags(tags: Tag[]): Tag[] {
  return tags.filter((t) => LANGUAGE_NAMESPACES.includes(t.namespace));
}

/** "read" semantics per spec: progress / page_count > 0.85. */
export function isRead(progress: number | null | undefined, pageCount: number): boolean {
  if (!progress || !pageCount) return false;
  return progress / pageCount > 0.85;
}

export function progressFraction(
  progress: number | null | undefined,
  pageCount: number,
): number {
  if (!progress || !pageCount) return 0;
  return Math.min(1, progress / pageCount);
}
