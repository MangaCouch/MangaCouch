// Search bar supporting the raw `namespace:value, …` comma syntax (passed
// through to the server as `q`), sort controls, category filter, and the
// random / new-only affordances.

import { useEffect, useState } from 'react';
import type { Category } from '../api/types';
import type { SortKey } from '../api/endpoints';
import { t } from '../i18n/strings';

export interface SearchControls {
  q: string;
  category: string;
  sort: SortKey;
  sortdir: 'asc' | 'desc';
  newonly: boolean;
}

export function SearchBar({
  controls,
  categories,
  onChange,
  onRandom,
}: {
  controls: SearchControls;
  categories: Category[];
  onChange: (next: Partial<SearchControls>) => void;
  /** Jump to a random (preferably unread) archive. */
  onRandom: () => void;
}) {
  // Local input state so typing doesn't fire a request per keystroke; the
  // parent debounces by reacting to `q` changes.
  const [text, setText] = useState(controls.q);
  useEffect(() => setText(controls.q), [controls.q]);

  return (
    <div className="searchbar">
      <form
        className="searchbar__form"
        onSubmit={(e) => {
          e.preventDefault();
          onChange({ q: text });
        }}
      >
        <input
          type="search"
          className="searchbar__input"
          placeholder={t('library.search')}
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label={t('library.search')}
        />
        <button type="submit" className="btn btn--icon" aria-label="Search">
          🔍
        </button>
      </form>

      <div className="searchbar__controls">
        <select
          className="select"
          value={controls.category}
          onChange={(e) => onChange({ category: e.target.value })}
          aria-label={t('library.allCategories')}
        >
          <option value="">{t('library.allCategories')}</option>
          {categories.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>

        <select
          className="select"
          value={controls.sort}
          onChange={(e) => onChange({ sort: e.target.value as SortKey })}
          aria-label="Sort by"
        >
          <option value="date_added">{t('library.sort.date_added')}</option>
          <option value="title">{t('library.sort.title')}</option>
          <option value="lastread">{t('library.sort.lastread')}</option>
        </select>

        <button
          type="button"
          className="btn btn--icon"
          title={controls.sortdir === 'asc' ? 'Ascending' : 'Descending'}
          onClick={() =>
            onChange({ sortdir: controls.sortdir === 'asc' ? 'desc' : 'asc' })
          }
          aria-label="Toggle sort direction"
        >
          {controls.sortdir === 'asc' ? '▲' : '▼'}
        </button>

        <button
          type="button"
          className={`btn ${controls.newonly ? 'btn--active' : ''}`}
          onClick={() => onChange({ newonly: !controls.newonly })}
        >
          {t('library.newonly')}
        </button>

        <button
          type="button"
          className="btn"
          onClick={onRandom}
          title={t('library.random')}
        >
          🎲 {t('library.random')}
        </button>
      </div>
    </div>
  );
}
