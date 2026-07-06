// A single library grid card: cover, title, page count, tag chips, and a
// read/progress indicator. Memoized — the infinite-scroll grid re-renders on
// every page append, and hundreds of cards re-filtering tags adds up.

import { memo } from 'react';
import { Link } from 'react-router-dom';
import type { Archive } from '../api/types';
import { thumbnailUrl } from '../api/endpoints';
import { SmartImage } from './SmartImage';
import { isRead, progressFraction, tagDisplay } from '../lib/tags';
import { t } from '../i18n/strings';

const CHIP_NAMESPACES = ['artist', 'group', 'parody', 'series', 'language'];

export const ArchiveCard = memo(function ArchiveCard({ archive }: { archive: Archive }) {
  const read = isRead(archive.progress, archive.page_count);
  const frac = progressFraction(archive.progress, archive.page_count);
  const chips = archive.tags
    .filter((tag) => CHIP_NAMESPACES.includes(tag.namespace))
    .slice(0, 3);

  return (
    <Link to={`/archive/${archive.id}`} className="card" title={archive.title}>
      <div className="card__cover">
        <SmartImage
          src={thumbnailUrl(archive.id)}
          alt={archive.title}
          className="card__cover-img"
          loading="lazy"
        />
        {read && <span className="card__badge card__badge--read">{t('library.read')}</span>}
        {!read && frac > 0 && (
          <div className="card__progress">
            <div className="card__progress-bar" style={{ width: `${frac * 100}%` }} />
          </div>
        )}
        <span className="card__pages">
          {archive.page_count} {t('library.pages')}
        </span>
      </div>
      <div className="card__meta">
        <div className="card__title">{archive.title}</div>
        {chips.length > 0 && (
          <div className="card__chips">
            {chips.map((tag) => (
              <span key={`${tag.namespace}:${tag.value}`} className="card__chip">
                {tagDisplay(tag)}
              </span>
            ))}
          </div>
        )}
      </div>
    </Link>
  );
});
