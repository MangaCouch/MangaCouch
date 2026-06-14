// Reader settings: the persisted default reading preferences (spec §5.7 —
// "remember default reading settings") and local bookmarks.

import { lsGet, lsSet } from '../lib/storage';

export type ReadMode = 'paged' | 'scroll';
export type ReadDirection = 'ltr' | 'rtl';
export type FitMode = 'width' | 'height' | 'container' | 'original';

export interface ReaderSettings {
  mode: ReadMode;
  direction: ReadDirection;
  /** Double-page spreads (only meaningful in paged mode). */
  doublePage: boolean;
  fit: FitMode;
  /** Number of images to preload ahead/behind (doubled in double-page mode). */
  preload: number;
  /** Autoplay interval in seconds; 0 = off. */
  autoplaySeconds: number;
}

const SETTINGS_KEY = 'readerSettings';

export const DEFAULT_SETTINGS: ReaderSettings = {
  mode: 'paged',
  direction: 'rtl', // manga default
  doublePage: false,
  fit: 'container',
  preload: 2,
  autoplaySeconds: 0,
};

export function loadSettings(): ReaderSettings {
  return { ...DEFAULT_SETTINGS, ...lsGet<Partial<ReaderSettings>>(SETTINGS_KEY, {}) };
}

export function saveSettings(settings: ReaderSettings): void {
  lsSet(SETTINGS_KEY, settings);
}

// ---- Bookmarks (local) ----------------------------------------------------

export interface Bookmark {
  archiveId: string;
  page: number;
  /** 0-based page index. */
  createdAt: number;
  title?: string;
}

const BOOKMARKS_KEY = 'bookmarks';

export function loadBookmarks(): Bookmark[] {
  return lsGet<Bookmark[]>(BOOKMARKS_KEY, []);
}

export function bookmarksFor(archiveId: string): Bookmark[] {
  return loadBookmarks().filter((b) => b.archiveId === archiveId);
}

export function addBookmark(b: Bookmark): void {
  const all = loadBookmarks();
  if (all.some((x) => x.archiveId === b.archiveId && x.page === b.page)) return;
  all.push(b);
  lsSet(BOOKMARKS_KEY, all);
}

export function removeBookmark(archiveId: string, page: number): void {
  const all = loadBookmarks().filter(
    (b) => !(b.archiveId === archiveId && b.page === page),
  );
  lsSet(BOOKMARKS_KEY, all);
}

export function isBookmarked(archiveId: string, page: number): boolean {
  return loadBookmarks().some((b) => b.archiveId === archiveId && b.page === page);
}
