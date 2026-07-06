// Core reader state machine. Owns: page list fetch, resume point, current
// page, debounced progress persistence, reader settings, the wide-page set,
// the force-load-all flag, and the set of pages eligible to load (preload
// window). Both the paged and scroll views consume this.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getArchive, getPages, setProgress } from '../api/endpoints';
import type { Archive, PageDescriptor } from '../api/types';
import { ApiError } from '../api/client';
import {
  loadSettings,
  saveSettings,
  type ReaderSettings,
} from './settings';
import { computeSpreads, spreadIndexForPage, type Spread } from './spreads';

const PROGRESS_DEBOUNCE_MS = 1200;

export interface ReaderState {
  archive: Archive | undefined;
  pages: PageDescriptor[];
  loading: boolean;
  error: Error | undefined;
  /** Current 0-based page index. */
  current: number;
  /** Total pages. */
  total: number;
  settings: ReaderSettings;
  updateSettings: (patch: Partial<ReaderSettings>) => void;
  spreads: Spread[];
  /** Index of the spread containing `current`. */
  currentSpread: number;
  goToPage: (page: number) => void;
  goToSpread: (spreadIndex: number) => void;
  next: () => void;
  prev: () => void;
  atStart: boolean;
  atEnd: boolean;
  /** Register that a page's image is landscape/wide (collapses its spread). */
  markWide: (index: number) => void;
  wideSet: Set<number>;
  /** True once the user pressed "Load all images". */
  forceLoadAll: boolean;
  triggerLoadAll: () => void;
  /** Whether a given page index is within the active load/preload window. */
  shouldLoad: (index: number) => boolean;
}

export function useReader(
  archiveId: string,
  initialPage: number | undefined,
): ReaderState {
  const [archive, setArchive] = useState<Archive | undefined>(undefined);
  const [pages, setPages] = useState<PageDescriptor[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [current, setCurrent] = useState(0);
  const [settings, setSettings] = useState<ReaderSettings>(() => loadSettings());
  const [wideSet, setWideSet] = useState<Set<number>>(() => new Set());
  const [forceLoadAll, setForceLoadAll] = useState(false);

  const progressTimer = useRef<number | undefined>(undefined);
  const lastSaved = useRef<number>(-1);
  const initialApplied = useRef(false);

  // Fetch archive (for resume point + metadata) and the page list together.
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    initialApplied.current = false;
    Promise.all([
      getArchive(archiveId, controller.signal),
      getPages(archiveId, controller.signal),
    ])
      .then(([arch, pagesRes]) => {
        if (!active) return;
        setArchive(arch);
        const sorted = [...pagesRes.pages].sort((a, b) => a.index - b.index);
        setPages(sorted);
        // Resume: explicit ?page wins; else the saved progress; else 0.
        const resume =
          initialPage != null && initialPage >= 0
            ? initialPage
            : (arch.progress ?? 0);
        const clamped = Math.max(0, Math.min(resume, Math.max(0, sorted.length - 1)));
        setCurrent(clamped);
        lastSaved.current = clamped;
        initialApplied.current = true;
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!active) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof ApiError && err.status === 401) return;
        setError(err instanceof Error ? err : new Error(String(err)));
        setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [archiveId, initialPage]);

  const total = pages.length;

  const updateSettings = useCallback((patch: Partial<ReaderSettings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch };
      saveSettings(next);
      return next;
    });
  }, []);

  const markWide = useCallback((index: number) => {
    setWideSet((prev) => {
      if (prev.has(index)) return prev;
      const next = new Set(prev);
      next.add(index);
      return next;
    });
  }, []);

  const triggerLoadAll = useCallback(() => setForceLoadAll(true), []);

  const spreads = useMemo(
    () => computeSpreads(pages, settings.mode === 'paged' && settings.doublePage, wideSet),
    [pages, settings.mode, settings.doublePage, wideSet],
  );

  const currentSpread = useMemo(
    () => spreadIndexForPage(spreads, current),
    [spreads, current],
  );

  // Debounced progress persistence (spec §5.7: one server-side progress model).
  const persistProgress = useCallback(
    (page: number) => {
      // Always cancel a pending save first: flipping to page N and back before
      // the debounce fires must not write N as the resume point.
      window.clearTimeout(progressTimer.current);
      if (page === lastSaved.current) return;
      progressTimer.current = window.setTimeout(() => {
        lastSaved.current = page;
        // Fire and forget; a failed progress write is non-fatal.
        setProgress(archiveId, page).catch(() => {});
      }, PROGRESS_DEBOUNCE_MS);
    },
    [archiveId],
  );

  const goToPage = useCallback(
    (page: number) => {
      setCurrent((prev) => {
        const clamped = Math.max(0, Math.min(page, Math.max(0, total - 1)));
        if (clamped !== prev) persistProgress(clamped);
        return clamped;
      });
    },
    [total, persistProgress],
  );

  const goToSpread = useCallback(
    (spreadIndex: number) => {
      const idx = Math.max(0, Math.min(spreadIndex, spreads.length - 1));
      const first = spreads[idx]?.[0] ?? 0;
      goToPage(first);
    },
    [spreads, goToPage],
  );

  const next = useCallback(() => {
    if (settings.mode === 'paged' && settings.doublePage) {
      goToSpread(currentSpread + 1);
    } else {
      goToPage(current + 1);
    }
  }, [settings.mode, settings.doublePage, currentSpread, current, goToSpread, goToPage]);

  const prev = useCallback(() => {
    if (settings.mode === 'paged' && settings.doublePage) {
      goToSpread(currentSpread - 1);
    } else {
      goToPage(current - 1);
    }
  }, [settings.mode, settings.doublePage, currentSpread, current, goToSpread, goToPage]);

  const atStart = current <= 0;
  const atEnd = current >= total - 1;

  // Track the latest current page in a ref so the unmount handler reads the
  // final value rather than a stale closure.
  const currentRef = useRef(current);
  currentRef.current = current;

  // Persist final progress on unmount (flush the debounce).
  useEffect(() => {
    return () => {
      window.clearTimeout(progressTimer.current);
      const page = currentRef.current;
      if (initialApplied.current && page !== lastSaved.current) {
        setProgress(archiveId, page).catch(() => {});
      }
    };
  }, [archiveId]);

  // Preload window: load the current page plus N neighbours each way
  // (doubled in double-page mode per spec).
  const shouldLoad = useCallback(
    (index: number) => {
      if (forceLoadAll) return true;
      if (settings.mode === 'scroll') {
        // Scroll mode relies on native lazy loading + IntersectionObserver in
        // the view; treat everything as loadable there.
        return true;
      }
      const radius = settings.preload * (settings.doublePage ? 2 : 1);
      return Math.abs(index - current) <= Math.max(1, radius);
    },
    [forceLoadAll, settings.mode, settings.preload, settings.doublePage, current],
  );

  return {
    archive,
    pages,
    loading,
    error,
    current,
    total,
    settings,
    updateSettings,
    spreads,
    currentSpread,
    goToPage,
    goToSpread,
    next,
    prev,
    atStart,
    atEnd,
    markWide,
    wideSet,
    forceLoadAll,
    triggerLoadAll,
    shouldLoad,
  };
}
