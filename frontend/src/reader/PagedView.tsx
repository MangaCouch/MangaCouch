// Paged (slide) reader view. Renders the current spread (1 or 2 pages),
// applies the fit mode, honors RTL/LTR ordering, and exposes prev/next tap
// zones. Wide pages report themselves so a spread collapses to single.

import { useMemo } from 'react';
import { pageImageUrl } from '../api/endpoints';
import { SmartImage } from '../components/SmartImage';
import { isWideImage } from './spreads';
import type { ReaderState } from './useReader';

export function PagedView({
  reader,
  archiveId,
}: {
  reader: ReaderState;
  archiveId: string;
}) {
  const { pages, settings, spreads, currentSpread, markWide, shouldLoad, prev, next } =
    reader;

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
    <div className={`pages ${fitClass}`} data-direction={settings.direction}>
      <button
        type="button"
        className="pages__zone pages__zone--left"
        onClick={onLeftZone}
        aria-label="Previous/next page (left zone)"
      />
      <div className="pages__spread" data-count={ordered.length}>
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
      <button
        type="button"
        className="pages__zone pages__zone--right"
        onClick={onRightZone}
        aria-label="Previous/next page (right zone)"
      />
    </div>
  );
}
