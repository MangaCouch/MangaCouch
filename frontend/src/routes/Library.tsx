// Library view: search bar + responsive cover grid with infinite scroll.
//
// Search/sort/category state lives in the URL query string (shareable per
// spec §5.1 "Searches are shareable as URL query params"). Pages accumulate
// as the user scrolls; a manual "Load more" button is the fallback.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { listArchives, listCategories } from '../api/endpoints';
import type { Archive, Category } from '../api/types';
import type { SortKey } from '../api/endpoints';
import { ApiError } from '../api/client';
import { ArchiveCard } from '../components/ArchiveCard';
import { SearchBar, type SearchControls } from '../components/SearchBar';
import { Spinner, ErrorBanner } from '../components/ui';
import { useAsync } from '../hooks/useApi';
import { t } from '../i18n/strings';

function controlsFromParams(params: URLSearchParams): SearchControls {
  return {
    q: params.get('q') ?? '',
    category: params.get('category') ?? '',
    sort: (params.get('sort') as SortKey) ?? 'date_added',
    sortdir: (params.get('sortdir') as 'asc' | 'desc') ?? 'desc',
    newonly: params.get('newonly') === '1',
    random: params.get('random') === '1',
  };
}

export function Library() {
  const [params, setParams] = useSearchParams();
  const controls = useMemo(() => controlsFromParams(params), [params]);

  const { data: catData } = useAsync<{ categories: Category[] }>(
    (signal) => listCategories(signal),
    [],
  );
  const categories = catData?.categories ?? [];

  // Accumulated results + paging state.
  const [items, setItems] = useState<Archive[]>([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [total, setTotal] = useState<number | null>(null);

  // A key that changes whenever the query (not the page) changes — triggers a
  // full reset of the accumulated list.
  const queryKey = useMemo(
    () =>
      JSON.stringify({
        q: controls.q,
        category: controls.category,
        sort: controls.sort,
        sortdir: controls.sortdir,
        newonly: controls.newonly,
        random: controls.random,
      }),
    [controls],
  );

  // Reset and fetch page 1 when the query changes.
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setItems([]);
    setPage(1);
    setHasMore(true);
    setLoading(true);
    setError(null);
    listArchives(
      {
        q: controls.q || undefined,
        category: controls.category || undefined,
        sort: controls.sort,
        sortdir: controls.sortdir,
        newonly: controls.newonly || undefined,
        random: controls.random || undefined,
        page: 1,
      },
      controller.signal,
    )
      .then((res) => {
        if (!active) return;
        setItems(res.archives);
        setTotal(res.total ?? res.archives.length);
        setHasMore(
          res.archives.length > 0 &&
            (res.total ? res.archives.length < res.total : res.archives.length >= 1) &&
            !controls.random,
        );
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
  }, [queryKey, controls.q, controls.category, controls.sort, controls.sortdir, controls.newonly, controls.random]);

  // Load the next page (append).
  const loadMore = useCallback(async () => {
    if (loading || !hasMore) return;
    const next = page + 1;
    setLoading(true);
    try {
      const res = await listArchives({
        q: controls.q || undefined,
        category: controls.category || undefined,
        sort: controls.sort,
        sortdir: controls.sortdir,
        newonly: controls.newonly || undefined,
        page: next,
      });
      setItems((prev) => {
        const seen = new Set(prev.map((a) => a.id));
        const merged = [...prev, ...res.archives.filter((a) => !seen.has(a.id))];
        return merged;
      });
      setPage(next);
      setTotal(res.total ?? total);
      const loadedSoFar = items.length + res.archives.length;
      setHasMore(
        res.archives.length > 0 && (res.total ? loadedSoFar < res.total : true),
      );
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) {
        setError(err instanceof Error ? err : new Error(String(err)));
      }
    } finally {
      setLoading(false);
    }
  }, [loading, hasMore, page, controls, items.length, total]);

  // Infinite-scroll sentinel.
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting) loadMore();
      },
      { rootMargin: '600px' },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [loadMore]);

  const updateControls = useCallback(
    (next: Partial<SearchControls>) => {
      const merged = { ...controls, ...next };
      const p = new URLSearchParams();
      if (merged.q) p.set('q', merged.q);
      if (merged.category) p.set('category', merged.category);
      if (merged.sort && merged.sort !== 'date_added') p.set('sort', merged.sort);
      if (merged.sortdir && merged.sortdir !== 'desc') p.set('sortdir', merged.sortdir);
      if (merged.newonly) p.set('newonly', '1');
      if (merged.random) p.set('random', '1');
      setParams(p);
    },
    [controls, setParams],
  );

  return (
    <div className="library">
      <SearchBar controls={controls} categories={categories} onChange={updateControls} />

      {error && <ErrorBanner error={error} onRetry={() => updateControls({})} />}

      {total !== null && !error && (
        <div className="library__count">
          {total} {t('library.results')}
        </div>
      )}

      {items.length === 0 && !loading && !error && (
        <div className="library__empty">{t('library.empty')}</div>
      )}

      <div className="grid">
        {items.map((a) => (
          <ArchiveCard key={a.id} archive={a} />
        ))}
      </div>

      {loading && <Spinner label={t('library.loading')} />}

      {!loading && hasMore && (
        <div className="library__more">
          <button type="button" className="btn" onClick={loadMore}>
            {t('library.loadMore')}
          </button>
        </div>
      )}

      <div ref={sentinelRef} className="library__sentinel" aria-hidden />
    </div>
  );
}
