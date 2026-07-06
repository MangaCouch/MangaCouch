// Continuous vertical scroll (webtoon) view. All pages stack vertically; the
// page nearest the viewport center becomes "current" and drives progress. Each
// page uses SmartImage so a broken page still shows a retry, not a gap.

import { useEffect, useRef } from 'react';
import { pageImageUrl } from '../api/endpoints';
import { SmartImage } from '../components/SmartImage';
import type { ReaderState } from './useReader';

export function ScrollView({
  reader,
  archiveId,
}: {
  reader: ReaderState;
  archiveId: string;
}) {
  const { pages, settings, current, goToPage, forceLoadAll } = reader;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const pageRefs = useRef<(HTMLDivElement | null)[]>([]);
  const scrolledToResume = useRef(false);

  // Observe which page is centered to keep `current`/progress in sync. The
  // observer only signals "something crossed a boundary"; the actual pick is
  // the page spanning (or nearest to) the viewport center, so pages taller
  // than the viewport — whose intersection ratio never gets large — still
  // become current while scrolling through them.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const updateCurrent = () => {
      const box = container.getBoundingClientRect();
      const centerY = box.top + box.height / 2;
      let bestIndex = -1;
      let bestDist = Number.POSITIVE_INFINITY;
      pageRefs.current.forEach((el, index) => {
        if (!el) return;
        const r = el.getBoundingClientRect();
        const dist =
          r.top > centerY ? r.top - centerY : r.bottom < centerY ? centerY - r.bottom : 0;
        if (dist < bestDist) {
          bestDist = dist;
          bestIndex = index;
        }
      });
      if (bestIndex >= 0) goToPage(bestIndex);
    };
    const obs = new IntersectionObserver(updateCurrent, {
      root: container,
      threshold: [0, 0.25, 0.5, 0.75, 1],
    });
    for (const el of pageRefs.current) if (el) obs.observe(el);
    return () => obs.disconnect();
  }, [pages.length, goToPage]);

  // Jump to the resume page once after pages are laid out.
  useEffect(() => {
    if (scrolledToResume.current || pages.length === 0) return;
    const el = pageRefs.current[current];
    if (el) {
      el.scrollIntoView({ block: 'start' });
      scrolledToResume.current = true;
    }
  }, [pages.length, current]);

  const fitClass = `scroll--fit-${settings.fit}`;

  return (
    <div ref={containerRef} className={`scroll ${fitClass}`}>
      <div className="scroll__inner">
        {pages.map((page, index) => (
          <div
            key={index}
            ref={(el) => {
              pageRefs.current[index] = el;
            }}
            data-index={index}
            className="scroll__page"
          >
            <SmartImage
              src={pageImageUrl(archiveId, page.path)}
              alt={`Page ${index + 1}`}
              className="scroll__img"
              loading="lazy"
              forceLoad={forceLoadAll}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
