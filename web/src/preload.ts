import { useEffect, useRef, useState } from "react";
import { ApiError, PageLimitError, aborted } from "./api";
import { WINDOW } from "./pages";
import type { Pages } from "./pages";

/**
 * Warm-up order: the page, its memory window both ways, then up to thirty pages ahead
 * (into the browser's HTTP cache; only the window is kept in memory).
 */
function warmOrder(start: number, total: number) {
  const saveData = (
    navigator as Navigator & { connection?: { saveData?: boolean } }
  ).connection?.saveData;
  const ahead = saveData ? 3 : 30;
  const order = [start];
  for (let step = 1; step <= Math.min(WINDOW, ahead); step++)
    order.push(start + step, start - step);
  for (
    let page = start + Math.min(WINDOW, ahead) + 1;
    page < Math.min(total, start + ahead + 1);
    page++
  )
    order.push(page);
  return order.filter((value) => value >= 0 && value < total);
}

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
  enabled = true,
) {
  const [progress, setProgress] = useState(() => pages.progress(total));
  const budget = useRef({ pages, start, remaining: 200 * 1024 ** 2 });
  const [attempt, setAttempt] = useState(0);
  const [halted, setHalted] = useState(false);
  const [recoverable, setRecoverable] = useState(false);
  useEffect(() => {
    if (!halted || !recoverable) return;
    const timer = setTimeout(() => {
      pages.retryFailed(total);
      setHalted(false);
    }, 10000);
    return () => clearTimeout(timer);
  }, [halted, recoverable, pages, total]);
  useEffect(() => setHalted(false), [pages]);
  useEffect(() => {
    let last = pages.foregroundSuccess;
    const update = () => {
      setProgress(pages.progress(total));
      if (pages.foregroundSuccess > last) {
        last = pages.foregroundSuccess;
        setHalted(false);
        pages.retryFailed(total);
      }
    };
    update();
    return pages.subscribe(update);
  }, [pages, total]);
  useEffect(() => {
    if (!enabled || halted) return;
    pages.focus(start);
    const controller = new AbortController();
    void (async () => {
      if (budget.current.pages !== pages || budget.current.start !== start)
        budget.current = { pages, start, remaining: 200 * 1024 ** 2 };
      const allowance = budget.current;
      for (const index of warmOrder(start, total)) {
        // Skip what is in memory now and known failures, which only an explicit retry
        // clears.
        if (pages.has(index) || pages.state(index) === "failed") continue;
        // Pages beyond the memory window download into the browser's HTTP cache only;
        // they move into memory as reading approaches them.
        const store = pages.near(index);
        if (!store && pages.state(index) === "done") continue;
        try {
          if (allowance.remaining <= 0) return;
          const blob = await pages.get(
            index,
            controller.signal,
            true,
            store,
            allowance.remaining,
          );
          allowance.remaining -= blob.size;
        } catch (error) {
          if (aborted(error) || controller.signal.aborted) return;
          if (error instanceof PageLimitError) return;
          if (
            error instanceof ApiError &&
            [401, 403, 404, 409, 503].includes(error.status)
          ) {
            setRecoverable(error.status === 503);
            setHalted(true);
            return;
          }
        }
        if (controller.signal.aborted) return;
        try {
          await yieldToReader(controller.signal);
        } catch {
          return;
        }
      }
    })();
    return () => controller.abort();
  }, [pages, total, start, enabled, attempt, halted]);
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
