import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { aborted } from "./api";

/** False inside a collapsed settings section: polling below it pauses until it opens again. */
export const PollingVisible = createContext(true);

const hidden = () => typeof document !== "undefined" && !!document.hidden;

/**
 * Resolves after `ms`, and then only once the tab is visible again, for a loop that polls
 * a server job; rejects with the signal's reason when it aborts.
 */
export function pollDelay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (error?: unknown) => {
      clearTimeout(timer);
      signal.removeEventListener("abort", stop);
      document.removeEventListener?.("visibilitychange", check);
      if (error === undefined) resolve();
      else reject(error);
    };
    const stop = () => finish(signal.reason);
    const check = () => {
      if (!hidden()) finish();
    };
    if (signal.aborted) return stop();
    signal.addEventListener("abort", stop, { once: true });
    timer = setTimeout(() => {
      if (hidden()) document.addEventListener?.("visibilitychange", check);
      else finish();
    }, ms);
  });
}

/**
 * Runs `fn` now and then every `ms` while enabled. Skips runs while the tab is hidden,
 * runs again when the tab becomes visible or the window gains focus, never overlaps runs,
 * clears `error` after a successful run and stops (aborting the request) on unmount.
 * A change of `key` restarts polling immediately; `ms` may change between runs.
 * `refresh()` runs once now (or right after the run in flight) and never rejects.
 */
export function usePolling(
  fn: (signal: AbortSignal) => Promise<void>,
  ms: number,
  { enabled = true, key }: { enabled?: boolean; key?: unknown } = {},
) {
  const visible = useContext(PollingVisible);
  const [error, setError] = useState<unknown>(null);
  const latest = useRef({ fn, ms });
  latest.current = { fn, ms };
  const trigger = useRef<() => Promise<void>>(async () => {});
  const reschedule = useRef<() => void>(() => {});
  const active = enabled && visible;
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let running: Promise<void> | null = null,
      queued: Promise<void> | null = null;
    const schedule = () => {
      clearTimeout(timer);
      if (!controller.signal.aborted)
        timer = setTimeout(
          () => void run(false),
          failures ? Math.min(60_000, 1000 * 2 ** failures) : latest.current.ms,
        );
    };
    const run = (forced: boolean): Promise<void> => {
      if (running) {
        if (!forced) return running;
        return (queued ??= running.then(() => {
          queued = null;
          return run(true);
        }));
      }
      if (!forced && hidden()) {
        schedule();
        return Promise.resolve();
      }
      running = (async () => {
        try {
          await latest.current.fn(controller.signal);
          if (!controller.signal.aborted) {
            failures = 0;
            setError(null);
          }
        } catch (failure) {
          if (!controller.signal.aborted && !aborted(failure)) {
            failures++;
            if (failures >= 2) setError(failure);
          }
        } finally {
          running = null;
          schedule();
        }
      })();
      return running;
    };
    const wake = () => {
      if (!hidden()) void run(false);
    };
    trigger.current = () => run(true);
    reschedule.current = () => {
      if (!running) schedule();
    };
    void run(false);
    const page = typeof document === "undefined" ? undefined : document;
    page?.addEventListener?.("visibilitychange", wake);
    window.addEventListener?.("focus", wake);
    return () => {
      controller.abort();
      clearTimeout(timer);
      trigger.current = async () => {};
      reschedule.current = () => {};
      page?.removeEventListener?.("visibilitychange", wake);
      window.removeEventListener?.("focus", wake);
    };
  }, [active, key]);
  useEffect(() => reschedule.current(), [ms]);
  const refresh = useCallback(() => trigger.current(), []);
  return { error, refresh };
}
