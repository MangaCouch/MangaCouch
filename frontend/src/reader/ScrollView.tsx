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

  // Observe which page is centered to keep `current`/progress in sync.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const obs = new IntersectionObserver(
      (entries) => {
        // Pick the most-visible intersecting page.
        let best: { index: number; ratio: number } | null = null;
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const index = Number((entry.target as HTMLElement).dataset.index);
          if (!best || entry.intersectionRatio > best.ratio) {
            best = { index, ratio: entry.intersectionRatio };
          }
        }
        if (best) goToPage(best.index);
      },
      { root: container, threshold: [0.25, 0.5, 0.75] },
    );
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
