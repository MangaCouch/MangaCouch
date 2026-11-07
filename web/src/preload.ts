import { useEffect, useState } from "react";
import { ApiError, aborted } from "./api";
import type { Pages } from "./pages";

function yieldToReader(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const cancel = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, 50);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

export function useWarmPages(
  pages: Pages,
  total: number,
  start = 0,
  reading = false,
  enabled = true,
) {
  const [progress, setProgress] = useState(() => pages.progress(total));
  const [attempt, setAttempt] = useState(0);
  const [halted, setHalted] = useState(false);
  useEffect(() => setHalted(false), [pages]);
  useEffect(() => {
    const update = () => setProgress(pages.progress(total));
    update();
    return pages.subscribe(update);
  }, [pages, total]);
  useEffect(() => {
    if (!enabled || halted) return;
    pages.focus(start);
    const controller = new AbortController();
    void (async () => {
      const nearby = [
        start,
        start + 1,
        start - 1,
        start + 2,
        start - 2,
        start + 3,
        start - 3,
        start + 4,
        start + 5,
        start + 6,
        start + 7,
        start + 8,
      ];
      for (const index of nearby.filter(
        (value) => value >= 0 && value < total,
      )) {
        // Skip what is cached now (evicted pages are fetched again) and known failures,
        // which only an explicit retry clears.
        if (pages.has(index) || pages.state(index) === "failed") continue;
        if (pages.warmEnough() && index > start + 1) break;
        try {
          await pages.get(index, controller.signal, true);
        } catch (error) {
          if (aborted(error) || controller.signal.aborted) return;
          if (
            error instanceof ApiError &&
            [401, 403, 503].includes(error.status)
          ) {
            setHalted(true);
            return;
          }
        }
        if (controller.signal.aborted) return;
        if (reading) {
          try {
            await yieldToReader(controller.signal);
          } catch {
            return;
          }
        }
      }
    })();
    return () => controller.abort();
  }, [pages, total, start, reading, enabled, attempt, halted]);
  return {
    ...progress,
    paused: halted || (!enabled && progress.waiting > 0),
    retry: () => {
      pages.retryFailed(total);
      setHalted(false);
      setAttempt((value) => value + 1);
    },
  };
}
