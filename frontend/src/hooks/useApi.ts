// Lightweight data-fetching hooks. We deliberately avoid react-query to keep
// the bundle lean (spec: "keep deps lean"); a small fetch + state machine
// covers the app's needs.

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';

export interface AsyncState<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  /** Re-run the fetcher. */
  reload: () => void;
}

/**
 * Run an async fetcher and track loading/error/data. The fetcher receives an
 * AbortSignal so in-flight requests cancel on unmount or dependency change.
 * `deps` controls when it re-runs (like useEffect deps).
 */
export function useAsync<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  // Keep the latest fetcher without making it a dependency.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError(undefined);
    fetcherRef.current(controller.signal)
      .then((result) => {
        if (active) {
          setData(result);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!active) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        // 401 is handled globally (bounces to passcode); don't surface it.
        if (err instanceof ApiError && err.status === 401) return;
        setError(err instanceof Error ? err : new Error(String(err)));
        setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

/** Poll a fetcher on an interval. Useful for the live job list. */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  enabled = true,
): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    let timer: number | undefined;
    const controller = new AbortController();

    const run = async () => {
      try {
        const result = await fetcherRef.current(controller.signal);
        if (active) {
          setData(result);
          setError(undefined);
          setLoading(false);
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (active) {
          if (!(err instanceof ApiError && err.status === 401)) {
            setError(err instanceof Error ? err : new Error(String(err)));
          }
          setLoading(false);
        }
      } finally {
        if (active) timer = window.setTimeout(run, intervalMs);
      }
    };
    run();
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [intervalMs, enabled, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
