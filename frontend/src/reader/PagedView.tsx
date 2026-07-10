// Paged (slide) reader view. Renders the current spread (1 or 2 pages),
// applies the fit mode, honors RTL/LTR ordering, and exposes full-height
// prev/next tap zones (left/right thirds; the center third toggles the
// chrome). Supports pinch-zoom / pan / double-tap via useZoom. Wide pages
// report themselves so a spread collapses to single.

import { useEffect, useMemo, useRef } from 'react';
import { pageImageUrl } from '../api/endpoints';
import { SmartImage } from '../components/SmartImage';
import { isWideImage } from './spreads';
import type { ReaderState } from './useReader';
import { useZoom } from './useZoom';

export function PagedView({
  reader,
  archiveId,
}: {
  reader: ReaderState;
  archiveId: string;
}) {
  const {
    pages,
    settings,
    spreads,
    currentSpread,
    current,
    forceLoadAll,
    markWide,
    shouldLoad,
    prev,
    next,
  } = reader;

  const containerRef = useRef<HTMLDivElement | null>(null);
  const zoom = useZoom(containerRef);
  const { reset: resetZoom } = zoom;

  // A page turn always starts un-zoomed.
  useEffect(() => {
    resetZoom();
  }, [current, resetZoom]);

  // Warm the neighbour pages within the preload window (the spread itself only
  // renders the current pages, so without this every page turn cold-fetches).
  const preloaded = useRef(new Map<number, HTMLImageElement>());
  useEffect(() => {
    preloaded.current.clear();
  }, [archiveId]);
  useEffect(() => {
    const radius = forceLoadAll
      ? pages.length
      : Math.max(1, settings.preload * (settings.doublePage ? 2 : 1));
    for (let d = 1; d <= radius; d++) {
      for (const idx of [current + d, current - d]) {
        const page = pages[idx];
        if (!page || preloaded.current.has(idx)) continue;
        const img = new Image();
        img.src = pageImageUrl(archiveId, page.path);
        preloaded.current.set(idx, img);
      }
    }
    // Drop references outside the window so decoded bitmaps can be reclaimed
    // (the HTTP cache still has the bytes for an instant re-fetch).
    if (!forceLoadAll) {
      for (const idx of preloaded.current.keys()) {
        if (Math.abs(idx - current) > radius) preloaded.current.delete(idx);
      }
    }
  }, [current, forceLoadAll, pages, settings.preload, settings.doublePage, archiveId]);

  const fitClass = `pages--fit-${settings.fit}`;
  const spread = useMemo(() => spreads[currentSpread] ?? [], [spreads, currentSpread]);

  // For RTL manga, the visual order of a two-page spread is reversed.
  const ordered = useMemo(() => {
    if (settings.direction === 'rtl' && spread.length === 2) {
      return [...spread].reverse();
    }
    return spread;
  }, [spread, settings.direction]);

  // Tap zones: in RTL, the left zone advances. In LTR, the right zone advances.
  const onLeftZone = settings.direction === 'rtl' ? next : prev;
  const onRightZone = settings.direction === 'rtl' ? prev : next;

  return (
    <div
      ref={containerRef}
      className={`pages ${fitClass} ${zoom.zoomed ? 'pages--zoomed' : ''}`}
      data-direction={settings.direction}
      onTouchStart={zoom.handlers.onTouchStart}
      onTouchMove={zoom.handlers.onTouchMove}
      onTouchEnd={zoom.handlers.onTouchEnd}
      onWheel={zoom.handlers.onWheel}
      onDoubleClick={zoom.handlers.onDoubleClick}
    >
      {!zoom.zoomed && (
        <button
          type="button"
          className="pages__zone pages__zone--left"
          onClick={onLeftZone}
          aria-label="Previous/next page (left zone)"
        />
      )}
      <div className="pages__spread" data-count={ordered.length} style={zoom.style}>
        {ordered.map((index) => {
          const page = pages[index];
          if (!page) return null;
          return (
            <div className="pages__slot" key={index}>
              <SmartImage
                src={shouldLoad(index) ? pageImageUrl(archiveId, page.path) : ''}
                alt={`Page ${index + 1}`}
                className="pages__img"
                loading={index === reader.current ? 'eager' : 'lazy'}
                forceLoad={reader.forceLoadAll}
                onLoaded={(img) => {
                  if (isWideImage(img)) markWide(index);
                }}
              />
            </div>
          );
        })}
      </div>
      {!zoom.zoomed && (
        <button
          type="button"
          className="pages__zone pages__zone--right"
          onClick={onRightZone}
          aria-label="Previous/next page (right zone)"
        />
      )}
    </div>
  );
}
